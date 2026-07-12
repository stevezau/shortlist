"""Per-user pipeline orchestration: history → candidates → filter → rank → curate → deliver → privacy."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger

from rowarr.engine import candidates as candidates_mod
from rowarr.engine import ranking
from rowarr.engine.clients.plex import PlexClient, PlexTvClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import Curator, CuratorError, NullCurator
from rowarr.engine.delivery import deliver_row
from rowarr.engine.history import HistorySource, derive_seeds
from rowarr.engine.models import (
    Candidate,
    EngineConfig,
    MediaType,
    Pick,
    RunReport,
    UserProfile,
    UserRunReport,
    UserType,
)
from rowarr.engine.privacy import SnapshotStore, sync_user_restrictions


@dataclass
class EngineContext:
    """Everything one run needs; adapters (CLI/server) build this once."""

    config: EngineConfig
    plex: PlexClient
    plextv: PlexTvClient
    tmdb: TmdbClient
    history_source: HistorySource
    curator: Curator
    snapshots: SnapshotStore
    recent_picks: dict[str, set[int]] = field(default_factory=dict)  # slug -> tmdb_ids (staleness guard)


def run(ctx: EngineContext, users: list[UserProfile]) -> RunReport:
    """Run the pipeline for every enabled user. Users are independent — one failure never
    stops the run (per-user try/except; plex-safety rule 6 resume-safety).

    Write ordering is leak-safe: rows are created/updated UNPROMOTED, then every user's
    share filters are merged, and only then are rows promoted onto shared Home — so a new
    collection is never visible to anyone before the exclusions that hide it exist.
    """
    report = RunReport(started_at=datetime.now(UTC), dry_run=ctx.config.dry_run)

    sections = ctx.plex.sections()
    library_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for section in sections:
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        library_index[kind].update(ctx.plex.build_library_index(section))
    movie_section = next((s for s in sections if s.type == "movie"), None)

    # Preload label casing + collection ids from the PMS — the source of truth survives
    # restarts and covers users whose delivery fails this run.
    stored_labels = {slug: label for slug, (label, _) in ctx.plex.owned_collections(ctx.config.label_prefix).items()}

    to_promote: list[UserProfile] = []
    for user in users:
        user_report = UserRunReport(username=user.username, slug=user.slug)
        report.users.append(user_report)
        started = time.monotonic()
        try:
            delivered = _run_user(ctx, user, users, library_index, movie_section, stored_labels, user_report)
            if delivered:
                to_promote.append(user)
        except Exception as e:
            user_report.status = "error"
            user_report.error = f"{type(e).__name__}: {e}"
            logger.exception("{}: pipeline failed", user.username)
        finally:
            user_report.duration_s = round(time.monotonic() - started, 2)

    # Privacy sync runs for EVERY user — delivery failure doesn't exempt a user from
    # excluding rows that already exist or were just created.
    sync_failed = False
    for user in users:
        user_report = next(r for r in report.users if r.slug == user.slug)
        try:
            user_report.privacy_synced = sync_user_restrictions(
                ctx.plextv,
                user,
                users,
                stored_labels,
                ctx.snapshots,
                label_prefix=ctx.config.label_prefix,
                dry_run=ctx.config.dry_run,
            )
        except Exception as e:
            sync_failed = True
            user_report.status = "error"
            user_report.error = (user_report.error or "") + f" | privacy sync: {type(e).__name__}: {e}"
            logger.exception("{}: privacy sync failed", user.username)

    filters_ok = not sync_failed
    for user in to_promote:
        user_report = next(r for r in report.users if r.slug == user.slug)
        if ctx.config.dry_run:
            logger.info("[dry-run] {}: would promote row to shared Home", user.username)
            continue
        if not filters_ok:
            logger.warning("{}: promotion skipped — a privacy sync failed this run", user.username)
            continue
        try:
            collection = ctx.plex.find_owned_collection(movie_section, ctx.config.label_prefix, user.slug)
            if collection is not None:
                ctx.plex.promote(collection, shared=True)
        except Exception as e:
            user_report.status = "error"
            user_report.error = (user_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("{}: promote failed", user.username)

    report.finished_at = datetime.now(UTC)
    ok = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    logger.info("run complete: {}/{} users ok (dry_run={})", ok, len(report.users), ctx.config.dry_run)
    return report


def _run_user(
    ctx: EngineContext,
    user: UserProfile,
    all_users: list[UserProfile],
    library_index: dict[MediaType, dict[int, int]],
    movie_section,
    stored_labels: dict[str, str],
    user_report: UserRunReport,
) -> bool:
    """Run stages for one user; returns True when a row was delivered (candidate for promotion)."""
    cfg = ctx.config
    user.history = ctx.history_source.fetch(user, min_completion=cfg.min_completion)
    user_report.counts.history = len(user.history)

    if len(user.history) < cfg.min_history:
        picks = _cold_start_picks(ctx, user, library_index, cfg)
        user_report.status = "cold_start"
    else:

        def resolve(item):
            # Resolve a watched title to its TMDB id via the library's own metadata.
            if item.rating_key and any(item.rating_key in idx.values() for idx in library_index.values()):
                for idx in library_index.values():
                    for tmdb_id, key in idx.items():
                        if key == item.rating_key:
                            return tmdb_id
            return None

        seeds = derive_seeds(user.history, resolve, max_seeds=cfg.max_seeds)
        user_report.counts.seeds = len(seeds)
        pool = candidates_mod.gather_candidates(ctx.tmdb, seeds)
        user_report.counts.candidates = len(pool)

        watched_ids = {s.tmdb_id for s in seeds}
        in_library = candidates_mod.filter_candidates(
            pool,
            library_index,
            watched_tmdb_ids=watched_ids,
            excluded_genres=user.excluded_genres,
            recent_pick_ids=ctx.recent_picks.get(user.slug, set()),
        )
        user_report.counts.in_library = len(in_library)
        ranked = ranking.pre_rank(in_library, cfg.candidates_pre_rank)
        user_report.counts.pre_ranked = len(ranked)

        k = user.row_size or cfg.row_size
        try:
            picks = ctx.curator.curate(user, ranked, k)
            user_report.llm_tokens = getattr(ctx.curator, "last_tokens", 0)
        except CuratorError as e:
            logger.warning("{}: curator failed ({}); degrading to heuristic mode", user.username, e)
            picks = NullCurator().curate(user, ranked, k)
        if len(picks) < k:
            picks = _pad_picks(picks, ranked, k)
        user_report.status = "ok"

    user_report.picks = picks
    user_report.counts.picks = len(picks)
    if not picks:
        logger.warning("{}: no picks produced — leaving any existing row untouched", user.username)
        return False

    if movie_section is None:
        raise RuntimeError("no movie section found for delivery")
    diff, stored = deliver_row(ctx.plex, movie_section, user, picks, cfg, dry_run=cfg.dry_run)
    user_report.diff = diff
    if not cfg.dry_run:
        stored_labels[user.slug] = stored
    return True


def _pad_picks(picks: list[Pick], ranked: list[Candidate], k: int) -> list[Pick]:
    """Top up short curator output from the heuristic order (never invents titles)."""
    have = {p.tmdb_id for p in picks}
    fillers = NullCurator().curate(
        UserProfile(username="", plex_account_id=0, user_type=UserType.SHARED),
        [c for c in ranked if c.tmdb_id not in have],
        k - len(picks),
    )
    out = list(picks)
    for f in fillers:
        out.append(Pick(**{**f.__dict__, "rank": len(out) + 1}))
    return out


def _cold_start_picks(
    ctx: EngineContext,
    user: UserProfile,
    library_index: dict[MediaType, dict[int, int]],
    cfg: EngineConfig,
) -> list[Pick]:
    """ "Popular on <server>" fallback for users with thin history: top-rated unwatched movies."""
    section = next((s for s in ctx.plex.sections() if s.type == "movie"), None)
    if section is None:
        return []
    top = section.search(sort="audienceRating:desc", limit=cfg.row_size * 2)
    picks = []
    for item in top:
        tmdb_id = next(
            (int(g.id.removeprefix("tmdb://")) for g in getattr(item, "guids", []) if g.id.startswith("tmdb://")),
            None,
        )
        if tmdb_id is None:
            continue
        picks.append(
            Pick(
                tmdb_id=tmdb_id,
                rating_key=item.ratingKey,
                title=item.title,
                rank=len(picks) + 1,
                reason="Popular on this server",
            )
        )
        if len(picks) == (user.row_size or cfg.row_size):
            break
    return picks
