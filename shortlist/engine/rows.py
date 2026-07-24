"""Row construction: turn one user's (or the audience's) history into ranked, delivered picks.

Everything here is the "what goes in the row" half of the engine. The ordering that keeps a row
private — deliver unpromoted, merge filters, promote last — lives in ``pipeline.py``; this module
only builds and delivers collections, always UNPROMOTED.
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from loguru import logger

import shortlist.engine.pipeline as _pipeline
from shortlist.engine import candidates as candidates_mod
from shortlist.engine import picker, ranking
from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.plex_pms import _retry_idempotent
from shortlist.engine.delivery import (
    deliver_rows,
    remove_row,
    render_row_name,
    resolve_row_template,
    row_marker,
    section_kind,
    sections_for_keys,
    target_sections,
)
from shortlist.engine.history import derive_seeds
from shortlist.engine.models import (
    SHARED_SLUG_PREFIX,
    Candidate,
    CollectionDiff,
    EngineConfig,
    MediaType,
    Pick,
    RequestWhy,
    RowSpec,
    UserProfile,
    UserRunReport,
    UserType,
    WatchedItem,
)

if TYPE_CHECKING:
    from shortlist.engine.pipeline import EngineContext


def effective_row_sources(spec: RowSpec, default_sources: list[str]) -> tuple[str, ...]:
    """The candidate sources a row actually gathers from, sorted (so identical sets share one pool).

    A row uses its own ``candidate_sources`` or the global default — the setting is the single source
    of truth for every row, shared or per-person. ``llm_web`` (a live web search + an LLM call) used
    to be hard-dropped from per-person rows on a cost assumption; a head-to-head (2026-07-19) showed it
    surfaces ~22 strong taste matches per person that TMDB-similar misses (Ozark, Succession, True
    Detective, Chernobyl…), so it's now allowed wherever it's configured. It's still gated by
    ``_web_search_capable`` (needs a curator + a search backend), and remains OFF unless it's in the
    sources list — remove it there to control the per-person Exa/LLM cost.
    """
    return tuple(sorted(spec.candidate_sources or default_sources))


def _sections_of(ctx: EngineContext, library_keys: list) -> dict[int, str]:
    """ratingKey -> section key, for the given libraries (all of them when none are pinned).

    Built by inverting the per-section tmdb->ratingKey index the run already holds, so it costs no
    extra Plex reads. Lets a WATCHED title be traced back to the library it lives in.
    """
    wanted = {str(k) for k in library_keys}
    out: dict[int, str] = {}
    for section_key, index in ctx.section_index.items():
        if wanted and str(section_key) not in wanted:
            continue
        for rating_key in index.values():
            out[rating_key] = str(section_key)
    return out


def _history_for_row(ctx: EngineContext, history: list[WatchedItem], spec: RowSpec) -> list[WatchedItem]:
    """The watches that should SEED this row: the ones from the libraries it delivers into.

    A row's libraries used to narrow only what could be delivered, never what was searched — so a
    Movies row on a server whose owner mostly watches sport and TV spent all `max_seeds` slots on
    titles it could never deliver, TMDB returned more of the same, the library intersection threw
    nearly all of it away, and the row came back thin and reported "ok" (issue #1 follow-up).

    Filtering BEFORE `derive_seeds` is what makes the fix work: the seed budget is then filled from
    the relevant watches, looking as far back through the history as it needs to.

    Falls back to the unfiltered history when nothing survives — a weak row beats no row, and that
    is exactly what this person would have got before.
    """
    by_media = _media_filter(history, spec.media)
    if not spec.library_keys:
        return by_media
    sections = _sections_of(ctx, spec.library_keys)
    in_library = [w for w in by_media if w.rating_key is not None and w.rating_key in sections]
    if in_library:
        return in_library
    logger.debug(
        "row '{}': nothing in this person's history comes from its libraries — seeding from all of it",
        spec.slug,
    )
    return by_media


def _media_filter(items: list, media: str) -> list:
    """Keep only items of the row's media type ('both' keeps everything)."""
    if media == "both":
        return list(items)
    kind = MediaType(media)
    return [item for item in items if item.media_type is kind]


# How many episodes watched = the person is clearly watching this show, not discovering it. The
# ``show_pct`` fraction alone is unreachable for a long RETURNING series: it keeps adding episodes,
# so watched/total never hits 80% even after 160 plays (SFLIX/MooHouse Gold Rush 160/226 = 71%, and
# even the owner's own watched count topped out at 173/226; 2026-07-20). A per-show floor catches those.
#
# The floor SCALES with series length rather than being flat. 3 episodes = "given it a real try" for a
# limited series (issue #12: mark-as-watched doesn't appear in play history, only the PMS database sees
# it and reconcile is manual, so plays undercount — a flat 3 was needed to stop in-progress shows
# recurring). But 3 of a 200-episode run is 1.5%, still plainly a discovery. ``_ENGAGED_FRACTION`` lifts
# the floor toward ~15% of length for long shows (200 eps -> 30) while ``_ENGAGED_EPISODES`` holds the
# 3-episode minimum for short ones.
_ENGAGED_EPISODES = 3
_ENGAGED_FRACTION = 0.15


def _engaged_floor(total: int) -> float:
    """Episodes watched at which a show counts as 'engaged, not a fresh pick', scaled to its length."""
    return max(_ENGAGED_EPISODES, total * _ENGAGED_FRACTION)


def _watched_titles(
    watched_movies: set[int],
    show_plays: dict[int, int],
    episode_counts: dict[int, int],
    show_pct: float,
) -> set[tuple[int, MediaType]]:
    """The (tmdb_id, media_type) titles this person has already watched — the ones a watched-cap counts.

    Every watched movie, plus every show they've clearly watched: seen to >= ``show_pct`` of its
    episodes, OR watched a length-scaled "engaged" floor of them (``_engaged_floor``). A returning
    series that keeps airing never reaches the fraction, so the floor is what catches a person 160
    episodes deep; scaling it with length stops 3 episodes of a 200-episode run counting as finished.
    For a short series the ``show_pct`` fraction is the tighter bar, so ``min`` keeps it strict there.
    A show whose episode count is unknown is counted as watched rather than risk re-surfacing one they've
    worked through.
    """
    finished: set[tuple[int, MediaType]] = {(tid, MediaType.MOVIE) for tid in watched_movies}
    for tid, plays in show_plays.items():
        total = episode_counts.get(tid)
        if not total or plays >= min(total * show_pct, _engaged_floor(total)):
            finished.add((tid, MediaType.SHOW))
    return finished


def _apply_watched_cap(
    picks: list[Pick],
    candidates: list[Candidate],
    watched: set[tuple[int, MediaType]],
    k: int,
    pct: float,
) -> list[Pick]:
    """Keep at most ``floor(k * pct)`` already-finished picks; backfill freed slots with fresh ones.

    The row shows unwatched titles first and lets at most ``pct`` of it be things the person has
    already finished. Only used when ``pct`` > 0 — at 0 the pool already excludes finished titles.
    Backfill prefers fresh candidates the curator didn't pick; it re-admits finished ones only if
    the row still can't reach ``k`` and the cap has room.
    """
    max_watched = int(k * pct)  # floor: 20% of a 15-row is 3
    kept: list[Pick] = []
    watched_kept = 0
    for pick in picks:
        if (pick.tmdb_id, pick.media_type) in watched:
            if watched_kept >= max_watched:
                continue  # over the cap — drop, backfill below
            watched_kept += 1
        kept.append(pick)
    if len(kept) < k:
        fresh = [c for c in candidates if (c.tmdb_id, c.media_type) not in watched]
        room = max_watched - watched_kept
        spare_watched = [c for c in candidates if (c.tmdb_id, c.media_type) in watched][: max(0, room)]
        kept = _pad_picks(kept, [*fresh, *spare_watched], k)
    return [replace(p, rank=i + 1) for i, p in enumerate(kept)]


_MAX_REFRESH_PERIOD_DAYS = 14  # freshness just above 0 → refresh about fortnightly
_KEEP_FRACTION = 2 / 3  # on a refresh night, keep the strongest ~two-thirds; swap the weakest third


def _refresh_period_days(freshness: float) -> int:
    """Days between a row's refreshes, from its freshness. 1.0 → 1 (nightly); lower → longer, capped
    at ~a fortnight. Freshness 0.0 is handled by the caller as 'never refresh once built'."""
    f = max(0.0, min(1.0, freshness))
    if f >= 1.0:
        return 1
    return max(1, round(1 + (1 - f) * (_MAX_REFRESH_PERIOD_DAYS - 1)))


def _is_refresh_night(row_slug: str, owner_slug: str, run_day: int, freshness: float) -> bool:
    """Whether this row rebuilds today, vs redelivering last run's picks unchanged.

    Freshness is a CADENCE, not a nightly shuffle: 0.0 = never refresh once built (a frozen, pinned
    row), 1.0 = every night, in between = every N days. A per-(row, owner) phase — a STABLE crc32,
    never Python's per-process-salted ``hash`` — spreads refreshes across the cycle so the whole
    server never re-curates (and re-writes to Plex) on one night. ``run_day <= 0`` (direct engine
    calls and tests, which pass no day) always refreshes, preserving the pre-cadence behaviour.
    """
    if run_day <= 0:
        return True
    if freshness <= 0.0:
        return False
    period = _refresh_period_days(freshness)
    if period <= 1:
        return True
    phase = zlib.crc32(f"{row_slug}|{owner_slug}".encode()) % period
    return run_day % period == phase


def _reusable_prior(
    prior: list[Pick], kind: MediaType, sec_idx: dict[int, int], watched: set[tuple[int, MediaType]], pct: float
) -> list[Pick]:
    """Last run's picks for this library still valid to redeliver, in their original rank order: right
    media type, still in the library, and — for a 0%-watched row — not since finished."""
    out: list[Pick] = []
    for p in prior:
        if p.media_type is not kind or p.tmdb_id not in sec_idx:
            continue
        if pct <= 0 and (p.tmdb_id, p.media_type) in watched:
            continue
        out.append(p)
    return out


def _rating_key_resolver(seed_index: dict[int, int]) -> Callable[[WatchedItem], int | None]:
    """A resolver from a watched item to its tmdb_id, via ratingKey, across EVERY library.

    A user's watches resolve against every library, not just the delivery ones: what they watched in
    a second movie library is still what they watched.

    `seed_index` is keyed by ratingKey for that reason. Inverting a tmdb_id -> ratingKey index here
    instead would silently drop libraries: the same film in "Movies" and "4K Movies" has ONE tmdb id
    and TWO ratingKeys, so only the last library scanned would survive the inversion — and every
    watch in the other one would resolve to nothing, leaving the user seedless and their row empty.
    """

    def resolve(item: WatchedItem) -> int | None:
        return seed_index.get(item.rating_key) if item.rating_key else None

    return resolve


def _stamp_disposition(
    gather_stats: candidates_mod.GatherStats,
    *,
    dropped: list[tuple[Candidate, str]],
    in_library: list[Candidate],
    ranked: list[Candidate],
) -> None:
    """Annotate the gather trace with each candidate's FATE, so the operator can follow every title
    from a source's returns to the row (or to the reason it fell out).

    Reads only the lists selection already produced (``dropped`` from filter_candidates, ``in_library``,
    ``ranked``) — it computes nothing new about which candidates win and mutates none of them. Two
    things are written onto ``gather_stats.trace``:

    * a per-source ``disposition`` tally: ``{kept, already_watched, not_in_your_libraries,
      excluded_genre, lost_ranking_cutoff}`` counts, and
    * a ``fate``/``fate_reason`` on each already-recorded per-seed return, keyed by tmdb_id.

    A candidate that survived filtering but lost the ``candidates_pre_rank`` cut is
    ``lost_ranking_cutoff``; one that made the pre-rank is ``kept`` (whether or not it ends in the
    final row — the per-library row build, downstream of here, decides that and is traced separately
    by the delivered-picks stage).
    """
    ranked_ids = {(c.tmdb_id, c.media_type) for c in ranked}
    in_library_ids = {(c.tmdb_id, c.media_type) for c in in_library}
    drop_reason: dict[tuple[int, MediaType], str] = {}
    for cand, reason in dropped:
        drop_reason.setdefault((cand.tmdb_id, cand.media_type), reason)

    def fate_of(tmdb_id: int, media: MediaType) -> str:
        key = (tmdb_id, media)
        if key in ranked_ids:
            return "kept"
        if key in in_library_ids:
            return "lost_ranking_cutoff"  # survived filtering but lost the pre-rank cut
        # Defensive fallback: a returned id with no matching pooled candidate. Shouldn't occur —
        # every returned title is added to the pool, so it resolves to a real fate above.
        return drop_reason.get(key, "not_returned")

    for source in gather_stats.trace.get("sources", []):
        tally: dict[str, int] = {}
        for query in source.get("queries", []):
            qmedia = MediaType.SHOW if query.get("media") == "show" else MediaType.MOVIE
            for ret in query.get("returned", []):
                verdict = fate_of(int(ret.get("tmdb_id") or 0), qmedia)
                ret["fate"] = verdict
                tally[verdict] = tally.get(verdict, 0) + 1
        if tally:
            source["disposition"] = tally


def row_library_index(
    ctx: EngineContext,
    spec: RowSpec,
    library_index: dict[MediaType, dict[int, int]],
) -> dict[MediaType, dict[int, int]]:
    """What THIS row may recommend: the index of the libraries it actually delivers into.

    An unpinned row keeps the union index (a title in any library of its type is deliverable). A row
    pinned to `library_keys` must be narrowed to those libraries — it was curated against the union,
    so a row pinned to a 200-title "Kids Movies" was choosing from the whole 5000-title movie
    catalogue, and delivery then dropped every pick that library didn't hold: a one-item row, or no
    row at all, reported as ok.
    """
    if not spec.library_keys:
        return library_index
    narrowed: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for section in sections_for_keys(ctx.delivery_sections, spec.library_keys):
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        narrowed[kind].update(ctx.section_index.get(section.key, {}))
    return narrowed


def _candidate_pool(
    ctx: EngineContext,
    seeds: list,
    library_index: dict[MediaType, dict[int, int]],
    *,
    excluded_genres: set[str],
    profile=None,
    sources: list[str] | None = None,
    media: str = "both",
    watched_exclusions: set[tuple[int, MediaType]] | None = None,
    recent_count: int | None = None,
) -> tuple[tuple[list[Candidate], list[Candidate], list[Candidate]], candidates_mod.GatherStats]:
    """Gather TMDB candidates for ``seeds`` and intersect them with the library.

    Returns ``((pool, in_library, ranked), gather_stats)`` — the 3-tuple of candidate lists, plus the
    AI token/Exa spend the gather incurred (for per-run cost accounting):

    * ``pool`` — every pooled candidate (used for request-demand bookkeeping before narrowing).
    * ``in_library`` — the ones the delivery libraries actually hold and this user may still see.
    * ``ranked`` — the pre-ranked candidates the curator chooses from.

    ``media`` narrows the pool BEFORE the pre-rank truncation. Filtering after it meant a
    movie-heavy watcher's shows-only row could lose every show to the 40-candidate cut and deliver
    nothing — a dead row on a green run. Identity is (tmdb_id, media_type), never the bare id — movie
    1399 and TV 1399 are different titles.

    (No staleness partition anymore: rows now carry their prior picks forward on non-refresh nights,
    so there's nothing to "hold back" — see ``_reusable_prior`` / ``_is_refresh_night``.)
    """
    # The titles this person has already watched (per the row's policy), not just the ~30 seeds — a
    # recommendation you've finished is the exact thing the row shouldn't surface. Falls back to the
    # seed set for callers that don't compute the full breakdown (e.g. shared rows).
    watched_ids = watched_exclusions if watched_exclusions is not None else {(s.tmdb_id, s.media_type) for s in seeds}
    # Blocked titles ride along with the watched exclusions — same "don't surface this" machinery —
    # but UNCONDITIONALLY: a watched-cap above 0 re-admits finished titles, and "stop suggesting
    # this" must not be re-admitted by it (issue #5).
    gather_stats = candidates_mod.GatherStats()
    pool = candidates_mod.gather_candidates(
        ctx.tmdb,
        seeds,
        sources=sources if sources is not None else ctx.config.candidate_sources,
        curator=ctx.curator,
        profile=profile,
        trakt=ctx.trakt,
        search=ctx.search,
        web_search_mode=ctx.config.web_search_provider,
        web_search_cache=ctx.web_search_cache,
        recent_count=recent_count if recent_count is not None else ctx.config.recent_count,
        stats=gather_stats,
    )
    # `dropped` collects (candidate, reason) as filter_candidates works — observation only, it does
    # not change which candidates are kept.
    dropped: list[tuple[Candidate, str]] = []
    valid = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched_ids,
        excluded_genres=excluded_genres,
        recent_pick_ids=set(),
        dropped=dropped,
    )
    in_library = _media_filter(valid, media)
    # Pre-rank EACH media type to its own cap, not the mixed pool to one cap — otherwise a 'both'
    # row whose pool skews one way (a mostly-TV watcher) truncates the other type away before the
    # per-media curate ever sees it, and that library's collection comes up empty.
    kinds = [MediaType.MOVIE, MediaType.SHOW] if media == "both" else [MediaType(media)]
    cap = ctx.config.candidates_pre_rank
    ranked = [c for kind in kinds for c in ranking.pre_rank([x for x in in_library if x.media_type is kind], cap)]
    # Stamp each traced return with its fate (kept as a candidate, or dropped and why), derived
    # entirely from the lists selection already produced above — so the trace can follow every title
    # in and out without altering a single delivered pick.
    _stamp_disposition(gather_stats, dropped=dropped, in_library=in_library, ranked=ranked)
    return (pool, in_library, ranked), gather_stats


def _add_step_tokens(report: UserRunReport, step: str, n: int) -> None:
    """Accumulate ``n`` AI tokens under a WHERE-it-went bucket on the user's report (no-op for 0)."""
    if n:
        report.llm_tokens_by_step[step] = report.llm_tokens_by_step.get(step, 0) + n


def _record_gather(report: UserRunReport, stats: candidates_mod.GatherStats, *, pool_label: str | None = None) -> None:
    """Fold a candidate-gather's AI cost into the user report: per-source tokens (also into the grand
    total), Exa searches, and Exa cache hits. Called once per pool COMPUTATION — a cache hit re-adds
    nothing to tokens, but IS counted in exa_cache_hits so the run shows what the cache saved.

    This is the ONLY AI cost now — the AI is used only to FIND titles (web search). Ranking the pool
    and writing each row's reason are done in code (``picker.build_picks``), so there is no per-row
    LLM spend to attribute anymore.

    ``pool_label`` names the pool this gather computed (e.g. "movie · Movies"); its trace is filed
    under ``report.trace["gathers"]`` so the UI can show what each distinct pool queried. Most users
    have a single pool shared by every row, so this is usually one entry.
    """
    for source, tokens in stats.tokens_by_source.items():
        report.llm_tokens += tokens
        _add_step_tokens(report, source, tokens)
    report.exa_searches += stats.exa_searches
    report.exa_cache_hits += stats.exa_cache_hits
    if stats.trace:
        report.trace.setdefault("gathers", []).append({"pool": pool_label or "", **stats.trace})


_TRACE_HISTORY_SAMPLE = 40  # most recent watches to record in the trace (display only — full count is in counts)


def _record_history_trace(
    report: UserRunReport,
    history: list,
    specs: list[RowSpec],
    seeds_for,
    watched_movies: set[int],
    show_plays: dict[int, int],
    library_of_watch=lambda _item: "",
    library_of_seed=lambda _seed: "",
) -> None:
    """File the history/seeds/watched stage of the trace: the most recent watches, the seeds derived
    from them (the widest set any row uses), and a watched summary. Display only.

    ``library_of_watch``/``library_of_seed`` resolve each item to its Plex library's display name so
    the UI can group by real library — a server can have several movie or TV libraries with custom
    names, so grouping by media type alone would be wrong. Both default to "" (unknown), which the UI
    falls back to a media-type label for.
    """
    recent = sorted(history, key=lambda i: i.watched_at, reverse=True)[:_TRACE_HISTORY_SAMPLE]
    seeds = max((seeds_for(spec) for spec in specs), key=len, default=[])
    report.trace["history"] = {
        "total": len(history),
        "recent": [
            {
                "title": i.title,
                "media": i.media_type.value,
                "library": library_of_watch(i),
                "year": i.year,
                "watched_at": i.watched_at.isoformat() if i.watched_at else None,
            }
            for i in recent
        ],
        "watched_movies": len(watched_movies),
        "watched_shows": len(show_plays),
    }
    report.trace["seeds"] = [
        {
            "title": s.title,
            "media": s.media_type.value,
            "library": library_of_seed(s),
            "tmdb_id": s.tmdb_id,
            "weight": round(s.weight, 3),
            # The two ingredients behind the weight, so the UI can say "watched 4x, last seen 3 days
            # ago" instead of an opaque bar — this is what makes the influence bar legible.
            "watch_count": s.watch_count,
            "recency_days": s.recency_days,
        }
        for s in sorted(seeds, key=lambda s: s.weight, reverse=True)
    ]


def _in_audience(user: UserProfile, spec: RowSpec) -> bool:
    return spec.audience is None or user.plex_account_id in spec.audience


def _is_muted(user: UserProfile, spec: RowSpec) -> bool:
    override = user.row_overrides.get(spec.slug)
    return bool(override and override.muted)


def _why_no_rows(user: UserProfile, cfg: EngineConfig) -> str:
    """Plain-English reason this person had no per-person row to build.

    "Skipped" on its own reads as a bug — a beta user turned their only row into a shared row, saw
    every user skipped with no collections created, and filed it as broken (issue #3). The answer is
    always in the configuration, so say which part of it.
    """
    per_person = cfg.per_person_rows()
    if not per_person:
        shared = len(cfg.shared_rows())
        return (
            f"There are no per-person rows to build. Every enabled row ({shared}) is a SHARED row, which is "
            "built once for the whole server from what several people have watched — not per person. "
            "Add a per-person row (Rows → New row) to give people their own."
            if shared
            else "No rows are enabled, so there was nothing to build."
        )
    if not any(_in_audience(user, spec) for spec in per_person):
        return "This person isn't in the audience of any per-person row."
    if all(_is_muted(user, spec) for spec in per_person if _in_audience(user, spec)):
        return "Every per-person row they're in is muted for this person."
    return "None of this person's rows were due to rebuild in this run."


def _remove_muted_and_retired(ctx: EngineContext, user: UserProfile, cfg: EngineConfig, diff: CollectionDiff) -> None:
    """Remove this user's rows that were muted or disabled since the last run.

    A row muted or switched off in the UI is gone from ``cfg.rows``, but its collection still sits on
    this person's Home (excluded from everyone else, so private — just not gone). Removing it makes
    "muted"/"disabled" mean *gone*. This runs before the "no active rows -> return" check so a user
    whose every row was switched off is still cleaned up, and only ever makes the server MORE private,
    so it happens regardless of whether the user has any row this time.
    """
    muted = [s for s in cfg.per_person_rows() if _in_audience(user, s) and _is_muted(user, s)]
    retired = [s for s in cfg.retired_rows if not s.shared and _in_audience(user, s)]
    for spec in (*muted, *retired):
        # write_lock: a Plex mutation (and the collections-cache read/invalidate inside it) must be
        # serialized when users run concurrently — only reads + LLM overlap (Stage 3).
        with ctx.write_lock:
            # Scan EVERY library, not the run's (now targeting-scoped) delivery_sections: a muted row
            # whose library_keys later dropped a library can still have a stale copy there, and a
            # muted row must leave them all. plex.sections() is cached, so this is cheap.
            remove_row(ctx.plex, user, cfg, spec, dry_run=cfg.dry_run, diff=diff, sections=ctx.plex.sections())


def _run_user(
    ctx: EngineContext,
    user: UserProfile,
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
    demand: requests_mod.DemandMap | None = None,
    order_work: list[tuple] | None = None,
) -> bool:
    """Deliver every per-person row this user is in the audience of. Candidates are computed once
    and reused across rows; each row curates and delivers with its own size/media/recipe. Returns
    True when at least one row was delivered (a candidate for promotion).

    When ``demand`` is provided (requests are on), the candidates this user wanted but no delivery
    library holds are folded into it, so the run-wide request pass can ask Sonarr/Radarr for them.
    """
    cfg = ctx.config

    user_report.diff = CollectionDiff()
    _remove_muted_and_retired(ctx, user, cfg, user_report.diff)

    specs = [
        spec
        for spec in cfg.per_person_rows()
        if _in_audience(user, spec) and not _is_muted(user, spec) and cfg.should_build(spec)
    ]
    if not specs:
        # Mark the STATUS too, not just the live event: the pipeline's terminal event said "skipped"
        # while the persisted row kept its default "pending", so a reload showed a user stuck
        # mid-run forever.
        user_report.status = "skipped"
        user_report.reason = _why_no_rows(user, cfg)
        return False
    _pipeline._emit(ctx, user.slug, "history", {})
    user.history = ctx.history_source.fetch(user, min_completion=cfg.min_completion)
    user_report.counts.history = len(user.history)

    cold = len(user.history) < cfg.min_history
    base_cold: list[Pick] = []
    # A candidate pool per DISTINCT effective source-set among this user's rows. Rows that share
    # sources (the common case — every row inheriting the global set) reuse one pool; a row that
    # picks its own sources gets its own. Keyed by the sorted source tuple, memoised across the user.
    Pool = tuple[list[Candidate], list[Candidate], list[Candidate]]
    pool_cache: dict[tuple, Pool] = {}
    pool_failures: dict[tuple, str] = {}  # pool key -> why every source for it failed
    # This person's watched breakdown, filled in the non-cold branch and read by pools_for: watched
    # movie tmdb_ids, and show tmdb_id -> episode-play count (for the finished-show fraction). The
    # derived set of FINISHED (tmdb_id, media_type) titles is computed once the breakdown is in.
    watched_movies: set[int] = set()
    show_plays: dict[int, int] = {}
    watched_titles: set[tuple[int, MediaType]] = set()

    def effective_watched_pct(spec: RowSpec) -> float:
        return spec.watched_pct if spec.watched_pct is not None else cfg.watched_pct

    def effective_freshness(spec: RowSpec) -> float:
        return spec.freshness if spec.freshness is not None else cfg.freshness

    def effective_recent_count(spec: RowSpec) -> int:
        return spec.recent_count if spec.recent_count is not None else cfg.recent_count

    def effective_sources(spec: RowSpec) -> tuple[str, ...]:
        # Sorted so two rows with the same sources in a different order share ONE pool (gather is
        # set-based) — otherwise they'd each rebuild it, re-hitting rate-limited/LLM sources and, for
        # the non-deterministic llm_* sources, possibly diverging despite identical configuration.
        return effective_row_sources(spec, cfg.candidate_sources)

    def pool_key(spec: RowSpec) -> tuple:
        # Sources alone is not enough. A row's media and its libraries both change which candidates
        # survive — and both now narrow the pool BEFORE the pre-rank truncation, so two rows that
        # differ in either must not share a pool. Rows that differ in none of the three (the common
        # case: everything inheriting the defaults) still share exactly one.
        return (
            effective_sources(spec),
            spec.media,
            tuple(sorted(str(k) for k in spec.library_keys)),
            # Only whether the pool hard-excludes finished titles changes the CANDIDATES: a 0% row
            # drops them from the pool; any >0 row keeps them and caps at delivery. Two >0 rows (20%
            # and 50%) share one pool and differ only in their cap, so they must not key apart.
            effective_watched_pct(spec) == 0,
            # recent_count changes how many titles the WEB-SEARCH source searches, so its candidates
            # differ — but only for rows that actually use llm_web. Key on it only then, so two non-web
            # rows differing solely in recent_count still share one pool (no wasted TMDB/curate gather).
            effective_recent_count(spec) if "llm_web" in effective_sources(spec) else 0,
        )

    def pools_for(spec: RowSpec) -> Pool | None:
        """This row's pool, or None when every source it uses is down.

        Per ROW, not per user: a row pinned to a single source (a Trakt-only row while Trakt 502s)
        must not take the person's other rows down with it — those rows have working sources and a
        row they can still fill.
        """
        key = pool_key(spec)
        if key in pool_failures:
            return None
        if key not in pool_cache:
            try:
                pool_cache[key], gather_stats = _candidate_pool(
                    ctx,
                    seeds_for(spec),
                    row_library_index(ctx, spec, library_index),
                    excluded_genres=user.excluded_genres,
                    profile=user,
                    sources=list(key[0]),
                    recent_count=effective_recent_count(spec),
                    media=spec.media,
                    # A 0% row drops finished titles from the pool entirely; a >0 row keeps them (the
                    # per-library cap trims the surplus at delivery). None -> exclude only the seeds.
                    watched_exclusions=watched_titles if effective_watched_pct(spec) == 0 else None,
                )
            except Exception as e:
                pool_failures[key] = f"{type(e).__name__}: {e}"
                logger.warning("{}: row '{}' has no working candidate source ({})", user.username, spec.slug, e)
                return None
            # Once per pool computation (this cache miss) — the gather's AI cost belongs to this user.
            # Label the pool by its media + sources so a multi-pool user's trace stays legible.
            pool_label = f"{spec.media} · {', '.join(key[0])}"
            _record_gather(user_report, gather_stats, pool_label=pool_label)
        return pool_cache[key]

    if cold:
        # Enough picks for the LARGEST row this user is in; each row then takes its own k.
        base_cold = _cold_start_picks(ctx, user, cfg, k=max(spec.size for spec in specs))
        user_report.status = "cold_start"
    else:
        resolve = _rating_key_resolver(seed_index)
        seed_cache: dict[tuple, list] = {}

        def seeds_for(spec: RowSpec) -> list:
            """This row's seeds, from the watches its own libraries hold. Memoised per (media,
            libraries) so rows that target the same thing derive them once."""
            key = (spec.media, tuple(sorted(str(k) for k in spec.library_keys)))
            if key not in seed_cache:
                relevant = _history_for_row(ctx, user.history, spec)
                seed_cache[key] = derive_seeds(relevant, resolve, max_seeds=cfg.max_seeds)
            return seed_cache[key]

        # Reported as the widest seed set any of this person's rows uses — the "both media, every
        # library" case when they have one, so the number still means "how much of their history fed
        # tonight's rows" rather than one arbitrary row's slice.
        user_report.counts.seeds = max((len(seeds_for(spec)) for spec in specs), default=0)
        # Full watched breakdown (not just the seeds): every watched movie, and each show's
        # episode-play count. History is already completion-filtered, so this is meaningful watches.
        for item in user.history:
            tid = item.tmdb_id if item.tmdb_id is not None else resolve(item)
            if tid is None:
                continue
            if item.media_type is MediaType.MOVIE:
                watched_movies.add(tid)
            else:
                show_plays[tid] = show_plays.get(tid, 0) + 1
        # The finished-title set, derived once: read by pools_for (0% hard-exclude) and the per-row
        # watched cap (>0). Mutated in place so the pools_for closure sees it.
        watched_titles |= _watched_titles(watched_movies, show_plays, ctx.episode_counts, cfg.watched_show_pct)
        # Resolve each watch/seed to the display name of the Plex library it lives in, so the trace can
        # group by REAL library (a server can have several movie or TV libraries, custom-named). Both
        # maps are built from data the run already holds — no extra Plex reads.
        section_titles = {str(s.key): getattr(s, "title", "") or "" for s in ctx.delivery_sections}
        rating_key_to_section = _sections_of(ctx, [])  # ratingKey -> section key, across all libraries
        tmdb_to_section = {  # tmdb_id -> section key (first library holding it; good enough for display)
            tmdb_id: str(section_key) for section_key, index in ctx.section_index.items() for tmdb_id in index
        }

        def library_of_watch(item: WatchedItem) -> str:
            return section_titles.get(rating_key_to_section.get(item.rating_key or -1, ""), "")

        def library_of_seed(s) -> str:
            return section_titles.get(tmdb_to_section.get(s.tmdb_id, ""), "")

        _record_history_trace(
            user_report,
            user.history,
            specs,
            seeds_for,
            watched_movies,
            show_plays,
            library_of_watch=library_of_watch,
            library_of_seed=library_of_seed,
        )
        _pipeline._emit(ctx, user.slug, "candidates", {"history": len(user.history), "seeds": user_report.counts.seeds})
        for spec in specs:  # build every row's pool up front so counts and demand see them all
            pools_for(spec)
        # Only if EVERY row's sources are down do we know nothing about this person: that's a failed
        # user, not a quiet "ok" that leaves yesterday's rows in place. One dead row among several
        # is just that one row.
        if pool_failures and not pool_cache:
            raise RuntimeError("; ".join(sorted(pool_failures.values())))
        # Counts are the distinct union across pools (a title in two rows' pools is one candidate).
        user_report.counts.candidates = len({(c.tmdb_id, c.media_type) for p in pool_cache.values() for c in p[0]})
        user_report.counts.in_library = len({(c.tmdb_id, c.media_type) for p in pool_cache.values() for c in p[1]})
        user_report.counts.pre_ranked = len({(c.tmdb_id, c.media_type) for p in pool_cache.values() for c in p[2]})
        # Record what this user wanted that the server doesn't have, for the run-wide request pass.
        # A missing title is attributed to exactly the rows whose pool surfaced it: it gets the user's
        # own request tag plus the tag of each such row. Deduped per user so demand counts them once.
        if demand is not None:
            user_tag = {user.request_tag} if user.request_tag else set()
            first_seen: dict[tuple[int, MediaType], Candidate] = {}
            title_tags: dict[tuple[int, MediaType], set[str]] = {}
            title_why: dict[tuple[int, MediaType], list[RequestWhy]] = {}
            for spec in specs:
                pools = pools_for(spec)
                if pools is None:
                    continue
                # The row's own name (the same one the user sees), so the inbox can say WHICH row a
                # request came from. Fill the placeholders the template may carry.
                row_template = resolve_row_template(spec, user, cfg)
                # A missing title still has a media type, so {library_name} renders as the library that
                # type would land in ("TV Shows" for a missing show). Keyed by media type; the first
                # library of that type wins when the row spans several.
                media_library: dict[MediaType, str] = {}
                for section in target_sections(ctx.delivery_sections, spec):
                    media_library.setdefault(section_kind(section), getattr(section, "title", "") or "")
                for c in requests_mod.collect_missing(pools[0], library_index):
                    key = (c.tmdb_id, c.media_type)
                    first_seen.setdefault(key, c)
                    tags = title_tags.setdefault(key, set())
                    tags |= user_tag  # the user wanted it, whatever the row's media
                    # ...but a row's tag only applies to titles that row could actually show, so a
                    # shows-only row never tags a missing movie (its pool holds both until delivery).
                    if spec.request_tag and spec.media in ("both", c.media_type.value):
                        tags.add(spec.request_tag)
                    # Provenance for the inbox: this row surfaced it for this user, seeded by the
                    # strongest history title behind the candidate ("because you watched …").
                    seed_title = c.top_seed.title if c.top_seed else ""
                    row_name = row_template.replace("{user}", user.display_name).replace(
                        "{top_seed}", seed_title or "your favourites"
                    )
                    # {library_name} renders as the library this title's media type lands in; blank (an
                    # unknown media type) collapses the gap ("✨  Picked for You" -> "✨ Picked for You").
                    if "{library_name}" in row_name:
                        library_name = media_library.get(c.media_type, "")
                        row_name = " ".join(row_name.replace("{library_name}", library_name).split())
                    entry = RequestWhy(
                        user=user.username,
                        row=row_name,
                        seed=seed_title,
                        source=(sorted(c.sources)[0] if c.sources else ""),
                    )
                    why = title_why.setdefault(key, [])
                    if entry not in why:
                        why.append(entry)
            # `demand` is the run-wide shared map; the per-user tally above is local, so only this
            # merge needs the lock (Stage 3 parallel runs).
            with ctx.write_lock:
                for key, cand in first_seen.items():
                    requests_mod.accumulate(
                        demand, [cand], tags=title_tags[key], wanter=user.username, why=title_why[key]
                    )
        user_report.status = "ok"

    if not ctx.plex.sections_by_type():
        raise RuntimeError("no movie or show library found for delivery")

    # One diff and label map for the whole user, accumulated across their rows (already holding any
    # muted-row deletions from above). Handed to delivery rather than returned from it: a row can
    # half-succeed across libraries, and a row that was created and labelled must reach
    # `stored_labels` even if a later write blows up — otherwise nobody's share filter excludes it
    # and it is visible to everyone (the leak we exist to fix).
    all_picks: list[Pick] = []
    delivered_any = False

    for spec in specs:
        # A per-row override lets this one person resize or restyle this one row; each field falls
        # through to the row's own setting when unset. Row beats global — the same direction the
        # name template and the curation recipe resolve in.
        override = user.row_overrides.get(spec.slug)
        k = (override.size if override and override.size else None) or spec.size or cfg.row_size
        # A row runs PER LIBRARY, not per media type: each library it targets gets its own full
        # collection of k, curated from that library's own contents. So a server with two movie
        # libraries (Movies + 4K) gets a full row in EACH, and a mostly-TV watcher still gets a full
        # movie row and a full show row (the "one movie in Picked for You" bug, SFLIX 2026-07-15).
        targets = target_sections(ctx.delivery_sections, spec)
        if not cold:
            # This row's own pool: its sources, its media and its libraries — already narrowed to
            # all three BEFORE the pre-rank truncation, so nothing this row could show was cut by
            # candidates it could never show.
            pools = pools_for(spec)
            if pools is None:
                continue  # every source this row uses is down; its siblings still deliver
            _pool, _in_library, pool_for_row = pools
            _pipeline._emit(ctx, user.slug, "curating", {"candidates": len(pool_for_row)})
        section_picks: dict[str, list[Pick]] = {}
        fresh = effective_freshness(spec)
        for section in targets:
            kind = section_kind(section)
            # tmdb_id -> ratingKey for THIS library only; a candidate not in this library isn't a
            # valid pick for it, however well it ranks for the row overall.
            if cold:
                # Cold picks already come FROM a library (plex.top_rated), so they're in-library by
                # construction; delivery remaps each to the target library and drops any it lacks.
                cands = [p for p in base_cold if p.media_type is kind][:k]
                section_picks[section.key] = [replace(p, rank=i + 1) for i, p in enumerate(cands)]
                continue
            sec_idx = ctx.section_index.get(section.key, {})
            pct = effective_watched_pct(spec)
            sub = [c for c in pool_for_row if c.media_type is kind and c.tmdb_id in sec_idx]
            # str(section.key): previous_picks is keyed by the PickRow.section_key STRING column, so the
            # live section key (which may not be a str) must be coerced or carry-forward silently misses.
            prior_valid = _reusable_prior(
                ctx.previous_picks.get((user.slug, spec.slug, str(section.key)), []), kind, sec_idx, watched_titles, pct
            )
            refresh = _is_refresh_night(spec.slug, user.slug, ctx.run_day, fresh)

            if prior_valid and not refresh:
                # Not this row's refresh night: redeliver last run's picks unchanged — no curator call
                # (saves the tokens), and delivery's unchanged-skip then avoids the Plex write too. Pad
                # only if a title has since left the library, so the row stays full.
                sec_picks = prior_valid[:k]
                if len(sec_picks) < k and sub:
                    sec_picks = _pad_picks(sec_picks, sub, k)
            elif prior_valid:
                # Refresh night: keep the strongest ~two-thirds, swap the rest for genuinely-new titles.
                # Pick only from candidates NOT already in the row so a just-rotated-out title can't
                # bounce straight back — the internal anti-immediate-repeat guard that replaced staleness_runs.
                keep_n = min(len(prior_valid), round(_KEEP_FRACTION * k))
                kept = prior_valid[:keep_n]
                prior_ids = {(p.tmdb_id, p.media_type) for p in prior_valid}
                fresh_pool = [c for c in sub if (c.tmdb_id, c.media_type) not in prior_ids]
                new_picks = picker.build_picks(fresh_pool, k)
                sec_picks = (kept + [p for p in new_picks if (p.tmdb_id, p.media_type) not in prior_ids])[:k]
                if len(sec_picks) < k:
                    sec_picks = _pad_picks(sec_picks, fresh_pool, k)
            else:
                # Bootstrap: this row+library has never been built (or its picks predate row/library
                # stamping) — build a fresh full row, exactly like a first run.
                if not sub:
                    continue
                sec_picks = picker.build_picks(sub, k)
                if len(sec_picks) < k:
                    sec_picks = _pad_picks(sec_picks, sub, k)

            if pct > 0:
                # Let at most `pct` of this library's row be already-finished titles; backfill the
                # rest from its fresh candidates. (At pct == 0 the pool already dropped finished ones.)
                sec_picks = _apply_watched_cap(sec_picks, sub, watched_titles, k, pct)
            section_picks[section.key] = [replace(p, rank=i + 1) for i, p in enumerate(sec_picks[:k])]
            _log_row_provenance(user, spec, section, section_picks[section.key], sub, k)
        # Stamp each pick with the row AND the library it belongs to, so the user page can group picks
        # per row and the effectiveness report can split a multi-library row into one line per library.
        library_names = {section.key: getattr(section, "title", "") or "" for section in targets}
        section_picks = {
            key: [
                replace(p, collection_slug=spec.slug, section_key=key, library=library_names.get(key, "")) for p in sp
            ]
            for key, sp in section_picks.items()
        }
        # Record the exact title delivery will write for EACH library, so the promote phase can apply
        # this row's placement/pin. Per library, because a {top_seed} OR {library_name} title differs
        # library to library. Must match delivery's `render_row_name(..., library_name) + marker` — same
        # section title in, or promote would look for a row delivery never wrote (it'd stay unhidden).
        title_template = resolve_row_template(spec, user, cfg)
        marker = row_marker(user.plex_account_id)
        for section_key, sp in section_picks.items():
            if sp:
                title = render_row_name(title_template, user, sp, library_name=library_names.get(section_key, ""))
                user_report.placement_titles[title + marker] = spec.slug
        picks = [pick for sp in section_picks.values() for pick in sp]
        all_picks.extend(picks)
        _pipeline._emit(ctx, user.slug, "delivering", {"picks": len(picks)})

        # write_lock: the Plex collection writes AND the shared stored_labels mutation inside
        # deliver_rows must be serial across users — the leak-safe half of Stage 3 parallelism.
        # Timed on both sides so a slow run can be split into lock-CONTENTION (waiting behind another
        # user's write) vs real WORK (this user's own PMS calls) — the two look identical in wall-clock
        # otherwise, and only the second is fixable by making the writes cheaper (perf diag 2026-07-19).
        #
        # Delivery is upsert-idempotent (re-reads current membership, re-applies only the delta), so a
        # PMS timeout retries JUST this write, NOT the expensive gather+curate above. Each attempt
        # re-acquires the write-lock and the backoff sleep happens OUTSIDE it, so a stalled user never
        # holds the lock while waiting. This replaced a whole-user retry that re-ran the LLM and a full
        # re-gather on a single Plex hiccup (SFLIX run 3: ~2795s for danvex before it failed, 2026-07-19).
        # A delivery RETRY re-runs deliver_rows for the whole row, which appends one breakdown entry
        # per library — so a mid-row timeout (library 1 delivered, library 2 stalls) would record
        # library 1 twice on the retry. Reset the per-row breakdown to its pre-attempt length on each
        # attempt so the audit stays idempotent too, not just the Plex writes (rule 10). user_report.diff
        # needs no reset: it is None during delivery (only populated from swept rows after _run_user).
        breakdown_mark = len(user_report.breakdown)

        # Default args bind the loop-varying values at definition time (the function is called
        # synchronously by _retry_idempotent, but binding makes that explicit and satisfies B023).
        def _deliver_locked(picks=picks, spec=spec, section_picks=section_picks, mark=breakdown_mark) -> None:
            del user_report.breakdown[mark:]  # drop any entries a prior failed attempt appended
            lock_wait_start = time.monotonic()
            with ctx.write_lock:
                work_start = time.monotonic()
                deliver_rows(
                    ctx.plex,
                    user,
                    picks,
                    cfg,
                    spec,
                    sole_row=len(specs) == 1,
                    dry_run=cfg.dry_run,
                    stored_labels=stored_labels,
                    diff=user_report.diff,
                    sections=ctx.delivery_sections,
                    section_index=ctx.section_index,
                    section_picks=section_picks,
                    breakdown=user_report.breakdown,
                    poster_artist=ctx.poster_artist,
                    order_work=order_work,
                )
                logger.debug(
                    "{}: row '{}' delivery — waited {:.1f}s for write-lock, wrote {} librar(ies) in {:.1f}s",
                    user.username,
                    spec.slug,
                    work_start - lock_wait_start,
                    len(section_picks),
                    time.monotonic() - work_start,
                )

        _retry_idempotent(_deliver_locked, label=f"{user.username} delivery of {spec.slug!r}")
        delivered_any = delivered_any or bool(picks)

    user_report.picks = all_picks
    user_report.counts.picks = len(all_picks)
    if not all_picks:
        logger.warning("{}: no picks produced — existing rows are left as they are", user.username)
    return delivered_any  # nothing delivered -> nothing to promote


def _run_shared(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    report,
    order_work: list[tuple] | None = None,
) -> tuple[UserRunReport, UserProfile | None]:
    """Deliver one shared 'popular on this server' row from AGGREGATE history.

    Owns its own report row and its own error handling, so one shared row failing never stops the
    others and never leaves the run unaudited. Returns ``(user_report, agg)`` — the synthetic
    profile is a promotion candidate when a row was delivered, else None.
    """
    started = time.monotonic()
    slug = f"{SHARED_SLUG_PREFIX}_{spec.slug}"
    user_report = UserRunReport(username=f"Shared · {spec.slug}", slug=slug)
    report.users.append(user_report)
    try:
        agg = _shared_row(ctx, spec, users, seed_index, library_index, stored_labels, user_report, slug, order_work)
    except Exception as e:  # one shared row's failure never stops the next (rule 6 resume-safety)
        user_report.status = "error"
        user_report.error = f"{type(e).__name__}: {e}"
        logger.exception("shared row '{}': failed", spec.slug)
        agg = None
    finally:
        user_report.duration_s = round(time.monotonic() - started, 2)
        # A shared row has no per-user terminal event, so a skip left the activity feed showing it
        # mid-flight forever and its reason nowhere on screen (issue #3). Emit its outcome like any
        # other participant in the run.
        if user_report.status == "skipped":
            _pipeline._emit(ctx, slug, "skipped", {}, user_report.reason)
    return user_report, agg


def _shared_row(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
    slug: str,
    order_work: list[tuple] | None = None,
) -> UserProfile | None:
    """Build and deliver the shared row's picks (the body ``_run_shared`` guards).

    A title only qualifies once at least ``spec.min_watchers`` distinct people in the audience have
    watched it, so no single person's viewing can reach a public row. Reasons are aggregate-framed —
    never "because you watched X", since there is no single "you".
    """
    cfg = ctx.config
    audience = [u for u in users if spec.audience is None or u.plex_account_id in spec.audience]
    if not audience:
        user_report.status = "skipped"
        user_report.reason = "Nobody in this row's audience is enabled, so there was no history to build it from."
        return None

    base_resolve = _rating_key_resolver(seed_index)

    def resolve(item: WatchedItem) -> int | None:
        return item.tmdb_id or base_resolve(item)

    # Count DISTINCT watchers per title across the audience; keep only titles enough people watched.
    watchers: dict[tuple[int, MediaType], set[int]] = {}
    example: dict[tuple[int, MediaType], WatchedItem] = {}
    for user in audience:
        # Reuse the history _run_user already fetched (same min_completion) rather than re-fetching
        # it per shared row — that was S*A redundant Tautulli/PMS calls. Fall back to a fetch only
        # when it's empty (a user with genuinely none, or whose per-user pass errored before fetching).
        user_history = user.history or ctx.history_source.fetch(user, min_completion=cfg.min_completion)
        for item in user_history:
            tmdb_id = resolve(item)
            if tmdb_id is None:
                continue
            key = (tmdb_id, item.media_type)
            watchers.setdefault(key, set()).add(user.plex_account_id)
            example.setdefault(key, item)
    # Hard floor of 2, regardless of config: a public row must never be shaped by one person's
    # viewing, so a title needs at least two distinct watchers even if the row was set to 1.
    threshold = max(2, spec.min_watchers)
    agg_history = [example[key] for key, who in watchers.items() if len(who) >= threshold]
    user_report.counts.history = len(agg_history)

    agg = UserProfile(
        username="Everyone",
        plex_account_id=0,
        user_type=UserType.SHARED,
        slug=slug,
        history=agg_history,
    )
    if not agg_history:
        user_report.status = "skipped"
        # The commonest cause by far is a shared row whose audience is smaller than the floor, where
        # it is arithmetically unreachable — so say which it is rather than leaving someone to
        # conclude the app is broken (issue #3).
        #
        # "in this row's audience" is the honest phrase, NOT "enabled": `users` has already been
        # narrowed to people who are enabled AND not paused, then narrowed again by spec.audience.
        # Saying "only 1 user is enabled" to someone looking at ten enabled users on the Users page
        # is exactly the kind of confidently-wrong explanation that sends them back to the tracker.
        who = f"{len(audience)} {'person' if len(audience) == 1 else 'people'}"
        user_report.reason = (
            f"No title has been watched by {threshold} or more of the {who} in this row's audience yet. "
            f"A shared row is built only from titles several people have watched, so it needs {threshold} "
            f"of them with some viewing in common."
            if len(audience) >= threshold
            else f"A shared row needs at least {threshold} people with overlapping viewing, but only {who} "
            f"{'is' if len(audience) == 1 else 'are'} in this row's audience and active in runs (enabled, "
            f"not paused) — so it can never build. Add more people to the audience, or make this a "
            f"per-person row so each of them gets their own."
        )
        logger.info("shared row '{}': no title watched by >= {} people yet", spec.slug, threshold)
        return None

    seeds = derive_seeds(agg_history, resolve, max_seeds=cfg.max_seeds)
    row_sources = spec.candidate_sources if spec.candidate_sources else None  # None -> global default
    # Same three narrowings a per-person row gets: its sources, its media, its libraries.
    (_pool, _in_library, ranked), gather_stats = _candidate_pool(
        ctx,
        seeds,
        row_library_index(ctx, spec, library_index),
        excluded_genres=set(),
        profile=agg,
        sources=row_sources,
        media=spec.media,
        recent_count=spec.recent_count if spec.recent_count is not None else cfg.recent_count,
    )
    _record_gather(user_report, gather_stats)  # shared-row gather AI cost (llm_web + Exa)
    k = spec.size
    # Build PER LIBRARY, exactly like a per-person row: each targeted library gets its own full k
    # from its own contents. One mixed pool over a now media-segregated pool would let a 'both'
    # shared row come back all-movies-no-shows.
    targets = target_sections(ctx.delivery_sections, spec)
    section_picks: dict[str, list[Pick]] = {}
    for section in targets:
        kind = section_kind(section)
        sec_idx = ctx.section_index.get(section.key, {})
        sub = [c for c in ranked if c.media_type is kind and c.tmdb_id in sec_idx]
        if not sub:
            continue
        # NOTE: shared-row picks aren't persisted per-user (they file under `shared_<slug>`), so they
        # can't carry forward like per-person rows yet — they rebuild each run. They're few (1-2) and
        # aggregate history changes slowly, so churn here is minor. See [[perf-work-state]] follow-up.
        sec_picks = picker.build_picks(sub, k)
        if len(sec_picks) < k:
            # Backfill from this library's ranked pool so a thin build never SHRINKS the row.
            sec_picks = _pad_picks(sec_picks, sub, k)
        section_picks[section.key] = sec_picks
    # Force aggregate framing: a shared row is nobody's "because you watched", and the seed is
    # dropped so a {top_seed} name template can never surface one person's title.
    # Stamp the library too, so a shared row spanning >1 library splits per library in the report.
    library_names = {section.key: getattr(section, "title", "") or "" for section in targets}
    section_picks = {
        key: [
            replace(
                p,
                reason="Popular on this server",
                seed_title=None,
                seed_tmdb_id=None,
                collection_slug=spec.slug,
                section_key=key,
                library=library_names.get(key, ""),
            )
            for p in sp
        ]
        for key, sp in section_picks.items()
    }
    picks = [pick for sp in section_picks.values() for pick in sp]

    user_report.picks = picks
    user_report.counts.picks = len(picks)
    user_report.status = "ok"
    user_report.diff = CollectionDiff()
    _pipeline._emit(ctx, slug, "delivering", {"picks": len(picks)})
    deliver_rows(
        ctx.plex,
        agg,
        picks,
        cfg,
        spec,
        sole_row=True,  # one shared row per label
        dry_run=cfg.dry_run,
        stored_labels=stored_labels,
        diff=user_report.diff,
        sections=ctx.delivery_sections,
        section_index=ctx.section_index,
        section_picks=section_picks,
        breakdown=user_report.breakdown,
        order_work=order_work,
    )
    return agg if picks else None


# Below this, a title is in the pool because TMDB mentioned it somewhere near the bottom of a list,
# not because it resembles anything the person watched. A row of four genuinely-similar titles is
# worth more than ten where six are filler — and filler is what a beta user saw when a medical drama
# produced The Sandman, Servant and Torchwood, each captioned "Because you watched The Pitt".
# Sources with no ranking of their own sit at the neutral 1.0 and so are never filtered out here.
MIN_FILLER_AFFINITY = 0.35


def _log_row_provenance(
    user: UserProfile,
    spec: RowSpec,
    section,
    picks: list[Pick],
    pool: list[Candidate],
    wanted: int,
) -> None:
    """Explain a finished row in the log: what went in, and what was rejected as too loose.

    A beta user reported a medical-drama row full of fantasy, and answering "why?" meant querying
    TMDB by hand — nothing in the log said where any pick came from or how strong the claim was. One
    DEBUG block per row makes the same question answerable from a downloaded log.
    """
    label = f"{user.username}/{spec.slug}@{getattr(section, 'title', '?')}"
    if not picks:
        logger.debug("{}: no picks — {} candidates, none worth delivering", label, len(pool))
        return
    logger.debug("{}: {} picks from {} candidates (row size {})", label, len(picks), len(pool), wanted)
    for pick in picks:
        logger.debug(
            "  #{} {} — {} · {} · affinity {:.2f}",
            pick.rank,
            pick.title,
            f"seed {pick.seed_title}" if pick.seed_title else "no seed",
            "+".join(pick.sources) or "source not recorded",
            pick.affinity,
        )
    if len(picks) < wanted:
        # The row is deliberately short: `_pad_picks` refused to fill it from the tail. Say so, or
        # it reads as a bug — a short row is the fix working, not the pipeline failing.
        too_loose = [c for c in pool if c.affinity < MIN_FILLER_AFFINITY]
        logger.info(
            "{}: row is {} short of {} — {} candidate(s) were too loosely related to deliver{}",
            label,
            wanted - len(picks),
            wanted,
            len(too_loose),
            f" (closest rejected: {max(too_loose, key=lambda c: c.affinity).title})" if too_loose else "",
        )


def _pad_picks(picks: list[Pick], ranked: list[Candidate], k: int) -> list[Pick]:
    """Top up a short row from the ranked pool (never invents titles).

    Only from candidates whose source actually vouched for them: padding is where a weak association
    turns into a delivered row, so the row is allowed to come up short instead.
    """
    have = {(p.tmdb_id, p.media_type) for p in picks}  # movie 1399 and TV 1399 are different titles
    worth_it = [c for c in ranked if c.affinity >= MIN_FILLER_AFFINITY]
    if len(worth_it) < len(ranked):
        logger.debug(
            "padding: {} of {} candidates were too loosely related to deliver",
            len(ranked) - len(worth_it),
            len(ranked),
        )
    fillers = picker.build_picks([c for c in worth_it if (c.tmdb_id, c.media_type) not in have], k - len(picks))
    out = list(picks)
    for f in fillers:
        out.append(Pick(**{**f.__dict__, "rank": len(out) + 1}))
    return out


def _cold_start_picks(ctx: EngineContext, user: UserProfile, cfg: EngineConfig, k: int = 0) -> list[Pick]:
    """ "Popular on <server>" fallback for a user with thin history: top-rated titles.

    Every library gets a share, not just movies — a movies-only cold start would hand delivery a
    pick list with no shows in it, and a thin-history night (a Tautulli outage is enough) would
    then leave a TV watcher with a row of films they never asked for.
    """
    sections = ctx.plex.sections_by_type()
    if not sections:
        return []
    k = k if k else cfg.row_size
    share = max(1, k // len(sections))

    picks: list[Pick] = []
    for index, (kind, section) in enumerate(sections.items()):
        # The last library takes the remainder, so `row_size` titles are delivered, not k - k % n.
        wanted = k - len(picks) if index == len(sections) - 1 else min(share, k - len(picks))
        if wanted <= 0:
            break
        for tmdb_id, item in ctx.plex.top_rated(section, wanted):
            picks.append(
                Pick(
                    tmdb_id=tmdb_id,
                    rating_key=item.ratingKey,
                    title=item.title,
                    rank=len(picks) + 1,
                    reason="Popular on this server",
                    media_type=kind,
                    sources=["cold_start"],  # no history to work from — say so rather than imply a match
                )
            )
    return picks
