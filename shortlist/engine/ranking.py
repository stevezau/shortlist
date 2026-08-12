"""Candidate ranking — the whole of it, now that the LLM curate step is gone.

Three rules, all learned the hard way:

1. Seed provenance scales a title's score, it never zeroes it. When the score was
   ``seed_frequency x rating x weight``, every candidate from a seedless source — tmdb_discover,
   llm_web — scored exactly 0 and sorted below the worst seeded title on the list.
2. Rating is not similarity. Without `affinity` the only thing separating two single-seed
   candidates was TMDB's average vote — so a well-rated but unrelated show beat an obviously
   similar one, and a row seeded by a medical drama filled up with fantasy and sci-fi. Affinity
   carries "how near the top of TMDB's list for THIS seed was it", which is the actual claim.
3. Each source gets a fair SHARE of the pool (`pre_rank`), then each seed gets a fair share of the
   final row (`diversify_by_seed`). Ranking by score alone cannot do this: 30 seeds x TMDB
   suggestions is hundreds of seeded candidates, so a single global sort fills every slot with
   tmdb_similar however good the rest are, and the other sources — including the web searches we
   paid for — never reach the row at all.
"""

from __future__ import annotations

from shortlist.engine.models import Candidate

_UNATTRIBUTED = "_unattributed"  # a hand-built candidate carrying no source tag

# Years of age that halve a title's ranking weight at full strength. 8 rather than something
# tighter so the slider has usable RANGE: at 8, 50% still means a clear lean (16 years to half) and
# 100% is aggressive without being a ban. A smaller constant would make everything above ~40% feel
# the same. Mirrored in web/src/lib/constants.ts (RECENCY_HALF_LIFE_YEARS) — the UI draws the same
# curve it advertises, and test_recency_curve_matches_the_engine pins the two together.
RECENCY_HALF_LIFE_YEARS = 8.0


def recency_factor(year: int | None, year_now: int, recency: float) -> float:
    """Release-date weight for a title, in ``0..1`` — what ``recency`` multiplies into its score.

    ``0.5 ** (age / RECENCY_HALF_LIFE_YEARS * recency)``: exponential decay whose half-life the
    slider stretches, so ``recency`` behaves like a strength dial rather than an on/off. It is a
    weight and never a filter — an old title is only ever asked to be a better match, never excluded.

    Three inputs mean "no opinion", and all three return exactly ``1.0`` rather than a penalty:
    ``recency <= 0`` (age-blind — how ranking worked before this existed; the SERVER default is now
    0.5 for every install, and only the engine dataclass still defaults to 0), a title with
    no known ``year`` (TMDB serves plenty; the unrated-gets-5.0 prior sets the precedent that a
    missing signal is not a bad one), and ``year_now <= 0`` (a direct engine call with no
    ``run_day``, where treating everything as two millennia old would be worse than doing nothing).

    An undated title therefore ranks as though released THIS year, which is the best weight on
    offer — not the median the unrated prior gives. Deliberate: the alternative (charging it a
    half-life or two) invents an age from nothing and silently demotes a whole class of real
    candidates the moment an owner turns this up. Undated titles are a minority of any pool and they
    still have to win on rating, seed frequency and affinity like everything else; being spared a
    penalty is not the same as being promoted past a title that outscores them.

    Args:
        year: The title's release year (a show's first-air year), or None if unknown.
        year_now: The year the run belongs to, from ``EngineContext.run_day``. 0 disables.
        recency: Strength, 0.0 (ignore age) .. 1.0 (full decay). Values above 1.0 clamp.

    Returns:
        A multiplier in ``(0, 1]``.
    """
    if recency <= 0 or year is None or year_now <= 0:
        return 1.0
    # Clamped at 0: an unreleased title (2027 in 2026) has negative age, and `0.5 ** negative` is
    # GREATER than 1 — it would outrank everything actually watchable tonight.
    age = max(0, year_now - year)
    return 0.5 ** (age / RECENCY_HALF_LIFE_YEARS * min(recency, 1.0))


def score(candidate: Candidate, *, recency: float = 0.0, year_now: int = 0) -> float:
    """How promising a candidate is, before the picker selects from it.

    ``1 + seed_frequency`` (not ``seed_frequency``): "three of your seeds suggested this" is a real
    signal and should win ties, but a title nobody's history pointed at is not worthless — surfacing
    those is exactly what the "widen my taste" sources exist for.

    ``recency`` scales the result by release date (see ``recency_factor``); at its 0.0 default the
    factor is exactly 1.0, so this is the same arithmetic it has always been.
    """
    seed_weight = max((s.weight for s in candidate.seeds), default=0.0)
    rating = candidate.rating or 5.0  # unrated titles get a neutral prior, not zero
    base = (1 + candidate.seed_frequency) * rating * (1.0 + seed_weight) * candidate.affinity
    return base * recency_factor(candidate.year, year_now, recency)


def _sort_key(candidate: Candidate, recency: float = 0.0, year_now: int = 0) -> tuple:
    return (-score(candidate, recency=recency, year_now=year_now), -candidate.rating, candidate.title)


def cut_for_recency(
    in_library: list[Candidate],
    kinds: list,
    keep: int,
    recency: float,
    year_now: int,
) -> list[Candidate]:
    """Re-take the per-media ``pre_rank`` cut at this row's own release-date weight.

    The candidate GATHER is shared between rows (``RowPolicy.pool_key``) because it is the expensive
    part — TMDB, Trakt, Exa, the LLM. The truncation that follows it is neither expensive nor
    shareable: it is a sort over a list already in memory, and two rows that disagree about release
    date need two different cuts. So the pool keeps caching the gather, and this re-runs only the
    cut, which is what lets one person hold a "New & Notable" row at full strength beside a "Hidden
    Gems" row at zero without paying for a second gather.

    Per media type, exactly like the pool's own cut: a `both` row whose pool skews one way must not
    truncate the other type away before its library's collection is ever built.
    """
    return [
        c for kind in kinds for c in pre_rank([x for x in in_library if x.media_type is kind], keep, recency, year_now)
    ]


def pre_rank(candidates: list[Candidate], keep: int, recency: float = 0.0, year_now: int = 0) -> list[Candidate]:
    """Top `keep` candidates, giving every source a turn (best-first within each).

    Round-robin, not a global sort: each source offers its best remaining candidate in turn until
    `keep` is full. A source with few candidates runs out and the others take up the slack, so
    enabling a narrow source never costs you the breadth of a wide one — and a wide one can never
    shut a narrow one out.

    ``recency`` participates in the CUT, not just the order within it. Truncating on the base score
    and weighting afterwards would cap the setting's reach at whatever survived: on a library whose
    pool exceeds `keep` — the common case for a catalog-deep server, which is exactly who needs this
    — a newer title ranking below the cap could never be rescued however high the owner turned it.
    """
    ranked = sorted(candidates, key=lambda c: _sort_key(c, recency, year_now))
    if len(ranked) <= keep:
        return ranked

    queues: dict[str, list[Candidate]] = {}
    for candidate in ranked:  # already best-first, so each queue is too
        for source in candidate.sources or {_UNATTRIBUTED}:
            queues.setdefault(source, []).append(candidate)

    picked: list[Candidate] = []
    taken: set[tuple[int, object]] = set()
    while len(picked) < keep:
        progressed = False
        for source in sorted(queues):
            queue = queues[source]
            while queue:
                candidate = queue.pop(0)
                key = (candidate.tmdb_id, candidate.media_type)
                if key in taken:
                    continue  # another source already offered this exact title
                taken.add(key)
                picked.append(candidate)
                progressed = True
                break
            if len(picked) >= keep:
                break
        if not progressed:  # every queue is exhausted
            break
    return sorted(picked, key=lambda c: _sort_key(c, recency, year_now))


def diversify_by_seed(candidates: list[Candidate], keep: int) -> list[Candidate]:
    """Top `keep` candidates spread across the tastes that seeded them (best-first within each seed).

    This is the final row-selection step that used to be the LLM curate call's job. `pre_rank` gives
    each *source* a fair share; this gives each *seed* one. Without it a single heavily-watched title
    dominates the row: a person with 30 Breaking Bad look-alikes and a handful of everything else got
    a row of nothing but Breaking Bad, and their other tastes dropped off entirely (measured: 2 of 4
    tastes survived to an 8-slot row; with this step, all 4 do). The single best-scoring title is
    always kept first, so diversifying never displaces the strongest pick.

    Input must already be best-first (``pre_rank`` output). Candidates sharing a ``top_seed`` queue
    together; seeds are visited in the order their best candidate appears, one taken per seed per pass
    until `keep` is full. Seedless candidates (discover / web / library) share one queue keyed ``None``.
    """
    if len(candidates) <= keep:
        return candidates

    queues: dict[int | None, list[Candidate]] = {}
    order: list[int | None] = []  # seeds in order of their strongest candidate's rank
    for candidate in candidates:  # already best-first, so each queue is too
        key = candidate.top_seed.tmdb_id if candidate.top_seed else None
        if key not in queues:
            queues[key] = []
            order.append(key)
        queues[key].append(candidate)

    picked: list[Candidate] = []
    while len(picked) < keep:
        progressed = False
        for key in order:
            queue = queues[key]
            if queue:
                picked.append(queue.pop(0))
                progressed = True
                if len(picked) >= keep:
                    break
        if not progressed:  # every queue is exhausted
            break
    return picked
