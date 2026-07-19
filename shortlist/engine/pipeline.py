"""Run orchestration: the leak-safe ordering of an engine run.

``run()`` reads top to bottom as the ordered sequence of phases it is: build the library indexes,
sweep unhidable rows, deliver every row UNPROMOTED, merge every share filter, promote, then request.
Row construction itself lives in ``rows.py``; this module owns only the ordering and the privacy
guarantees that depend on it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger

import shortlist.engine.rows as rows
from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.poster import PosterArtist
from shortlist.engine.clients.search import WebSearchProvider
from shortlist.engine.clients.tmdb import Cache, NullCache, TmdbClient
from shortlist.engine.clients.trakt import TraktClient
from shortlist.engine.curator import Curator
from shortlist.engine.delivery import (
    render_row_name,
    resolve_row_template,
    row_marker,
    sweep_broken_rows,
    target_sections,
)
from shortlist.engine.history import HistorySource
from shortlist.engine.models import (
    CollectionDiff,
    EngineConfig,
    HubAnchor,
    MediaType,
    RequestOutcome,
    RequestReport,
    RowSpec,
    RunReport,
    UserProfile,
    UserRunReport,
)
from shortlist.engine.privacy import (
    SnapshotStore,
    shared_label_audiences,
    shortlist_labels_in,
    sync_user_restrictions,
)


@dataclass
class EngineContext:
    """Everything one run needs; the server adapter builds this once."""

    config: EngineConfig
    plex: PlexClient
    plextv: PlexTvClient
    tmdb: TmdbClient
    history_source: HistorySource
    curator: Curator
    snapshots: SnapshotStore
    # Optional 'related titles' candidate source; None when no Trakt key is configured.
    trakt: TraktClient | None = None
    # Optional external web-search backend for the llm_web source (Exa); None when no key is
    # configured. Native provider web-search tools don't need it; a local Ollama model does.
    search: WebSearchProvider | None = None
    # Optional image-generation backend for generate-mode row posters, built from the AI curator's
    # provider/key. None when the curator provider can't make images (Anthropic, Ollama) or none is set.
    poster_artist: PosterArtist | None = None
    # slug -> {(tmdb_id, media_type)}: the staleness guard. Keyed on the PAIR because TMDB ids
    # are unique only within a namespace — movie 550 and TV 550 are different titles.
    recent_picks: dict[str, set[tuple[int, MediaType]]] = field(default_factory=dict)
    # media_type -> [{tmdb_id, rating_key, title, year, genres}] for the delivery libraries, built
    # once per run and only when the AI-from-library candidate source is enabled (else empty).
    library_catalog: dict[MediaType, list[dict]] = field(default_factory=dict)
    # The same catalog split per library, so a row pinned to specific libraries only ever offers
    # the AI titles it could actually deliver.
    section_catalog: dict[str, list[dict]] = field(default_factory=dict)
    # section key -> {tmdb_id: ratingKey}: per-library index so a row delivered into a specific
    # library uses that library's ratingKeys. Built by _build_indexes each run.
    section_index: dict[str, dict[int, int]] = field(default_factory=dict)
    # tmdb_id -> total episode count (leafCount) for every show, so the watched-filter can tell a
    # finished show from a sampled one or one with a new season. Built by _build_indexes.
    episode_counts: dict[int, int] = field(default_factory=dict)
    # Every library rows may be delivered to (all movie + show sections), for resolving a row's
    # library_keys to real sections. Built by _build_indexes each run.
    delivery_sections: list = field(default_factory=list)
    # plex account id -> the slug Shortlist assigned that account, for EVERY user it knows (not just
    # tonight's). This is how "whose row is this?" is answered. It cannot be answered from a name:
    # people rename themselves, and two display names can slugify to the same string — either
    # would silently hand one account another's row.
    known_slugs: dict[int, str] = field(default_factory=dict)
    # (tmdb_id, media_type) the owner has already actioned in the Requests inbox — sent or rejected.
    # Keeps a slow download from re-winning a request slot every night, and a "no" from being undone
    # by a later auto-send. Empty for direct engine runs, which have no inbox.
    handled_requests: set[tuple[int, str]] = field(default_factory=set)
    progress: Callable[[str, str, dict], None] | None = None  # (user_slug, stage, counts) -> None
    # Called the moment one user finishes (before their terminal progress event), with their profile
    # and finished report — so the server can persist that user's results INCREMENTALLY and the UI
    # shows them as each person completes, instead of the whole roster appearing only at run's end.
    # Must be resilient: it runs on the worker threads, and any error is swallowed (never sinks a run).
    on_user_done: Callable[[UserProfile, UserRunReport], None] | None = None
    # Cross-run cache for the per-library tmdb_id -> ratingKey index, keyed by a cheap change signal
    # (item count + last-updated). An unchanged library skips its full scan next run. NullCache (the
    # default) disables it — safe, since a stale/missing entry only ever means a re-scan.
    index_cache: Cache = field(default_factory=NullCache)
    # Day number of this run (date.toordinal()), the phase for freshness rotation so a row shifts
    # day to day but is reproducible within a day. Set at the start of run(); 0 disables rotation.
    run_day: int = 0
    # How many users to process concurrently. 1 = fully sequential (the safe engine/test default).
    # The server sets this from `run.concurrency`. Only the READ + LLM work overlaps; every Plex and
    # plex.tv write is serialized by ``write_lock``, so the leak-safe ordering is preserved exactly.
    concurrency: int = 1
    write_lock: threading.Lock = field(default_factory=threading.Lock)


def _emit(ctx: EngineContext, slug: str, stage: str, counts: dict) -> None:
    # Mirror every stage to the container log too, so `docker logs` narrates a run in real time —
    # the same story the UI's activity feed tells, for anyone watching the console.
    logger.info("run · {} · {}{}", slug, stage, f" {counts}" if counts else "")
    if ctx.progress is not None:
        try:
            ctx.progress(slug, stage, counts)
        except Exception:  # a broken progress listener must never fail a run
            logger.exception("progress callback failed")


def run(ctx: EngineContext, users: list[UserProfile]) -> RunReport:
    """Run the pipeline for every enabled user. Users are independent — one failure never
    stops the run (per-user try/except; plex-safety rule 6 resume-safety).

    Write ordering is leak-safe: rows are created/updated UNPROMOTED, then every user's
    share filters are merged, and only then are rows promoted onto shared Home — so a new
    collection is never visible to anyone before the exclusions that hide it exist.
    """
    report = RunReport(started_at=datetime.now(UTC), dry_run=ctx.config.dry_run)
    # Freshness rotates a row by a per-DAY phase, so it shifts day to day but stays reproducible
    # within a day (a re-run the same night doesn't reshuffle). Only overwrite the default 0 (which
    # disables rotation) so a caller/test can pin a specific day.
    if not ctx.run_day:
        ctx.run_day = report.started_at.toordinal()

    # Tell the UI the full queue up front — cards can say "queued (3rd in line)"
    # instead of a bare "waiting…" while the indexes build.
    for position, user in enumerate(users, start=1):
        _emit(ctx, user.slug, "queued", {"position": position})

    # Reading the libraries is the long, quiet phase between "queued" and the first per-user stage
    # (it walks every item in every library, ~thousands of PMS reads). Narrate it so the activity log
    # doesn't look frozen while it runs.
    _emit(ctx, "Shortlist", "preparing", {})
    sections = ctx.plex.sections()
    seed_index, library_index = _build_indexes(ctx, users, sections)
    # What the delivery libraries now hold — so the server can drop inbox candidates that have since
    # arrived on the server (grabbed elsewhere) instead of leaving them to linger forever.
    report.library_present = {(tmdb_id, media_type) for media_type, idx in library_index.items() for tmdb_id in idx}

    # BEFORE ANY USER WORK: delete every row on the server that Plex cannot hide. Fail closed.
    if not _sweep_phase(ctx, report):
        return report

    # Preload label casing + collection ids from the PMS — the source of truth survives
    # restarts and covers users whose delivery fails this run.
    stored_labels = {slug: row.label for slug, row in ctx.plex.owned_collections(ctx.config.label_prefix).items()}

    # Missing-title demand, accumulated across users only when requests are on — the common case
    # (feature off) pays nothing for it. None -> _run_user does no missing-title bookkeeping at all.
    requests_on = bool(ctx.config.requests and ctx.config.requests.enabled)
    demand: requests_mod.DemandMap = {}

    # Collection item-ordering is deferred to a best-effort pass AFTER promotion (see
    # _collection_order_phase): each (collection, ranked_keys) delivery records here, so the expensive
    # one-move-per-item ordering never runs inside the serial delivery write-lock and can't stall it.
    order_work: list[tuple] = []

    # Deliver every per-person and shared row UNPROMOTED — nothing is on anyone's Home yet.
    to_promote, shared_to_promote = _deliver_phase(
        ctx, users, seed_index, library_index, stored_labels, report, demand if requests_on else None, order_work
    )

    # Merge the excludes into every share filter BEFORE anything is promoted.
    filters_ok = _privacy_sync_phase(ctx, users, stored_labels, report)
    if filters_ok is None:
        # The plex.tv roster could not be read — no filters written, nothing promoted. The sweep
        # above already deleted rows, and the report (already populated) keeps that audit (rule 10).
        return report

    # Only now, with the exclusions in place, promote rows onto shared Home.
    _promote_phase(ctx, to_promote, shared_to_promote, filters_ok, report)

    # Order each row's items (the expensive one-move-per-item step) as a best-effort pass AFTER
    # promotion — rows are already delivered, hidden and live, so a slow PMS here degrades only the
    # ordering, never the run. Privacy-neutral (never touches a label, filter, or promotion).
    _collection_order_phase(ctx, order_work)

    # Position the just-promoted rows in each library's Recommended shelf (must run after promotion —
    # a hub has to be promoted to be movable). Best-effort and privacy-neutral.
    if filters_ok:
        _order_phase(ctx, report)

    # Sonarr/Radarr requests, dead LAST — after every Plex write is done.
    _request_phase(ctx, requests_on, demand, report)

    report.finished_at = datetime.now(UTC)
    ok = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    logger.info("run complete: {}/{} users ok (dry_run={})", ok, len(report.users), ctx.config.dry_run)
    return report


# The real invalidation is the section SIGNATURE (item count + last-updated): the moment the library
# changes, the key changes and this is bypassed. This TTL is only a backstop for the rare change the
# signature can't see (a 1-for-1 swap that doesn't bump updatedAt) — kept short so even that self-heals
# within a couple of days rather than lingering.
INDEX_CACHE_TTL_S = 2 * 24 * 3600


def _library_index(ctx: EngineContext, section) -> tuple[dict[int, int], dict[int, int]]:
    """This section's ``(index, episodes)`` — from the cross-run cache when the library is unchanged.

    Keyed on the section + a cheap change signature (item count + last-updated); a signature change
    (a title added/removed/edited) misses and re-scans. JSON object keys are strings, so tmdb ids
    round-trip through ``str()``/``int()``. A missing signature or NullCache just always re-scans.
    """
    signature = ctx.plex.section_signature(section)
    cache_key = f"index:{section.key}:{signature}" if signature else None
    if cache_key and (cached := ctx.index_cache.get(cache_key)):
        data = json.loads(cached)
        index = {int(k): v for k, v in data["index"].items()}
        episodes = {int(k): v for k, v in data["episodes"].items()}
        _emit(ctx, section.title, "indexed (cached)", {"items": len(index)})
        return index, episodes
    _emit(ctx, section.title, "indexing", {})
    index, episodes = ctx.plex.build_library_index(section)
    if cache_key:
        payload = {
            "index": {str(k): v for k, v in index.items()},
            "episodes": {str(k): v for k, v in episodes.items()},
        }
        ctx.index_cache.set(cache_key, json.dumps(payload), INDEX_CACHE_TTL_S)
    _emit(ctx, section.title, "indexed", {"items": len(index)})
    return index, episodes


def _library_catalog(ctx: EngineContext, section) -> list[dict]:
    """This section's AI-from-library catalog — from the cross-run cache when the library is unchanged.

    Same signature-keyed cross-run cache as ``_library_index`` right next to it. Without this the
    llm_library source re-walked every targeted library in full (``section.all()``) on EVERY run, even
    when nothing changed — a second full library scan beside the (already-cached) index. A signature
    change (a title added/removed/edited) misses and re-scans; a missing signature / NullCache always
    re-scans (same safe fallback as the index)."""
    signature = ctx.plex.section_signature(section)
    cache_key = f"catalog:{section.key}:{signature}" if signature else None
    if cache_key and (cached := ctx.index_cache.get(cache_key)):
        catalog = json.loads(cached)
        _emit(ctx, section.title, "catalogued (cached)", {"items": len(catalog)})
        return catalog
    _emit(ctx, section.title, "cataloguing", {})
    catalog = ctx.plex.build_library_catalog(section)
    if cache_key:
        ctx.index_cache.set(cache_key, json.dumps(catalog), INDEX_CACHE_TTL_S)
    return catalog


def _build_indexes(
    ctx: EngineContext, users: list[UserProfile], sections: list
) -> tuple[dict[int, int], dict[MediaType, dict[int, int]]]:
    """Build the library indexes a run reads from.

    Three indexes, because they answer different questions.

    Only the libraries the rows actually TARGET are read (their media type + ``library_keys``), so a
    library no row uses — a Sports library, a music library mis-typed as a show — is never scanned or
    shown in the activity log. The privacy sweep walks EVERY library independently (it deletes leaking
    rows regardless of targeting), so narrowing here never weakens hiding.

    `seed_index` (ratingKey -> tmdb_id, across every TARGETED library) turns what a user WATCHED into a
    TMDB id, and people watch films in "4K Movies" too. It is keyed by ratingKey, not by tmdb_id,
    because that is the direction it is READ in: the same film in two movie libraries is ONE tmdb id
    and TWO ratingKeys, so a tmdb-keyed index would keep only the last library scanned — and every
    watch in the other library would resolve to nothing, leaving that user seedless with an empty row
    and a run that still reported success. A watch in a library no row targets simply doesn't seed
    (nothing could be recommended from it anyway).

    `library_index` (per media type) decides what may be RECOMMENDED: a title is deliverable if it
    lives in ANY library of its type, since a row can target any of them. A pick in no delivery
    library could never be shown to anyone.

    `ctx.section_index` (per section key) decides WHERE a pick can go: a Plex collection lives in one
    library and can only hold that library's items (its ratingKeys), so delivering a row into a
    specific library needs that library's ratingKey for each pick.
    """
    seed_index: dict[int, int] = {}
    library_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    section_index: dict[str, dict[int, int]] = {}
    section_catalog: dict[str, list[dict]] = {}
    episode_counts: dict[int, int] = {}
    # Only when there is someone to recommend to. The indexes walk every item in every TARGETED
    # library, and are read only inside _run_user — so with no users this is thousands of PMS reads
    # thrown away, in front of the sweep, on the one path (a closed gate) where the sweep is the entire
    # point and must not be preceded by anything that can fail.
    #
    # Read only the libraries some row targets (media type + library_keys). Retired rows are included
    # so a disabled row is still curated/indexed where it lives; the mute/retire CLEANUP scans every
    # library independently (rows._remove_muted_and_retired), and the leak sweep covers everything else
    # independently too. A library no row uses (e.g. a Sports library) is skipped entirely.
    if users:
        # The EFFECTIVE specs — per_person_rows() synthesizes the legacy default row when rows aren't
        # managed, so an unconfigured run still reads every library (it targets them all).
        wanted_keys = {
            str(section.key)
            for spec in (*ctx.config.per_person_rows(), *ctx.config.shared_rows(), *ctx.config.retired_rows)
            for section in target_sections(sections, spec)
        }
        index_sections = [section for section in sections if str(section.key) in wanted_keys]
    else:
        index_sections = []
    for section in index_sections:
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        index, episodes = _library_index(ctx, section)
        episode_counts.update(episodes)
        seed_index.update({rating_key: tmdb_id for tmdb_id, rating_key in index.items()})
        # Every library of a deliverable type is both a recommendation source (union) and a possible
        # delivery target (its own per-section index) — a row picks which ones under library_keys.
        library_index[kind].update(index)
        section_index[section.key] = index
    ctx.section_index = section_index
    ctx.episode_counts = episode_counts
    ctx.delivery_sections = index_sections
    # The AI-from-library source needs titles/genres. Built when ANY row wants it — not just the
    # global setting: a row overriding its sources to llm_library found an empty catalog and
    # produced nothing, forever, while reporting ok. And built from every TARGETED library, not one
    # representative per type, or a row pinned to "4K Movies" would be offered the "Movies" catalog.
    if users and _wants_library_catalog(ctx.config):
        catalog: dict[MediaType, list[dict]] = {MediaType.MOVIE: [], MediaType.SHOW: []}
        seen: dict[MediaType, set[int]] = {MediaType.MOVIE: set(), MediaType.SHOW: set()}
        for section in index_sections:
            kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
            items = _library_catalog(ctx, section)  # cross-run cached, like the index
            section_catalog[section.key] = items
            # Deduped across libraries: the same film in "Movies" and "4K Movies" is one title to
            # recommend, and listing it twice would spend the LLM's slice of the catalog on itself.
            for item in items:
                if item["tmdb_id"] not in seen[kind]:
                    seen[kind].add(item["tmdb_id"])
                    catalog[kind].append(item)
        ctx.library_catalog = catalog
        ctx.section_catalog = section_catalog
    return seed_index, library_index


def _wants_library_catalog(config) -> bool:
    """Whether any row on this server uses the AI-from-library source (globally or as an override)."""
    if "llm_library" in config.candidate_sources:
        return True
    return any("llm_library" in (spec.candidate_sources or []) for spec in config.rows)


def _sweep_phase(ctx: EngineContext, report: RunReport) -> bool:
    """Delete every row on the server that Plex cannot hide. Returns False (run aborts) on failure.

    This is server-wide, not per-user, and it is deliberately not inside the delivery loop. A row's
    hideability has nothing to do with whether its owner is enabled tonight, so scoping the
    sweep to `users` would let one click of "pause" — or `paused_all`, which makes `users` empty
    — turn a live leak into a permanent one, silently, with every run reporting green.

    It also runs before anything that can fail. TMDB rate-limits, Tautulli disappears, the PMS
    times out; none of that may leave a row visible to everyone for another night.
    """
    try:
        # The accumulator is the report's own dict, so rows deleted before any mid-walk failure
        # are still audited — a destructive write must never go unrecorded (rule 10).
        sweep_broken_rows(
            ctx.plex,
            ctx.config,
            # slug -> the marker its row's title must carry. Without this a shared-tag row would
            # survive for any user who gets no picks tonight, still showing them other people's.
            markers={slug: row_marker(account_id) for account_id, slug in ctx.known_slugs.items()},
            dry_run=ctx.config.dry_run,
            deleted=report.swept_rows,
        )
    except Exception as e:
        # Fail closed. We cannot prove the server has no unhidable rows, so we do not write more.
        report.error = f"unhidable-row sweep failed: {type(e).__name__}: {e}"
        report.finished_at = datetime.now(UTC)
        logger.exception("could not sweep unhidable rows — refusing to write anything this run")
        return False
    return True


def _deliver_phase(
    ctx: EngineContext,
    users: list[UserProfile],
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    report: RunReport,
    demand: requests_mod.DemandMap | None,
    order_work: list[tuple],
) -> tuple[list[UserProfile], list[tuple[RowSpec, UserProfile]]]:
    """Deliver every per-person and shared row, all UNPROMOTED. Returns the promotion candidates."""
    to_promote: list[UserProfile] = []

    def process(user: UserProfile) -> tuple[UserProfile, UserRunReport, bool]:
        user_report = UserRunReport(username=user.username, slug=user.slug)
        # A row swept for this user is part of their story this run — but the swept dict is the
        # run-level record, so a paused user's deletion is never lost just because they have no
        # UserRunReport.
        swept_titles = report.swept_rows.get(user.slug, [])
        started = time.monotonic()
        delivered = False
        try:
            # PMS timeouts are retried at the DELIVERY write (idempotent) inside _run_user, so a Plex
            # hiccup no longer re-runs the whole user's gather + LLM curate (which is what made a slow
            # night catastrophic — SFLIX run 3, 2026-07-19). A timeout that exhausts the delivery
            # retries, or one from a non-delivery PMS read, falls through here and fails just this user.
            delivered = rows._run_user(
                ctx, user, seed_index, library_index, stored_labels, user_report, demand, order_work
            )
        except Exception as e:
            user_report.status = "error"
            user_report.error = f"{type(e).__name__}: {e}"
            logger.exception("{}: pipeline failed", user.username)
        finally:
            if swept_titles:
                if user_report.diff is None:
                    user_report.diff = CollectionDiff(deleted=list(swept_titles))
                else:
                    user_report.diff.deleted = list(swept_titles) + user_report.diff.deleted
            user_report.duration_s = round(time.monotonic() - started, 2)
            # Persist this user's results NOW (before the terminal event that makes the UI refetch), so
            # each person appears as they finish rather than the whole roster only at run's end. Never
            # let a persistence hiccup sink the run — the end-of-run persist is the backstop.
            if ctx.on_user_done is not None:
                try:
                    ctx.on_user_done(user, user_report)
                except Exception:
                    logger.exception("{}: live-persist of user report failed (will persist at run end)", user.slug)
            # terminal per-user event — without it the UI can only resolve a user
            # when the whole run ends, so finished users kept spinning for minutes
            if user_report.status == "error":
                _emit(ctx, user.slug, "error", {"seconds": int(user_report.duration_s)})
            elif user_report.status in ("skipped", "pending"):
                _emit(ctx, user.slug, "skipped", {})
            else:  # ok | cold_start
                _emit(
                    ctx,
                    user.slug,
                    "done",
                    {"picks": len(user_report.picks or []), "seconds": int(user_report.duration_s)},
                )
        return user, user_report, delivered

    # Users are independent, so their READ + LLM work overlaps across a bounded pool while every Plex
    # and plex.tv WRITE is serialized inside _run_user by ctx.write_lock — the leak-safe ordering is
    # untouched. `pool.map` preserves order, so report.users and to_promote read exactly as they would
    # sequentially; concurrency=1 (the default) skips the pool entirely and stays fully sequential.
    if ctx.concurrency > 1 and len(users) > 1:
        with ThreadPoolExecutor(max_workers=ctx.concurrency) as pool:
            results = list(pool.map(process, users))
    else:
        results = [process(user) for user in users]
    for user, user_report, delivered in results:
        report.users.append(user_report)
        if delivered:
            to_promote.append(user)

    # Shared "popular on this server" rows: built once from aggregate history, delivered UNPROMOTED
    # like the per-person rows so promotion still happens only after the filters are merged.
    shared_to_promote: list[tuple[RowSpec, UserProfile]] = []
    shared_specs = [s for s in ctx.config.shared_rows() if ctx.config.should_build(s)] if users else []
    for spec in shared_specs:
        _shared_report, agg = rows._run_shared(
            ctx, spec, users, seed_index, library_index, stored_labels, report, order_work
        )
        if agg is not None:
            shared_to_promote.append((spec, agg))
    return to_promote, shared_to_promote


def _privacy_sync_phase(
    ctx: EngineContext, users: list[UserProfile], stored_labels: dict[str, str], report: RunReport
) -> bool | None:
    """Merge Shortlist's excludes into every share filter. Returns whether promotion may proceed, or
    None when the plex.tv roster could not be read (the run must abort — nothing promoted)."""
    # The PMS is the source of truth for which rows exist, so ask it again before writing any
    # share filter. Whatever happened above — a delivery that failed half-way, a crash, a row left
    # by an older version — every row that EXISTS must be excluded on every other user's share.
    # A row missing from this map is a row nobody's filter hides.
    #
    # Delivery keeps the collections cache warm (append-on-create) for speed, so force a FRESH PMS
    # read here: this privacy-critical enumeration must not depend on the in-process cache being a
    # complete mirror of the server — it reads the server itself, unconditionally.
    sync_failed = False
    if not ctx.config.dry_run:
        try:
            ctx.plex.invalidate_collections_cache()
            stored_labels.update(
                {slug: row.label for slug, row in ctx.plex.owned_collections(ctx.config.label_prefix).items()}
            )
        except Exception as e:
            # We can no longer enumerate what exists, so we cannot promise the filters cover it.
            # Sync with what we know, but promote nothing: an unpromoted row is not on anyone's
            # Home screen, and the next run will put this right.
            sync_failed = True
            report.error = f"could not re-read collections before the privacy sync: {type(e).__name__}: {e}"
            logger.exception("could not re-read collections before the privacy sync — nothing will be promoted")

    # Sync EVERY account that shares this server — not the users we happened to process.
    #
    # A row is visible to anyone whose share filter doesn't exclude it. Plex does not care that we
    # consider its owner "not enabled in Shortlist" or "not in tonight's run". Syncing only the
    # processed users is how, on a live server, 45 of 48 accounts ended up able to see three other
    # people's private rows: only the three Shortlist managed had excludes written at all. It is also
    # why a single-user run (building just one person's row) used to mint a row that nobody's filter
    # hid — the other accounts never had an exclude written for it.
    #
    # We ask plex.tv who can see the server rather than trusting our own user table, because the
    # audience is Plex's fact, not ours.
    try:
        roster = {remote.id: remote for remote in ctx.plextv.list_users()}
    except Exception as e:
        # The sweep above has already DELETED rows. Returning (rather than letting this escape) is
        # what keeps those deletions in the audit trail (rule 10).
        report.error = f"could not read the plex.tv user list: {type(e).__name__}: {e}"
        report.finished_at = datetime.now(UTC)
        logger.exception("could not read the plex.tv user list — no filters written, nothing promoted")
        return None

    # Whose row is whose, by ACCOUNT ID. Never by name: people rename themselves, and two display
    # names can slugify to the same string — either would quietly hand one account another's row.
    # The profiles the adapter handed us are authoritative (their slug is the one in Shortlist's own
    # records); `known_slugs` covers everyone else Shortlist knows but isn't processing tonight. An
    # account in neither owns no row, and is therefore excluded from every one of them.
    own_slugs = {**ctx.known_slugs, **{u.plex_account_id: u.slug for u in users}}

    audience = _server_audience(users, roster, own_slugs)
    # Every CONFIGURED shared row: label -> its audience (None = public, seen by all). This is the
    # authoritative "what is a shared row", so the exclusion classifies by config, never by the
    # label string — a private row is never mistaken for a shared one, and a stale shared collection
    # not in the config is excluded (hidden) rather than treated as public.
    shared_labels = shared_label_audiences(ctx.config)
    reports = {r.slug: r for r in report.users}
    # account_id -> {field: expected} for every write this run, verified in ONE roster read after the
    # loop (below) instead of a full GET /api/users per write (which was O(A²) on a change night).
    to_verify: dict[int, dict[str, str]] = {}
    for user in audience:
        user_report = reports.get(user.slug)
        try:
            own_slug = own_slugs.get(user.plex_account_id)
            written = sync_user_restrictions(
                ctx.plextv,
                user,
                roster.get(user.plex_account_id),  # .get: a user Shortlist knows may be off the share
                stored_labels,
                ctx.snapshots,
                own_label=stored_labels.get(own_slug) if own_slug else None,
                label_prefix=ctx.config.label_prefix,
                shared_labels=shared_labels,
                dry_run=ctx.config.dry_run,
            )
            if written:
                # Every share we touch, audited by account id — most of these accounts have no
                # UserRunReport to record it on (rule 10).
                report.filter_writes[user.plex_account_id] = {"username": user.username, "fields": written}
                if not ctx.config.dry_run:
                    # {field: expected merged value} — read back once, after every write, below.
                    to_verify[user.plex_account_id] = {field: after for field, (_before, after) in written.items()}
            if user_report is not None:
                user_report.privacy_synced = bool(written)
        except Exception as e:
            # One user's filter not being written means the rows are not private. Nothing gets
            # promoted this run — including for users whose own sync succeeded.
            sync_failed = True
            message = f"privacy sync for {user.username}: {type(e).__name__}: {e}"
            if user_report is not None:
                user_report.status = "error"
                user_report.error = f"{user_report.error} | {message}" if user_report.error else message
            else:
                report.error = f"{report.error} | {message}" if report.error else message
            logger.exception("{}: privacy sync failed", user.username)

    # Verify every filter write persisted — ONCE, with a single fresh roster read, strictly before any
    # promotion (the caller promotes only when this returns True). A missing shortlist exclude means a
    # row would be visible to someone it shouldn't, so it fails the whole sync (blocks promotion) exactly
    # as the old per-user read-back-and-raise did — just without a full roster fetch per account.
    if to_verify and not sync_failed:
        try:
            fresh = {r.id: r for r in ctx.plextv.list_users()}
        except Exception as e:
            sync_failed = True
            note = f"could not verify filters: {type(e).__name__}"
            report.error = f"{report.error} | {note}" if report.error else note
            logger.exception("could not read the plex.tv roster to verify filter writes — nothing promoted")
        else:
            for account_id, expected_fields in to_verify.items():
                remote2 = fresh.get(account_id)
                for fieldname, expected in expected_fields.items():
                    got = remote2.filters[fieldname] if remote2 is not None else ""
                    missing = shortlist_labels_in(expected, ctx.config.label_prefix) - shortlist_labels_in(
                        got, ctx.config.label_prefix
                    )
                    if missing:
                        sync_failed = True
                        msg = f"read-back missing excludes {missing} on {fieldname} for account {account_id}"
                        stamp = reports.get(own_slugs.get(account_id, ""))
                        if stamp is not None:
                            stamp.status = "error"
                            stamp.error = f"{stamp.error} | {msg}" if stamp.error else msg
                        else:
                            report.error = f"{report.error} | {msg}" if report.error else msg
                        logger.error("privacy verify: {}", msg)

    return not sync_failed


def _promote_phase(
    ctx: EngineContext,
    to_promote: list[UserProfile],
    shared_to_promote: list[tuple[RowSpec, UserProfile]],
    filters_ok: bool,
    report: RunReport,
) -> None:
    """Promote delivered rows onto shared Home — never before the excludes that hide them exist.

    Promotion runs across EVERY delivery library, not just one per type: promote() is the only call
    that hides a collection from that library's normal browse view (modeUpdate), and a row can now be
    delivered into any library (library_keys). A row promoted in only the lowest-key library would sit
    unhidden — and browse-visible to everyone — in whatever other library it actually landed in."""
    for user in to_promote:
        user_report = next(r for r in report.users if r.slug == user.slug)
        if ctx.config.dry_run:
            logger.info("[dry-run] {}: would promote row to shared Home", user.username)
            continue
        if not filters_ok:
            logger.warning("{}: promotion skipped — a privacy sync failed this run", user.username)
            continue
        # Which row produced each of this user's collections, so promotion honours that row's
        # placement (Home / Library) and pin-to-top. Keyed by the exact title delivery wrote and
        # recorded per library during this user's run (a {top_seed} title differs per library).
        spec_by_slug = {spec.slug: spec for spec in ctx.config.rows}
        placements = {
            title: spec_by_slug[slug] for title, slug in user_report.placement_titles.items() if slug in spec_by_slug
        }
        # Fallback for rows that EXIST but got no picks this run (so they're absent from
        # placement_titles): a STATIC-titled row's title is stable, so map it to its spec by that title
        # — otherwise _promote_one would fall to the everywhere-visible default and yank a "Library
        # only" row onto Home for this one run. Dynamic ({top_seed}) titles can't be predicted without
        # picks, so those keep the safe hide-everywhere fallback. resolve_row_template is the shared
        # source of truth for the template precedence delivery also uses — they must not drift.
        marker = row_marker(user.plex_account_id)
        for spec in ctx.config.rows:
            if spec.shared or (spec.audience is not None and user.plex_account_id not in spec.audience):
                continue
            title_template = resolve_row_template(spec, user, ctx.config)
            if "{top_seed}" not in title_template:
                # A {library_name} title differs per library, so map one per library the row targets;
                # setdefault leaves the recorded per-library titles (placement_titles) winning.
                for section in target_sections(ctx.delivery_sections, spec):
                    name = render_row_name(title_template, user, [], library_name=getattr(section, "title", "") or "")
                    placements.setdefault(name + marker, spec)
        try:
            # Every row the user has, in every library — they can have several rows (all sharing
            # their label), and promoting only one would leave the others invisible to the one
            # person meant to see them.
            for section in ctx.delivery_sections:
                for collection in ctx.plex.find_owned_collections(section, user.label):
                    _promote_one(ctx, collection, placements.get(collection.title))
        except Exception as e:
            user_report.status = "error"
            user_report.error = (user_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("{}: promote failed", user.username)

    # Promote the shared rows too — public, so everyone with library access sees them.
    for spec, agg in shared_to_promote if not ctx.config.dry_run and filters_ok else []:
        shared_report = next((r for r in report.users if r.slug == agg.slug), None)
        try:
            for section in ctx.delivery_sections:
                for collection in ctx.plex.find_owned_collections(section, spec.label):
                    _promote_one(ctx, collection, spec)
        except Exception as e:
            if shared_report is not None:
                shared_report.status = "error"
                shared_report.error = (shared_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("shared row '{}': promote failed", spec.slug)


def _promote_one(ctx: EngineContext, collection, spec: RowSpec | None) -> None:
    """Promote one collection with its row's placement. Unmatched (spec is None) falls back to the
    legacy everywhere-visible behaviour, so a title we couldn't map is never left browse-visible."""
    if spec is None:
        ctx.plex.promote(collection, shared=True)
        return
    # A per-person row lands on its owner's Home via `home` and on a shared user's Home via `shared`;
    # setting both from one flag covers owner and friend without the caller knowing which this is.
    ctx.plex.promote(
        collection,
        shared=spec.show_home,
        home=spec.show_home,
        recommended=spec.show_library,
        pin_top=spec.pin_top,
    )


def _apply_order(ctx: EngineContext, report: RunReport, section, anchor, only_titles: set[str] | None) -> None:
    """One best-effort, gated reorder call + its audit. A shelf reorder is cosmetic and privacy-neutral
    (hubs are already promoted and browse-hidden; only position changes), so a failure never fails the
    run — next run re-applies. Only our hubs move; the anchor is read-only (Kometa coexistence)."""
    try:
        with ctx.write_lock:
            result = ctx.plex.order_owned_hubs(
                section,
                label_prefix=ctx.config.label_prefix,
                anchor_title=anchor.anchor_title,
                before=anchor.before,
                to_top=anchor.to_top,
                dry_run=ctx.config.dry_run,
                only_titles=only_titles,
            )
        if result.get("moved") and not result.get("skipped"):
            report.hub_orderings.append({"library": section.title, **result})
    except Exception as e:
        logger.warning("{}: hub ordering failed ({}: {}) — left Plex's order", section.title, type(e).__name__, e)


def _collection_order_phase(ctx: EngineContext, order_work: list[tuple]) -> None:
    """Order each delivered row's items to its ranked list — the expensive one-move-per-item step, run
    ONCE here, AFTER promotion. Best-effort and privacy-neutral: rows are already delivered, hidden and
    promoted, so a slow or unresponsive PMS during ordering degrades only the visual order, never the
    run or the leak-safe guarantee. Serial (never concurrent) so it can't stampede the PMS; a per-row
    failure is logged and skipped, and the next run re-applies the order."""
    if ctx.config.dry_run or not order_work:
        return
    # A user retried after a mid-delivery timeout can append the same collection twice; ordering it
    # twice is harmless (the second pass finds it in order -> 0 moves) but wasteful, so de-dupe by
    # ratingKey, keeping the last (most recent) ranked list for each.
    deduped: dict[int, tuple] = {}
    for collection, wanted_keys in order_work:
        deduped[getattr(collection, "ratingKey", id(collection))] = (collection, wanted_keys)
    total = 0
    for collection, wanted_keys in deduped.values():
        try:
            total += ctx.plex.order_collection(collection, wanted_keys)
        except Exception as e:  # cosmetic — a stall here must never fail an already-delivered run
            title = getattr(collection, "title", "?")
            logger.warning("ordering '{}' failed ({}: {}) — left in delivery order", title, type(e).__name__, e)
    logger.info("ordered {} collection(s), {} move(s) total", len(deduped), total)


def _row_titles_by_slug(report: RunReport) -> dict[str, set[str]]:
    """slug -> the collection TITLES that row was delivered as this run (aggregated across users). The
    only link from a managed hub back to its row is its title (rows share a per-user label, differ by
    title), so this is how the per-row override knows which hubs belong to which row."""
    out: dict[str, set[str]] = {}
    for user_report in report.users:
        for title, slug in user_report.placement_titles.items():
            out.setdefault(slug, set()).add(title)
    return out


def _order_phase(ctx: EngineContext, report: RunReport) -> None:
    """Place each library's Shortlist rows in its Recommended shelf per the configured anchors.

    Each row's effective anchor is its own per-library override (``RowSpec.hub_anchors``) if set, else
    the global default (``EngineConfig.hub_anchors``). When no row in a library overrides, all of that
    library's rows move together to the default (the simple, robust path). When some rows override,
    rows are grouped by their effective anchor and each group moved as a unit."""
    global_anchors = ctx.config.hub_anchors
    any_override = any(spec.hub_anchors for spec in ctx.config.rows)
    if not global_anchors and not any_override:
        return
    titles_by_slug = _row_titles_by_slug(report) if any_override else {}
    for section in ctx.delivery_sections:
        key = str(section.key)
        section_overridden = any(spec.hub_anchors.get(key) for spec in ctx.config.rows)
        if not section_overridden:
            # Global-only: move every owned row to the library default in one call (unchanged path).
            default = global_anchors.get(key)
            if default is not None:
                _apply_order(ctx, report, section, default, only_titles=None)
            continue
        # Some rows override here: group each row by its effective anchor (override, else default).
        groups: dict[tuple[bool, str, bool], set[str]] = {}
        for spec in ctx.config.rows:
            effective = spec.hub_anchors.get(key) or global_anchors.get(key)
            if effective is None:
                continue
            titles = titles_by_slug.get(spec.slug, set())
            if titles:
                grp = (effective.to_top, effective.anchor_title, effective.before)
                groups.setdefault(grp, set()).update(titles)
        for (to_top, anchor_title, before), titles in groups.items():
            anchor = HubAnchor(anchor_title=anchor_title, before=before, to_top=to_top)
            _apply_order(ctx, report, section, anchor, only_titles=titles)


def _request_phase(ctx: EngineContext, requests_on: bool, demand: requests_mod.DemandMap, report: RunReport) -> None:
    """Sonarr/Radarr requests for picks the library lacks — dead LAST, after every Plex write is done.

    It touches no Plex object, and running it here (not before the privacy sync) keeps its "never
    affects visibility" guarantee literally true: a slow or hung download app cannot delay the
    share-filter merge that hides freshly-delivered rows. It runs only on real user runs — a
    no-users run gathers no demand — and respects dry_run itself.
    """
    if requests_on and demand:
        try:
            report.requests = requests_mod.request_missing(
                ctx.config.requests,
                ctx.tmdb,
                demand,
                dry_run=ctx.config.dry_run,
                already_handled=ctx.handled_requests,
            )
        except Exception as e:
            # A wholesale request-pass failure (e.g. building a client) is a footnote, never a run
            # failure — every Plex write already completed above.
            logger.exception("request pass failed — rows are unaffected")
            report.requests = RequestReport(
                outcomes=[RequestOutcome(0, "request pass", MediaType.MOVIE, "error", f"{type(e).__name__}: {e}")]
            )


def _server_audience(processed: list[UserProfile], roster: dict, known_slugs: dict[int, str]) -> list[UserProfile]:
    """Everyone who can see this server — the audience the rows must be hidden from.

    Shortlist's own user list is not the answer: it holds the people we build rows FOR, while the
    people rows must be hidden FROM is every account the server is shared with. A user Shortlist has
    never heard of still sees every row whose label their filter doesn't exclude.

    Accounts are matched by plex ACCOUNT ID, never by name. `known_slugs` is the adapter's durable
    account -> slug map (the server's users table), so a user who renames
    themselves keeps the slug their row's label was built from. Rebuilding the profile from their
    current username instead would hand them a different slug, and `desired_excludes` would then
    decide their own row belonged to someone else and hide it from them.

    The owner is included here and skipped by `sync_user_restrictions` (Plex cannot restrict the
    owner — rule 5).
    """
    known = {u.plex_account_id: u for u in processed}
    audience = list(processed)
    for account_id, remote in roster.items():
        if account_id in known:
            continue
        audience.append(
            UserProfile(
                username=remote.username,
                plex_account_id=account_id,
                user_type=remote.user_type,
                # The slug Shortlist already gave this account — NOT one derived from their current
                # name. Empty for an account Shortlist has never seen, in which case UserProfile
                # derives one; that is safe precisely because such an account owns no row for the
                # derived slug to be wrong about.
                slug=known_slugs.get(account_id, ""),
            )
        )
    return audience
