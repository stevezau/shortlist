"""Request missing picks: ask Sonarr/Radarr for titles the curator wanted that the library lacks.

The engine already drops every candidate that isn't in a delivery library (``filter_candidates``);
this module keeps a record of those drops instead, ranks them by how many people wanted them and how
well-regarded they are, and — only when the owner has turned requests on — asks Sonarr/Radarr for the
top few. It never touches Plex, so it lives entirely outside the privacy machinery: a request pass
can fail without affecting a single row's visibility.
"""

from __future__ import annotations

from loguru import logger

from shortlist.engine.clients.arr import ArrError, RadarrClient, SonarrClient
from shortlist.engine.clients.mdblist import VOTE_SOURCES, MdbListClient, MdbListRateLimitError
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import (
    Candidate,
    MediaType,
    MissingTitle,
    RequestConfig,
    RequestOutcome,
    RequestReport,
    RequestWhy,
)

# When gating on a non-TMDB source, only this many top-by-demand candidates are looked up on MDBList
# per run, so a large missing pool can't blow the daily cap (each title's whole rating set is also
# cached, so re-runs mostly hit the cache). A generous multiple of the request cap.
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


def accumulate(
    demand: DemandMap,
    missing: list[Candidate],
    tags: set[str] | None = None,
    wanter: str | None = None,
    why: list[RequestWhy] | None = None,
) -> None:
    """Fold one user's missing candidates into the run-wide demand map, counting distinct wanters.

    ``tags`` are the request tags to attach to every title in this batch — the wanting user's own tag
    plus each row they're in the audience of. They accumulate across users, so a title three people
    want carries the union of all three users' (and their rows') tags when it's finally requested.

    ``wanter`` is the username whose taste surfaced these titles; it's collected into each title's
    ``wanters`` so the inbox can show WHO drove the demand, not just the count. ``why`` is the fuller
    provenance for the same batch (one entry per row that surfaced it, with the seed/source), merged
    and de-duplicated so the inbox can explain which row each request came from and why.
    """
    tags = {t for t in (tags or set()) if t}
    who = {wanter} if wanter else set()
    reasons = list(why or [])
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
                tags=set(tags),
                wanters=set(who),
                why=list(reasons),
            )
        else:
            existing.demand += 1
            existing.tags |= tags
            existing.wanters |= who
            for reason in reasons:
                if reason not in existing.why:
                    existing.why.append(reason)


def _within_year_window(year: int | None, min_year: int, max_year: int) -> bool:
    """Whether a candidate's release year falls inside the requested window.

    Both bounds are inclusive; ``<= 0`` disables that end (so ``0, 0`` accepts everything). A show's
    ``year`` is its first-air year (set at the candidate source). When a bound is active but the
    title has no year, it is excluded — a year restriction can't be judged against an unknown date,
    so the conservative choice is not to auto-request it.
    """
    if min_year <= 0 and max_year <= 0:
        return True
    if year is None:
        return False
    return (min_year <= 0 or year >= min_year) and (max_year <= 0 or year <= max_year)


def request_missing(
    cfg: RequestConfig,
    tmdb: TmdbClient,
    demand: DemandMap,
    *,
    dry_run: bool,
    min_write_interval: float = 1.0,
    already_handled: set[tuple[int, str]] | None = None,
    mdblist: MdbListClient | None = None,
) -> RequestReport:
    """Auto-request the strongest missing titles; queue the rest for the owner to approve.

    Base floors first (``min_demand``, the ``min_year``..``max_year`` window, then the chosen
    ``rating_source`` rating/vote floors): a title must clear all of them to be requestable at all.
    Among the survivors, those that also
    clear the higher auto-send bar (``auto_min_demand`` and ``auto_min_rating``) are requested now —
    ranked by demand, then rating, then votes, and capped at ``max_per_run``. Everyone else, including
    auto-worthy titles that overflowed the cap, is returned in ``report.queued`` for manual review.
    One title's failure never stops the rest: each is caught and recorded as its own outcome.
    """
    report = RequestReport()
    # Titles the owner has already actioned — asked for, or said no to — are out of the running
    # entirely. Two bugs lived here: a title still DOWNLOADING was still "missing", so it re-won a
    # slot every night and `max_per_run` starved forever on the same five titles; and a REJECTED
    # title could still be auto-sent later, so a "no" wasn't a no.
    handled = already_handled or set()
    # Cheap, source-independent floors first: enough distinct wanters, and inside the year window.
    pool = [
        m
        for m in demand.values()
        if (m.tmdb_id, str(m.media_type)) not in handled
        and m.demand >= cfg.min_demand
        and _within_year_window(m.year, cfg.min_year, cfg.max_year)
    ]
    # Then the rating gate, from whichever source the owner chose (it ranks the survivors too). A
    # non-TMDB source needs MDBList; if it isn't available, or its quota runs out mid-gate, we fall
    # back to TMDB so the run still completes (and flag it so the owner is told — see _gate_by_source).
    if cfg.rating_source != "tmdb" and mdblist is not None:
        qualifying = _gate_by_source(cfg, mdblist, pool, report)
    else:
        qualifying = _gate_by_tmdb(cfg, pool)
    report.considered = len(qualifying)

    # Attach each surviving title's IMDb id (one TMDB call, cached) so the inbox can deep-link to the
    # title page instead of an IMDb search. Only the gated shortlist is looked up, and best-effort — a
    # miss just leaves the search fallback.
    for m in qualifying:
        if not m.imdb_id:
            try:
                m.imdb_id = tmdb.imdb_id(m.tmdb_id, m.media_type) or ""
            except Exception as e:  # never fail the run for a link nicety
                logger.debug("imdb id lookup for {!r} failed: {}", m.title, e)

    # Build the Arr clients once (reused for the state check below and the send), then reconcile the
    # pool against what the Arrs already know: drop titles they already track (not really "missing" —
    # a downloading title isn't in Plex yet), and flag titles on an exclusion list so the owner sees
    # why approving them would be a no-op. Fails OPEN — a fetch error skips the check, never drops.
    radarr = RadarrClient(cfg.radarr, min_write_interval=min_write_interval) if cfg.radarr else None
    sonarr = SonarrClient(cfg.sonarr, min_write_interval=min_write_interval) if cfg.sonarr else None
    qualifying, in_arr, report.arr_present = _apply_arr_state(tmdb, qualifying, radarr, sonarr)
    if in_arr:
        logger.info("requests: {} qualifying already in Sonarr/Radarr — dropped", in_arr)

    # Hybrid split: the strongest clear the auto-send bar and go now (capped); the rest wait for the
    # owner. Auto-worthy titles beyond the cap fall through to the queue rather than being lost. An
    # excluded title is never auto-sent (the Arr would refuse it) — it's surfaced for a manual call.
    cap = max(0, cfg.max_per_run)
    auto: list[MissingTitle] = []
    for m in qualifying:  # already ranked best-first by the gate
        clears_auto = (
            cfg.auto_send and not m.excluded and m.demand >= cfg.auto_min_demand and m.rating >= cfg.auto_min_rating
        )
        if clears_auto and len(auto) < cap:
            auto.append(m)
        else:
            report.queued.append(m)

    if not auto:
        logger.info("requests: {} qualifying, 0 auto-sent, {} queued for approval", len(qualifying), len(report.queued))
        return report

    report.outcomes = _send(
        cfg, tmdb, auto, dry_run=dry_run, min_write_interval=min_write_interval, radarr=radarr, sonarr=sonarr
    )
    # Only the ones the Arr actually accepted. A send that failed, or was skipped for want of a TVDB
    # id, must stay requestable — suppressing it would lose the title silently.
    # Keyed by (tmdb_id, media_type), never the bare id — movie 550 and TV 550 are different titles,
    # and both can land in one auto batch (see filter_candidates for the same rule).
    landed = {(o.tmdb_id, o.media_type) for o in report.outcomes if o.status in ("requested", "would_request")}
    report.sent = [m for m in auto if (m.tmdb_id, m.media_type) in landed]
    # A failed auto-send (status "error" — e.g. a Sonarr/Radarr lookup 5xx) used to vanish: it was in
    # neither `sent` nor `queued`, so it never reached the inbox, retried blindly every night, and its
    # reason was invisible. Queue it WITH the reason so it shows in Waiting, retriable by hand. Only
    # "error" — the skips are deliberately NOT queued: skipped_present is already in the Arr (handled),
    # and skipped_no_tvdb/skipped_no_target can never be requested, so surfacing them is just noise.
    fail_detail = {(o.tmdb_id, o.media_type): o.detail for o in report.outcomes if o.status == "error"}
    for m in auto:
        if (m.tmdb_id, m.media_type) in fail_detail:
            m.detail = fail_detail[(m.tmdb_id, m.media_type)]
            report.queued.append(m)
    logger.info(
        "requests: {} of {} auto-{}, {} queued for approval ({} considered)",
        report.requested,
        len(auto),
        "would-send" if dry_run else "sent",
        len(report.queued),
        report.considered,
    )
    return report


def request_titles(
    cfg: RequestConfig,
    tmdb: TmdbClient,
    titles: list[MissingTitle],
    *,
    dry_run: bool,
    min_write_interval: float = 1.0,
) -> RequestReport:
    """Request an explicit list of titles the owner approved from the inbox — no gating.

    The thresholds already decided these were worth surfacing, and the owner picked them by hand, so
    this skips every floor and just sends. Each title's failure is its own outcome, never a raise.
    """
    report = RequestReport(considered=len(titles))
    report.outcomes = _send(cfg, tmdb, titles, dry_run=dry_run, min_write_interval=min_write_interval)
    return report


def _send(
    cfg: RequestConfig,
    tmdb: TmdbClient,
    titles: list[MissingTitle],
    *,
    dry_run: bool,
    min_write_interval: float,
    radarr: RadarrClient | None = None,
    sonarr: SonarrClient | None = None,
) -> list[RequestOutcome]:
    """Route every title to its Arr. Clients are built at most once — passed in when the caller
    already built them (so the arr-state check and the send share one), else built here."""
    if radarr is None and cfg.radarr:
        radarr = RadarrClient(cfg.radarr, min_write_interval=min_write_interval)
    if sonarr is None and cfg.sonarr:
        sonarr = SonarrClient(cfg.sonarr, min_write_interval=min_write_interval)
    return [_request_one(title, radarr, sonarr, tmdb, dry_run=dry_run) for title in titles]


def _apply_arr_state(
    tmdb: TmdbClient,
    pool: list[MissingTitle],
    radarr: RadarrClient | None,
    sonarr: SonarrClient | None,
) -> tuple[list[MissingTitle], int, set[tuple[int, str]]]:
    """Reconcile the gated pool against what the Arrs already know.

    Drops titles an Arr already tracks (they aren't really "missing" — a downloading title just isn't
    in Plex yet) and flags titles on an Arr import-exclusion list (``m.excluded``) so the inbox can
    show why approving one wouldn't add it. Returns ``(kept, dropped_count, arr_present)`` — the last
    is every (tmdb_id, MediaType.value) the Arrs track, for the server's stale-pending-row prune
    (see ``RequestReport.arr_present``); shows land in it via Sonarr v4's own ``tmdbId`` (empty on v3).

    Fails OPEN: any fetch error skips that Arr's checks entirely — a redundant request is a far
    smaller sin than silently dropping a title the owner actually wanted. Shows are matched on TVDB
    (Sonarr's key), resolved once per title and cached on ``m.tvdb_id`` for the later send.
    """
    if not pool:
        return pool, 0, set()
    # Only fetch the sets for media types actually present — a movie-only pool shouldn't pay for
    # Sonarr's two calls, and vice versa.
    want_movies = any(m.media_type is MediaType.MOVIE for m in pool)
    want_shows = any(m.media_type is not MediaType.MOVIE for m in pool)
    movie_present = _safe_ids(radarr.library_tmdb_ids) if radarr and want_movies else set()
    movie_excluded = _safe_ids(radarr.excluded_tmdb_ids) if radarr and want_movies else set()
    show_present, show_present_tmdb = _safe_id_pair(sonarr.library_ids) if sonarr and want_shows else (set(), set())
    show_excluded = _safe_ids(sonarr.excluded_tvdb_ids) if sonarr and want_shows else set()
    arr_present = {(tid, MediaType.MOVIE.value) for tid in movie_present} | {
        (tid, MediaType.SHOW.value) for tid in show_present_tmdb
    }

    kept: list[MissingTitle] = []
    dropped = 0
    for m in pool:
        if m.media_type is MediaType.MOVIE:
            present, excluded = movie_present, movie_excluded
            key: int | None = m.tmdb_id
        else:
            present, excluded = show_present, show_excluded
            # Only pay the TVDB lookup when there's actually a Sonarr set to match against.
            key = _resolve_tvdb(tmdb, m) if (show_present or show_excluded) else None
        if key is not None and key in present:
            dropped += 1
            continue
        if key is not None and key in excluded:
            m.excluded = True  # surfaced as its own inbox flag; the app is inferred from media_type
        kept.append(m)
    return kept, dropped, arr_present


def _safe_ids(fetch) -> set[int]:
    """Call an Arr id-set fetch, returning an empty set on ANY error so the check truly fails open.

    Deliberately broad: besides ``ArrError`` (connect/auth/non-200), a 200 with a non-JSON body — an
    SSO interstitial or SPA HTML from a misconfigured reverse proxy — makes ``r.json()`` raise
    ``ValueError``. Either way, skip the check and request as if the Arr held nothing, never abort the
    whole pass (which would drop every wanted title on a proxy hiccup).
    """
    try:
        return fetch()
    except Exception as e:
        logger.warning("Arr state fetch failed, skipping that check this run: {}", e)
        return set()


def _safe_id_pair(fetch) -> tuple[set[int], set[int]]:
    """``_safe_ids`` for a fetch returning a pair of id sets (``SonarrClient.library_ids``)."""
    try:
        return fetch()
    except Exception as e:
        logger.warning("Arr state fetch failed, skipping that check this run: {}", e)
        return set(), set()


def _resolve_tvdb(tmdb: TmdbClient, m: MissingTitle) -> int | None:
    """A show's TVDB id, cached on the title so presence-check and send don't each look it up."""
    if m.tvdb_id is None:
        try:
            m.tvdb_id = tmdb.tvdb_id(m.tmdb_id, m.media_type)
        except Exception as e:  # a lookup miss just means we can't dedup this one — never fatal
            logger.debug("tvdb lookup for {!r} failed: {}", m.title, e)
    return m.tvdb_id


def _gate_by_tmdb(cfg: RequestConfig, pool: list[MissingTitle]) -> list[MissingTitle]:
    """Keep titles clearing the TMDB rating/vote floors, ranked by demand then score."""
    qualifying = [m for m in pool if m.rating >= cfg.min_rating and m.vote_count >= cfg.min_votes]
    qualifying.sort(key=lambda m: (m.demand, m.rating, m.vote_count), reverse=True)
    return qualifying


def _gate_by_source(
    cfg: RequestConfig, mdblist: MdbListClient, pool: list[MissingTitle], report: RequestReport
) -> list[MissingTitle]:
    """Keep titles clearing the chosen MDBList source's rating/vote floors, ranked by demand then score.

    Only a shortlist (top by demand, then TMDB score as a cheap proxy) is looked up, so a big missing
    pool stays well under MDBList's daily cap — and every source is cached per title, so re-runs mostly
    hit the cache. A lookup that fails or has no score for this source just drops that title. If the
    daily quota runs out mid-gate, we stop, flag the run, and fall back to TMDB for the WHOLE pool so
    the run still completes (the owner is alerted from the flag).
    """
    source = cfg.rating_source
    enforce_votes = source in VOTE_SOURCES  # RT/Metacritic are critic scores — no audience-vote floor
    shortlist = sorted(pool, key=lambda m: (m.demand, m.rating, m.vote_count), reverse=True)[:_IMDB_SHORTLIST]
    scored: list[tuple[MissingTitle, float, int]] = []
    for title in shortlist:
        try:
            score = mdblist.rating(title.tmdb_id, title.media_type, source)
        except MdbListRateLimitError:
            logger.warning("MDBList daily limit reached — falling back to TMDB ratings for this run")
            report.ratings_rate_limited = True
            return _gate_by_tmdb(cfg, pool)
        except Exception as e:  # a single lookup hiccup drops that title, never the whole gate
            logger.warning("{} rating lookup for {!r} failed: {}", source, title.title, e)
            continue
        if score is None:
            continue
        rating, votes = score
        if rating >= cfg.min_rating and (not enforce_votes or votes >= cfg.min_votes):
            # Carry the chosen-source score forward so the auto-send bar and the queued rows the owner
            # reviews both reflect it, not the TMDB value the title arrived with.
            title.rating = rating
            title.vote_count = votes
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
            status, detail = radarr.add_movie(title.tmdb_id, dry_run=dry_run, extra_tags=title.tags)
            return outcome(status, detail)
        # Shows: Sonarr keys on TVDB, so cross the namespace first. The TVDB lookup is a TMDB call,
        # not an Arr one, so it raises RuntimeError/httpx errors rather than ArrError — catch it here
        # so one show's lookup hiccup becomes that title's outcome, never an escape that discards the
        # whole pass's recorded outcomes (the run-level handler would otherwise lose the audit trail).
        if sonarr is None:
            return outcome("skipped_no_target", "Sonarr isn't configured")
        # Reuse the TVDB id if the arr-state check already resolved it this run; else look it up now.
        tvdb_id = title.tvdb_id
        if tvdb_id is None:
            try:
                tvdb_id = tmdb.tvdb_id(title.tmdb_id, title.media_type)
            except Exception as e:
                logger.warning("TVDB lookup for {!r} failed: {}", title.title, e)
                return outcome("error", "could not resolve this show's TheTVDB id")
        if tvdb_id is None:
            return outcome("skipped_no_tvdb", "no TheTVDB id for this show")
        status, detail = sonarr.add_series(tvdb_id, dry_run=dry_run, extra_tags=title.tags)
        return outcome(status, detail)
    except ArrError as e:
        # A request failing is a footnote, never a run failure — Sonarr/Radarr are optional plumbing.
        logger.warning("request for {!r} failed: {}", title.title, e)
        return outcome("error", str(e))
