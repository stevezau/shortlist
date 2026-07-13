"""Request missing picks: ask Sonarr/Radarr for titles the curator wanted that the library lacks.

The engine already drops every candidate that isn't in a delivery library (``filter_candidates``);
this module keeps a record of those drops instead, ranks them by how many people wanted them and how
well-regarded they are, and — only when the owner has turned requests on — asks Sonarr/Radarr for the
top few. It never touches Plex, so it lives entirely outside the privacy machinery: a request pass
can fail without affecting a single row's visibility.
"""

from __future__ import annotations

from loguru import logger

from rowarr.engine.clients.arr import ArrError, RadarrClient, SonarrClient
from rowarr.engine.clients.omdb import OmdbClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.models import (
    Candidate,
    MediaType,
    MissingTitle,
    RequestConfig,
    RequestOutcome,
    RequestReport,
)

# When gating on IMDb, only this many top-by-demand candidates are looked up on OMDb per run, so a
# large missing pool can't exhaust OMDb's rate limit. A generous multiple of the request cap.
_IMDB_SHORTLIST = 20

# Demand accumulator: (tmdb_id, media_type) -> the missing title and how many users wanted it. The
# pair, never the bare id — movie 1399 and TV 1399 are different titles (see filter_candidates).
DemandMap = dict[tuple[int, MediaType], MissingTitle]


def collect_missing(pool: list[Candidate], library_index: dict[MediaType, dict[int, int]]) -> list[Candidate]:
    """Candidates from the pool that no delivery library holds — the requestable titles.

    Mirrors ``filter_candidates``'s library test exactly (a title is 'present' iff its
    (tmdb_id, media_type) maps to a ratingKey), so the two can never disagree about what's missing.
    Watched/excluded/stale filtering is intentionally NOT applied: those shape one user's row, but a
    title being absent from the server is a fact about the server, and worth requesting regardless.
    """
    return [c for c in pool if library_index.get(c.media_type, {}).get(c.tmdb_id) is None]


def accumulate(demand: DemandMap, missing: list[Candidate]) -> None:
    """Fold one user's missing candidates into the run-wide demand map, counting distinct wanters."""
    for c in missing:
        key = (c.tmdb_id, c.media_type)
        existing = demand.get(key)
        if existing is None:
            demand[key] = MissingTitle(
                tmdb_id=c.tmdb_id,
                title=c.title,
                media_type=c.media_type,
                year=c.year,
                rating=c.rating,
                vote_count=c.vote_count,
                demand=1,
            )
        else:
            existing.demand += 1


def request_missing(
    cfg: RequestConfig,
    tmdb: TmdbClient,
    demand: DemandMap,
    *,
    dry_run: bool,
    min_write_interval: float = 1.0,
) -> RequestReport:
    """Request the top qualifying missing titles from Sonarr/Radarr.

    Gating (all three, always): a title must clear ``min_rating`` AND ``min_votes``, and only the
    top ``max_per_run`` survivors — ranked by demand, then rating, then vote count — are requested.
    One title's failure never stops the rest: each is caught and recorded as its own outcome.
    """
    report = RequestReport()
    # Cheap, source-independent filters first: enough distinct wanters, and recent enough.
    pool = [
        m
        for m in demand.values()
        if m.demand >= cfg.min_demand and (cfg.min_year <= 0 or (m.year or 0) >= cfg.min_year)
    ]
    # Then the rating gate, from whichever source the owner chose.
    if cfg.rating_source == "imdb" and cfg.omdb_api_key:
        qualifying = _gate_by_imdb(cfg, tmdb, pool)
    else:
        qualifying = _gate_by_tmdb(cfg, pool)
    report.considered = len(qualifying)
    selected = qualifying[: max(0, cfg.max_per_run)]
    if not selected:
        logger.info("requests: {} candidates cleared the thresholds, none to request", len(qualifying))
        return report

    # Build each client at most once for the whole pass (they throttle their own writes).
    radarr = RadarrClient(cfg.radarr, min_write_interval=min_write_interval) if cfg.radarr else None
    sonarr = SonarrClient(cfg.sonarr, min_write_interval=min_write_interval) if cfg.sonarr else None

    for title in selected:
        report.outcomes.append(_request_one(title, radarr, sonarr, tmdb, dry_run=dry_run))
    ok = report.requested
    logger.info(
        "requests: {} of {} qualifying title(s) {} ({} considered)",
        ok,
        len(selected),
        "would be requested" if dry_run else "requested",
        report.considered,
    )
    return report


def _gate_by_tmdb(cfg: RequestConfig, pool: list[MissingTitle]) -> list[MissingTitle]:
    """Keep titles clearing the TMDB rating/vote floors, ranked by demand then score."""
    qualifying = [m for m in pool if m.rating >= cfg.min_rating and m.vote_count >= cfg.min_votes]
    qualifying.sort(key=lambda m: (m.demand, m.rating, m.vote_count), reverse=True)
    return qualifying


def _gate_by_imdb(cfg: RequestConfig, tmdb: TmdbClient, pool: list[MissingTitle]) -> list[MissingTitle]:
    """Keep titles clearing the IMDb rating/vote floors, ranked by demand then IMDb score.

    Only a shortlist (top by demand, then TMDB score as a cheap proxy) is looked up on OMDb, so a
    big missing pool can't blow OMDb's rate limit. A lookup that fails or has no IMDb data just drops
    that title — never a failed run.
    """
    shortlist = sorted(pool, key=lambda m: (m.demand, m.rating, m.vote_count), reverse=True)[:_IMDB_SHORTLIST]
    omdb = OmdbClient(cfg.omdb_api_key)
    scored: list[tuple[MissingTitle, float, int]] = []
    for title in shortlist:
        try:
            imdb_id = tmdb.imdb_id(title.tmdb_id, title.media_type)
            score = omdb.rating(imdb_id) if imdb_id else None
        except Exception as e:  # a TMDB/OMDb hiccup drops this title, never the whole gate
            logger.warning("IMDb lookup for {!r} failed: {}", title.title, e)
            continue
        if score is None:
            continue
        rating, votes = score
        if rating >= cfg.min_rating and votes >= cfg.min_votes:
            scored.append((title, rating, votes))
    scored.sort(key=lambda row: (row[0].demand, row[1], row[2]), reverse=True)
    return [title for title, _, _ in scored]


def _request_one(
    title: MissingTitle,
    radarr: RadarrClient | None,
    sonarr: SonarrClient | None,
    tmdb: TmdbClient,
    *,
    dry_run: bool,
) -> RequestOutcome:
    """Route one missing title to the right app; translate any failure into an outcome, never a raise."""

    def outcome(status: str, detail: str) -> RequestOutcome:
        return RequestOutcome(
            tmdb_id=title.tmdb_id,
            title=title.title,
            media_type=title.media_type,
            status=status,
            detail=detail,
        )

    try:
        if title.media_type is MediaType.MOVIE:
            if radarr is None:
                return outcome("skipped_no_target", "Radarr isn't configured")
            status, detail = radarr.add_movie(title.tmdb_id, dry_run=dry_run)
            return outcome(status, detail)
        # Shows: Sonarr keys on TVDB, so cross the namespace first. The TVDB lookup is a TMDB call,
        # not an Arr one, so it raises RuntimeError/httpx errors rather than ArrError — catch it here
        # so one show's lookup hiccup becomes that title's outcome, never an escape that discards the
        # whole pass's recorded outcomes (the run-level handler would otherwise lose the audit trail).
        if sonarr is None:
            return outcome("skipped_no_target", "Sonarr isn't configured")
        try:
            tvdb_id = tmdb.tvdb_id(title.tmdb_id, title.media_type)
        except Exception as e:
            logger.warning("TVDB lookup for {!r} failed: {}", title.title, e)
            return outcome("error", "could not resolve this show's TheTVDB id")
        if tvdb_id is None:
            return outcome("skipped_no_tvdb", "no TheTVDB id for this show")
        status, detail = sonarr.add_series(tvdb_id, dry_run=dry_run)
        return outcome(status, detail)
    except ArrError as e:
        # A request failing is a footnote, never a run failure — Sonarr/Radarr are optional plumbing.
        logger.warning("request for {!r} failed: {}", title.title, e)
        return outcome("error", str(e))
