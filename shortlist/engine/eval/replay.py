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

from dataclasses import dataclass, field

from shortlist.engine import candidates as candidates_mod
from shortlist.engine import history as history_mod
from shortlist.engine import ranking
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import Candidate, EngineConfig, MediaType, UserProfile, WatchedItem

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
) -> list[HoldoutCase]:
    """Up to `max_holdouts` of this person's most recent watches, each with the history before it.

    Each case slices the history independently at its own timestamp rather than removing one item
    from the whole list. Removing one item leaves every LATER watch in place, so testing a person's
    five most recent watches would leak four of them into the earliest case's seeds and quietly
    inflate every number.

    No completion filter: `WatchCache` applies `min_completion` when it stores a watch, so anything
    reaching this function already qualified.
    """
    eligible = sorted(
        (w for w in history if w.tmdb_id is not None),
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
    pool = candidates_mod.gather_candidates(
        tmdb,
        seeds,
        sources=config.candidate_sources,
        curator=curator,
        trakt=trakt,
        search=search,
        recent_count=config.recent_count,
    )
    gathered = any((c.tmdb_id, c.media_type) == key for c in pool)

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
