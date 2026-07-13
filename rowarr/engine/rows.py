"""Row construction: turn one user's (or the audience's) history into ranked, delivered picks.

Everything here is the "what goes in the row" half of the engine. The ordering that keeps a row
private — deliver unpromoted, merge filters, promote last — lives in ``pipeline.py``; this module
only builds and delivers collections, always UNPROMOTED.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from loguru import logger

import rowarr.engine.pipeline as _pipeline
from rowarr.engine import candidates as candidates_mod
from rowarr.engine import ranking
from rowarr.engine import requests as requests_mod
from rowarr.engine.curator import CuratorError, NullCurator
from rowarr.engine.delivery import deliver_rows, remove_row
from rowarr.engine.history import derive_seeds
from rowarr.engine.models import (
    SHARED_SLUG_PREFIX,
    Candidate,
    CollectionDiff,
    EngineConfig,
    MediaType,
    Pick,
    PromptConfig,
    RowSpec,
    UserProfile,
    UserRunReport,
    UserType,
    WatchedItem,
)

if TYPE_CHECKING:
    from rowarr.engine.pipeline import EngineContext


def _media_filter(items: list, media: str) -> list:
    """Keep only items of the row's media type ('both' keeps everything)."""
    if media == "both":
        return list(items)
    kind = MediaType(media)
    return [item for item in items if item.media_type is kind]


def _rating_key_resolver(seed_index: dict[MediaType, dict[int, int]]) -> Callable[[WatchedItem], int | None]:
    """A resolver from a watched item to its tmdb_id, via ratingKey, across EVERY library.

    A user's watches resolve against every library (seed_index), not just the delivery ones: what
    they watched in a second movie library is still what they watched.
    """
    by_rating_key = {key: tmdb_id for idx in seed_index.values() for tmdb_id, key in idx.items()}

    def resolve(item: WatchedItem) -> int | None:
        return by_rating_key.get(item.rating_key) if item.rating_key else None

    return resolve


def _candidate_pool(
    ctx: EngineContext,
    seeds: list,
    library_index: dict[MediaType, dict[int, int]],
    *,
    excluded_genres: set[str],
    recent: set[tuple[int, MediaType]],
) -> tuple[list[Candidate], list[Candidate], list[Candidate], list[Candidate]]:
    """Gather TMDB candidates for ``seeds``, intersect with the library, split by staleness.

    Returns ``(pool, in_library, ranked, held_back)``:

    * ``pool`` — every pooled candidate (used for request-demand bookkeeping before narrowing).
    * ``in_library`` — the ones the delivery libraries actually hold and this user may still see.
    * ``ranked`` — the pre-ranked fresh candidates the curator chooses from.
    * ``held_back`` — pre-ranked titles the staleness guard held back (recommended in the last N
      runs). Still valid recommendations, so they backfill a row fresh candidates can't fill —
      without them a thin pool SHRINKS the row rather than repeating a title.

    One ``filter_candidates`` pass, not two: the valid set is partitioned by ``recent``. Identity
    is (tmdb_id, media_type), never the bare id — movie 1399 and TV 1399 are different titles.
    """
    watched_ids = {(s.tmdb_id, s.media_type) for s in seeds}
    pool = candidates_mod.gather_candidates(ctx.tmdb, seeds)
    valid = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched_ids,
        excluded_genres=excluded_genres,
        recent_pick_ids=set(),
    )
    in_library = [c for c in valid if (c.tmdb_id, c.media_type) not in recent]
    held = [c for c in valid if (c.tmdb_id, c.media_type) in recent]
    ranked = ranking.pre_rank(in_library, ctx.config.candidates_pre_rank)
    held_back = ranking.pre_rank(held, ctx.config.candidates_pre_rank)
    return pool, in_library, ranked, held_back


def _run_user(
    ctx: EngineContext,
    user: UserProfile,
    seed_index: dict[MediaType, dict[int, int]],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
    demand: requests_mod.DemandMap | None = None,
) -> bool:
    """Deliver every per-person row this user is in the audience of. Candidates are computed once
    and reused across rows; each row curates and delivers with its own size/media/recipe. Returns
    True when at least one row was delivered (a candidate for promotion).

    When ``demand`` is provided (requests are on), the candidates this user wanted but no delivery
    library holds are folded into it, so the run-wide request pass can ask Sonarr/Radarr for them.
    """
    cfg = ctx.config

    def in_audience(spec: RowSpec) -> bool:
        return spec.audience is None or user.plex_account_id in spec.audience

    def is_muted(spec: RowSpec) -> bool:
        override = user.row_overrides.get(spec.slug)
        return bool(override and override.muted)

    # A row muted AFTER it was delivered still exists on the server — remove it before anything else,
    # so "muted" really means "gone", not merely "not refreshed". This only ever makes the server
    # more private, so it runs regardless of whether the user has any other rows this time.
    user_report.diff = CollectionDiff()
    for spec in cfg.per_person_rows():
        if in_audience(spec) and is_muted(spec):
            remove_row(ctx.plex, user, cfg, spec, dry_run=cfg.dry_run, diff=user_report.diff)

    specs = [spec for spec in cfg.per_person_rows() if in_audience(spec) and not is_muted(spec)]
    if not specs:
        return False  # this user is in no per-person row (none in audience, or all muted)
    # The adapter puts the Phase-A global+per-user recipe on the profile; a row with its own recipe
    # overrides it for that row only.
    base_prompt = user.prompt

    _pipeline._emit(ctx, user.slug, "history", {})
    user.history = ctx.history_source.fetch(user, min_completion=cfg.min_completion)
    user_report.counts.history = len(user.history)

    cold = len(user.history) < cfg.min_history
    ranked: list[Candidate] = []
    held_back: list[Candidate] = []
    base_cold: list[Pick] = []
    if cold:
        base_cold = _cold_start_picks(ctx, user, cfg)
        user_report.status = "cold_start"
    else:
        resolve = _rating_key_resolver(seed_index)
        seeds = derive_seeds(user.history, resolve, max_seeds=cfg.max_seeds)
        user_report.counts.seeds = len(seeds)
        _pipeline._emit(ctx, user.slug, "candidates", {"history": len(user.history), "seeds": len(seeds)})
        recent = ctx.recent_picks.get(user.slug, set())
        pool, in_library, ranked, held_back = _candidate_pool(
            ctx, seeds, library_index, excluded_genres=user.excluded_genres, recent=recent
        )
        user_report.counts.candidates = len(pool)
        # Record what this user wanted that the server doesn't have, for the run-wide request pass.
        # Done here, off the FULL pool, before pool is narrowed per-row below.
        if demand is not None:
            requests_mod.accumulate(demand, requests_mod.collect_missing(pool, library_index))
        user_report.counts.in_library = len(in_library)
        user_report.counts.pre_ranked = len(ranked)
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
        # through to the row's own setting, then the user-wide default, when unset.
        override = user.row_overrides.get(spec.slug)
        k = (override.size if override and override.size else None) or user.row_size or spec.size
        if cold:
            picks = [
                Pick(**{**pick.__dict__, "rank": i + 1})
                for i, pick in enumerate(_media_filter(base_cold, spec.media)[:k])
            ]
        else:
            row_prompt = (override.prompt if override and override.prompt else None) or spec.prompt
            effective_prompt = row_prompt if row_prompt is not None else base_prompt
            # A per-row copy carries the effective recipe to the curator; the real profile is never
            # mutated, so one row's recipe can't leak into the next row (or into delivery below).
            row_profile = _with_prompt(user, effective_prompt)
            pool_for_row = _media_filter(ranked, spec.media)
            _pipeline._emit(ctx, user.slug, "curating", {"candidates": len(pool_for_row)})
            try:
                picks = ctx.curator.curate(row_profile, pool_for_row, k)
                user_report.llm_tokens += getattr(ctx.curator, "last_tokens", 0)
            except CuratorError as e:
                logger.warning("{}: curator failed ({}); degrading to heuristic mode", user.username, e)
                picks = NullCurator().curate(row_profile, pool_for_row, k)
            if len(picks) < k:
                picks = _pad_picks(picks, pool_for_row + _media_filter(held_back, spec.media), k)
        # Stamp each pick with the row it belongs to, so the user page can group picks per row.
        picks = [Pick(**{**pick.__dict__, "collection_slug": spec.slug}) for pick in picks]
        all_picks.extend(picks)
        _pipeline._emit(ctx, user.slug, "delivering", {"picks": len(picks)})
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
        )
        delivered_any = delivered_any or bool(picks)

    user_report.picks = all_picks
    user_report.counts.picks = len(all_picks)
    if not all_picks:
        logger.warning("{}: no picks produced — existing rows are left as they are", user.username)
    return delivered_any  # nothing delivered -> nothing to promote


def _with_prompt(user: UserProfile, prompt: PromptConfig | None) -> UserProfile:
    """A shallow copy of the profile carrying ``prompt`` — used to curate one row without mutating
    the shared profile (its history/genres/overrides are read-only during curation)."""
    return replace(user, prompt=prompt)


def _run_shared(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[MediaType, dict[int, int]],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    report,
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
        agg = _shared_row(ctx, spec, users, seed_index, library_index, stored_labels, user_report, slug)
    except Exception as e:  # one shared row's failure never stops the next (rule 6 resume-safety)
        user_report.status = "error"
        user_report.error = f"{type(e).__name__}: {e}"
        logger.exception("shared row '{}': failed", spec.slug)
        agg = None
    finally:
        user_report.duration_s = round(time.monotonic() - started, 2)
    return user_report, agg


def _shared_row(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[MediaType, dict[int, int]],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
    slug: str,
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
        return None

    base_resolve = _rating_key_resolver(seed_index)

    def resolve(item: WatchedItem) -> int | None:
        return item.tmdb_id or base_resolve(item)

    # Count DISTINCT watchers per title across the audience; keep only titles enough people watched.
    watchers: dict[tuple[int, MediaType], set[int]] = {}
    example: dict[tuple[int, MediaType], WatchedItem] = {}
    for user in audience:
        for item in ctx.history_source.fetch(user, min_completion=cfg.min_completion):
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
        prompt=spec.prompt,
    )
    if not agg_history:
        user_report.status = "skipped"
        logger.info("shared row '{}': no title watched by >= {} people yet", spec.slug, threshold)
        return None

    seeds = derive_seeds(agg_history, resolve, max_seeds=cfg.max_seeds)
    _pool, _in_library, ranked_all, _held_back = _candidate_pool(
        ctx, seeds, library_index, excluded_genres=set(), recent=ctx.recent_picks.get(slug, set())
    )
    ranked = _media_filter(ranked_all, spec.media)
    k = spec.size
    try:
        picks = ctx.curator.curate(agg, ranked, k)
    except CuratorError:
        picks = NullCurator().curate(agg, ranked, k)
    if len(picks) < k:
        picks = _pad_picks(picks, ranked, k)
    # Force aggregate framing regardless of curator: a shared row is nobody's "because you watched",
    # and the seed is dropped so a {top_seed} name template can never surface one person's title.
    picks = [
        Pick(
            **{
                **pick.__dict__,
                "reason": "Popular on this server",
                "seed_title": None,
                "seed_tmdb_id": None,
                "collection_slug": spec.slug,
            }
        )
        for pick in picks
    ]

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
    )
    return agg if picks else None


def _pad_picks(picks: list[Pick], ranked: list[Candidate], k: int) -> list[Pick]:
    """Top up short curator output from the heuristic order (never invents titles)."""
    have = {(p.tmdb_id, p.media_type) for p in picks}  # movie 1399 and TV 1399 are different titles
    fillers = NullCurator().curate(
        UserProfile(username="", plex_account_id=0, user_type=UserType.SHARED),
        [c for c in ranked if (c.tmdb_id, c.media_type) not in have],
        k - len(picks),
    )
    out = list(picks)
    for f in fillers:
        out.append(Pick(**{**f.__dict__, "rank": len(out) + 1}))
    return out


def _cold_start_picks(ctx: EngineContext, user: UserProfile, cfg: EngineConfig) -> list[Pick]:
    """ "Popular on <server>" fallback for a user with thin history: top-rated titles.

    Every library gets a share, not just movies — a movies-only cold start would hand delivery a
    pick list with no shows in it, and a thin-history night (a Tautulli outage is enough) would
    then leave a TV watcher with a row of films they never asked for.
    """
    sections = ctx.plex.sections_by_type()
    if not sections:
        return []
    k = user.row_size or cfg.row_size
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
                )
            )
    return picks
