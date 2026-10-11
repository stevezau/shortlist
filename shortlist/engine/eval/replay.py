"""Leave-one-out replay: would this config have surfaced a title the person went on to watch?

Hide one watch, rebuild the pool from everything they had watched BEFORE it, and see where the
hidden title lands. Run the same cases under two `EngineConfig`s and the difference is attributable
to the config, because nothing else changed.

**This module performs no writes.** It reads history and asks TMDB for candidates — the same
read-only calls a normal run makes — and never touches a collection, a label or a share filter. None
of `plex-safety.md`'s write-ordering rules are in scope here, and that is deliberate: the question
"would this variant have surfaced it" is answerable from gather + filter + rank alone, so paying for
the delivery machinery would buy nothing.

Method: Cremonesi, Koren & Turrin (RecSys 2010), adapted. Not their literal construction — the true
item against 1000 random negatives — because that measures a scorer which ranks the whole catalogue,
and Shortlist only ever ranks what `gather_candidates` proposed. Running the real pipeline on the
reduced history is the honest analogue, and it is what "would this variant have surfaced it" actually
means.

**Read the numbers with the sample size in mind.** Ten to forty users at up to five holdouts each is
at most a couple of hundred paired cases. That is enough to catch a change that breaks something, and
not enough to prove a subtle improvement is real. The per-case table is the deliverable; the
aggregate is a gut-check. `summarise()` says so in its own output rather than leaving a number to
imply more confidence than it can carry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from shortlist.engine import candidates as candidates_mod
from shortlist.engine import history as history_mod
from shortlist.engine import ranking
from shortlist.engine.clients.tmdb import Cache, TmdbClient
from shortlist.engine.models import Candidate, EngineConfig, MediaType, UserProfile, WatchedItem
from shortlist.engine.taste import history_mix, recent_taste

#: Agreement among non-tied paired cases below which the aggregate direction means nothing at this
#: sample size. Reported, never enforced — it exists to stop a 51/49 split reading as a result.
TRUST_THRESHOLD = 0.7


@dataclass(frozen=True)
class HoldoutCase:
    """One hidden watch, and only the history that preceded it."""

    user: UserProfile
    held_out: WatchedItem
    #: Every OTHER watch strictly BEFORE `held_out.watched_at`. Strictly, because a later watch in
    #: the seed set is the whole experiment leaking into itself.
    history_before: list[WatchedItem]


@dataclass(frozen=True)
class ReplayOutcome:
    """Where the hidden title landed under one config."""

    case: HoldoutCase
    config_label: str
    #: Did `gather_candidates` propose it at all? This is the ceiling ranking cannot exceed, and
    #: separating it from rank is what tells a gather regression apart from a ranking one.
    gathered: bool
    #: `filter_candidates`' own reason, when it was proposed and then removed. Reused verbatim rather
    #: than re-derived, so eval and production always agree about why something went.
    drop_reason: str = ""
    rank: int | None = None
    #: Reported beside `rank` because "9th" alone means nothing — 9th of 12 and 9th of 400 are
    #: different answers, and a config that changes the pool size changes what a rank is worth.
    pool_size: int = 0
    in_final_row: bool = False
    reciprocal_rank: float = 0.0
    #: `dislike_threshold` silently does nothing for an account whose ratings are mostly tool-written
    #: (`history.ratings_are_trustworthy` judges the whole account at once). Surfaced so a flat
    #: result reads as "not applicable here", not "the change did nothing".
    ratings_trusted: bool = True
    #: The web search's own extraction held the title, before the AI chose anything (#152). With
    #: `ai_picked` it separates "the search never found it" from "the search found it and the AI passed".
    found_by_search: bool = False
    #: The AI proposed the title (and it resolved to TMDB). False when the AI path did not run.
    ai_picked: bool = False
    #: LLM tokens this case spent, and the web searches that were not served from the cache.
    tokens: int = 0
    new_searches: int = 0
    #: The AI answered with nothing usable, so the run used the search's whole extraction instead. Counted apart:
    #: it makes `ai_picked` true for reasons that have nothing to do with what the AI was told.
    ai_fallback: bool = False
    #: Per kind of search ("recent", "favourite", "older"), the 1-based positions within that kind whose extraction
    #: held the title. "Would F favourites have found it" is then "any position <= F", for every F from one run.
    found_by_group: dict[str, list[int]] = field(default_factory=dict)

    @property
    def hit(self) -> bool:
        return self.in_final_row


@dataclass
class Comparison:
    """Two configs over the same cases, and what can honestly be said about the difference."""

    label_a: str
    label_b: str
    outcomes_a: list[ReplayOutcome] = field(default_factory=list)
    outcomes_b: list[ReplayOutcome] = field(default_factory=list)


def holdout_cases(
    user: UserProfile,
    history: list[WatchedItem],
    *,
    max_holdouts: int = 5,
    keep: Callable[[WatchedItem], bool] | None = None,
) -> list[HoldoutCase]:
    """Up to `max_holdouts` of this person's most recent watches, each with the history before it.

    A movie watched more than once is never a case: it is a rewatch, not a discovery. ``keep`` drops
    anything else the caller knows is not one (a show started long before this watch), BEFORE the newest
    `max_holdouts` are taken, so the cap counts discoveries rather than whatever happened to be newest.

    Each case slices the history independently at its own timestamp rather than removing one item
    from the whole list. Removing one item leaves every LATER watch in place, so testing a person's
    five most recent watches would leak four of them into the earliest case's seeds and quietly
    inflate every number.

    No completion filter: `WatchCache` applies `min_completion` when it stores a watch, so anything
    reaching this function already qualified.
    """
    # Each library's copy of a title carries its own play count, so a film played once in "Movies" last
    # year and once in "4K Movies" last week reads as watch_count 1 twice. Any earlier copy makes it a rewatch.
    first_seen: dict[tuple[int, MediaType], datetime] = {}
    for w in history:
        if w.tmdb_id is not None:
            key = (w.tmdb_id, w.media_type)
            first_seen[key] = min(first_seen.get(key, w.watched_at), w.watched_at)
    eligible = sorted(
        (
            w
            for w in history
            if w.tmdb_id is not None
            and not (w.media_type is MediaType.MOVIE and w.watch_count > 1)
            and first_seen[(w.tmdb_id, w.media_type)] == w.watched_at
            and (keep is None or keep(w))
        ),
        key=lambda w: w.watched_at,
        reverse=True,
    )[:max_holdouts]
    return [
        HoldoutCase(
            user=user,
            held_out=watch,
            history_before=[h for h in history if h.watched_at < watch.watched_at],
        )
        for watch in eligible
    ]


def replay_case(
    case: HoldoutCase,
    config: EngineConfig,
    *,
    config_label: str,
    tmdb: TmdbClient,
    library_index: dict[MediaType, dict[int, int]],
    resolve_tmdb_id,
    run_year: int = 0,
    curator=None,
    trakt=None,
    search=None,
    web_search_cache: Cache | None = None,
) -> ReplayOutcome:
    """Rebuild the pool from `case.history_before` alone and report where the hidden title landed.

    Composed from the PUBLIC engine functions rather than by calling `rows._candidate_pool`, and that
    is not a stylistic choice. The real pool carries yesterday's row forward — `EngineContext
    .previous_picks`, `rows._reusable_prior`, `_held_for_idle` — so most nights it re-uses roughly
    two thirds of what it already had. Replaying through that would compare two rotation schedules
    while appearing to compare two ranking algorithms.
    """
    key = (case.held_out.tmdb_id, case.held_out.media_type)

    # Seeds, dislikes and the watched-exclusion set ALL come from the same reduced history. If the
    # exclusion set were built from the full history it would contain the held-out title, which
    # `filter_candidates` would then drop as "already_watched" on every single case — a permanent 0%
    # that reads as "the algorithm never works" rather than "the harness is broken".
    disliked = history_mod.disliked_seed_keys(case.history_before, config.dislike_threshold)
    seeds = history_mod.derive_seeds(
        case.history_before,
        resolve_tmdb_id,
        max_seeds=config.max_seeds,
        disliked=disliked,
    )
    # The profile carries the REDUCED history, never the full one: the AI is shown it, and a held-out title in
    # a prompt would hand the model the answer.
    profile = replace(case.user, history=case.history_before)
    taste, favourite_seeds, older_seeds = None, [], []
    wide = (config.favourite_count > 0 or config.older_count > 0) and config.max_seeds != 1
    if "llm_web" in config.candidate_sources:
        ratings = history_mod.ratings_policy(case.history_before, config.dislike_threshold)
        if wide:
            # Dated at the watch being predicted, so the sample and the look-back see the history as it stood.
            taste, favourite_seeds, older_seeds = history_mix(
                case.history_before,
                blocked=profile.blocked_seeds,
                ratings=ratings,
                resolve=resolve_tmdb_id,
                favourites=config.favourite_count,
                older=config.older_count,
                now=case.held_out.watched_at,
                lookback_years=config.older_lookback_years,
            )
        else:
            # Arm A sends what a run sends for a films-and-TV row over every library: the recent list without
            # their blocked and disliked titles. A run scopes it to each row's libraries and media.
            taste = recent_taste(case.history_before, blocked=profile.blocked_seeds, ratings=ratings)
    stats = candidates_mod.GatherStats()
    pool = candidates_mod.gather_candidates(
        tmdb,
        seeds,
        sources=config.candidate_sources,
        curator=curator,
        profile=profile,
        trakt=trakt,
        search=search,
        web_search_mode=config.web_search_provider,
        web_search_cache=web_search_cache,
        recent_count=config.recent_count,
        stats=stats,
        taste=taste,
        favourite_seeds=favourite_seeds,
        favourite_count=config.favourite_count if taste is not None and taste.wide else 0,
        older_seeds=older_seeds,
        older_count=config.older_count if taste is not None and taste.wide else 0,
    )
    gathered = any((c.tmdb_id, c.media_type) == key for c in pool)
    held_title = case.held_out.title.strip().lower()
    found_by_search = any(
        t.title.strip().lower() == held_title and t.media == case.held_out.media_type.value for t in stats.web_found
    )
    found_by_group: dict[str, list[int]] = {}
    seen_in_kind: dict[str, int] = {}
    for kind, titles in stats.web_found_by_search:
        seen_in_kind[kind] = seen_in_kind.get(kind, 0) + 1
        if any(t.title.strip().lower() == held_title and t.media == case.held_out.media_type.value for t in titles):
            found_by_group.setdefault(kind, []).append(seen_in_kind[kind])
    proposals = stats.trace.get("web", {}).get("proposals", [])
    curator_can_complete = curator is not None and getattr(curator, "can_complete", True)
    ai_picked = any((p["tmdb_id"], p["media"]) == (key[0], key[1].value) for p in proposals)

    dropped: list[tuple[Candidate, str]] = []
    watched = {(w.tmdb_id, w.media_type) for w in case.history_before if w.tmdb_id is not None}
    in_library = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched,
        excluded_genres=set(),
        dropped=dropped,
    )
    drop_reason = next((reason for c, reason in dropped if (c.tmdb_id, c.media_type) == key), "")

    ranked = ranking.cut_for_recency(
        in_library,
        [MediaType.MOVIE, MediaType.SHOW],
        config.candidates_pre_rank,
        config.recency,
        run_year,
    )
    rank = next((i + 1 for i, c in enumerate(ranked) if (c.tmdb_id, c.media_type) == key), None)
    final = ranking.diversify_by_seed(ranked, config.row_size)

    return ReplayOutcome(
        case=case,
        config_label=config_label,
        gathered=gathered,
        drop_reason=drop_reason,
        rank=rank,
        pool_size=len(ranked),
        in_final_row=any((c.tmdb_id, c.media_type) == key for c in final),
        reciprocal_rank=(1.0 / rank) if rank else 0.0,
        # The RATINGS, not the items — matching how `history.disliked_seed_keys` calls it.
        ratings_trusted=history_mod.ratings_are_trustworthy(item.user_rating for item in case.history_before),
        found_by_search=found_by_search,
        ai_picked=ai_picked,
        ai_fallback=bool(stats.trace.get("web", {}).get("unpicked")) and curator_can_complete,
        tokens=sum(stats.tokens_by_source.values()),
        new_searches=stats.exa_searches,
        found_by_group=found_by_group,
    )


def summarise(comparison: Comparison) -> str:
    """The aggregate, written so it cannot be mistaken for proof.

    Every line that could be read as a verdict carries the reason it is not one. At this sample size
    the per-case table is the evidence; this is orientation.
    """
    a, b = comparison.outcomes_a, comparison.outcomes_b
    if not a or len(a) != len(b):
        return "No comparable cases — nothing to summarise."

    def rate(rows: list[ReplayOutcome], pick) -> float:
        return sum(pick(r) for r in rows) / len(rows)

    wins = sum(1 for x, y in zip(a, b, strict=True) if y.reciprocal_rank > x.reciprocal_rank)
    losses = sum(1 for x, y in zip(a, b, strict=True) if y.reciprocal_rank < x.reciprocal_rank)
    ties = len(a) - wins - losses
    decided = wins + losses

    lines = [
        f"{len(a)} paired cases · {comparison.label_a} vs {comparison.label_b}",
        f"  candidate recall  {rate(a, lambda r: r.gathered):.2f} -> {rate(b, lambda r: r.gathered):.2f}",
        f"  hit@row_size      {rate(a, lambda r: r.hit):.2f} -> {rate(b, lambda r: r.hit):.2f}",
        f"  MRR               {rate(a, lambda r: r.reciprocal_rank):.3f} -> {rate(b, lambda r: r.reciprocal_rank):.3f}",
        f"  better/worse/same {wins} / {losses} / {ties}",
    ]
    if decided == 0:
        lines.append("  Every case tied — this change did nothing measurable here.")
    else:
        agreement = max(wins, losses) / decided
        direction = "better" if wins > losses else "worse"
        if agreement >= TRUST_THRESHOLD:
            lines.append(
                f"  {agreement:.0%} of decided cases agree it is {direction}. Consistent enough to be "
                f"worth acting on — still read the per-case table for who got worse."
            )
        else:
            lines.append(
                f"  Only {agreement:.0%} of decided cases agree (need {TRUST_THRESHOLD:.0%} at this "
                f"sample size). Treat as noise and read the per-case table."
            )
    return "\n".join(lines)


#: How far a held-out title got, in order. A title that fell out at a stage is reported at the last one it reached.
STAGES = ("not found", "found by search", "AI picked", "kept", "in row")

#: A show first played this long before the watch we hold out was already started, not discovered.
_ALREADY_STARTED = timedelta(days=1)


def first_watch_keep(first_viewed: dict[int, datetime]) -> Callable[[WatchedItem], bool]:
    """A `holdout_cases` filter that keeps discoveries: watches whose title was not started long before.

    Plex's library read stamps a show with its LATEST watch, so a show someone has followed for a year
    looks like a fresh discovery on the night they watch its newest episode. The play log knows when the
    show was first played.

    Args:
        first_viewed: Show rating key -> when the person first played any episode of it.

    Returns:
        A predicate that is False for a show first played more than a day before this watch. Anything with
        no recorded first play is kept: not knowing is not evidence it was started.
    """

    def keep(item: WatchedItem) -> bool:
        first = first_viewed.get(item.rating_key) if item.rating_key is not None else None
        if first is None:
            return True
        return first >= item.watched_at - _ALREADY_STARTED

    return keep


def furthest_stage(outcome: ReplayOutcome) -> str:
    """The last stage of `STAGES` this outcome's held-out title reached."""
    if outcome.in_final_row:
        return STAGES[4]
    if outcome.rank is not None:
        return STAGES[3]
    if outcome.ai_picked:
        return STAGES[2]
    if outcome.found_by_search:
        return STAGES[1]
    return STAGES[0]


def paired_picked(before: list[ReplayOutcome], after: list[ReplayOutcome]) -> tuple[int, int, int]:
    """(better, worse, same) over the same cases, judged on whether the AI picked the held-out title."""
    better = sum(1 for x, y in zip(before, after, strict=True) if y.ai_picked and not x.ai_picked)
    worse = sum(1 for x, y in zip(before, after, strict=True) if x.ai_picked and not y.ai_picked)
    return better, worse, len(before) - better - worse


def summarise_picked(
    label_before: str, label_after: str, before: list[ReplayOutcome], after: list[ReplayOutcome]
) -> str:
    """One paired comparison on "the AI picked it", with the same refusal to call a split a result as `summarise`."""
    if not before or len(before) != len(after):
        return f"{label_before} vs {label_after}: no comparable cases."
    better, worse, same = paired_picked(before, after)
    picked_before = sum(o.ai_picked for o in before)
    picked_after = sum(o.ai_picked for o in after)
    line = (
        f"{label_before} -> {label_after}: AI picked {picked_before} -> {picked_after} of {len(before)}; "
        f"better/worse/same {better} / {worse} / {same}."
    )
    decided = better + worse
    if decided == 0:
        return line + " Every case tied."
    agreement = max(better, worse) / decided
    direction = "better" if better > worse else "worse"
    if agreement >= TRUST_THRESHOLD:
        return line + f" {agreement:.0%} of decided cases agree it is {direction}."
    return line + f" Only {agreement:.0%} agree (need {TRUST_THRESHOLD:.0%}): treat as noise."
