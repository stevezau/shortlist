"""Run orchestration: the leak-safe ordering of an engine run.

``run()`` reads top to bottom as the ordered sequence of phases it is: build the library indexes,
sweep unhidable rows, deliver every row UNPROMOTED, merge every share filter, promote, then request.
Row construction itself lives in ``rows.py``; this module owns only the ordering and the privacy
guarantees that depend on it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger

import shortlist.engine.rows as rows
from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.clients.trakt import TraktClient
from shortlist.engine.curator import Curator
from shortlist.engine.delivery import row_marker, sweep_broken_rows
from shortlist.engine.history import HistorySource
from shortlist.engine.models import (
    CollectionDiff,
    EngineConfig,
    MediaType,
    RequestOutcome,
    RequestReport,
    RowSpec,
    RunReport,
    UserProfile,
    UserRunReport,
)
from shortlist.engine.privacy import SnapshotStore, shared_label_audiences, sync_user_restrictions


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
    # Optional 'related titles' candidate source; None when no Trakt key is configured.
    trakt: TraktClient | None = None
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
    # by a later auto-send. Empty for the CLI, which has no inbox.
    handled_requests: set[tuple[int, str]] = field(default_factory=set)
    progress: Callable[[str, str, dict], None] | None = None  # (user_slug, stage, counts) -> None
    # Day number of this run (date.toordinal()), the phase for freshness rotation so a row shifts
    # day to day but is reproducible within a day. Set at the start of run(); 0 disables rotation.
    run_day: int = 0


def _emit(ctx: EngineContext, slug: str, stage: str, counts: dict) -> None:
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
    targets = ctx.plex.sections_by_type()
    seed_index, library_index = _build_indexes(ctx, users, sections, targets)
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

    # Deliver every per-person and shared row UNPROMOTED — nothing is on anyone's Home yet.
    to_promote, shared_to_promote = _deliver_phase(
        ctx, users, seed_index, library_index, stored_labels, report, demand if requests_on else None
    )

    # Merge the excludes into every share filter BEFORE anything is promoted.
    filters_ok = _privacy_sync_phase(ctx, users, stored_labels, report)
    if filters_ok is None:
        # The plex.tv roster could not be read — no filters written, nothing promoted. The sweep
        # above already deleted rows, and the report (already populated) keeps that audit (rule 10).
        return report

    # Only now, with the exclusions in place, promote rows onto shared Home.
    _promote_phase(ctx, to_promote, shared_to_promote, filters_ok, report)

    # Sonarr/Radarr requests, dead LAST — after every Plex write is done.
    _request_phase(ctx, requests_on, demand, report)

    report.finished_at = datetime.now(UTC)
    ok = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    logger.info("run complete: {}/{} users ok (dry_run={})", ok, len(report.users), ctx.config.dry_run)
    return report


def _build_indexes(
    ctx: EngineContext, users: list[UserProfile], sections: list, targets: dict
) -> tuple[dict[int, int], dict[MediaType, dict[int, int]]]:
    """Build the library indexes a run reads from.

    Three indexes, because they answer different questions.

    `seed_index` (ratingKey -> tmdb_id, across EVERY library) turns what a user WATCHED into a TMDB
    id, and people watch films in "4K Movies" too. It is keyed by ratingKey, not by tmdb_id, because
    that is the direction it is READ in: the same film in two movie libraries is ONE tmdb id and TWO
    ratingKeys, so a tmdb-keyed index would keep only the last library scanned — and every watch in
    the other library would resolve to nothing, leaving that user seedless with an empty row and a
    run that still reported success.

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
    # Only when there is someone to recommend to. The indexes walk every item in every library, and
    # are read only inside _run_user — so with no users this is thousands of PMS reads thrown away,
    # in front of the sweep, on the one path (a closed gate) where the sweep is the entire point and
    # must not be preceded by anything that can fail.
    for section in sections if users else []:
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        # Named by library so a stuck scan shows WHICH library it's on (a slow PMS retries per call).
        _emit(ctx, section.title, "indexing", {})
        # Capture episode counts only for show libraries — movies have no leafCount to speak of.
        index = ctx.plex.build_library_index(section, episode_counts if kind is MediaType.SHOW else None)
        _emit(ctx, section.title, "indexed", {"items": len(index)})
        seed_index.update({rating_key: tmdb_id for tmdb_id, rating_key in index.items()})
        # Every library of a deliverable type is both a recommendation source (union) and a possible
        # delivery target (its own per-section index) — a row picks which ones under library_keys.
        library_index[kind].update(index)
        section_index[section.key] = index
    ctx.section_index = section_index
    ctx.episode_counts = episode_counts
    ctx.delivery_sections = list(sections) if users else []
    # The AI-from-library source needs titles/genres. Built when ANY row wants it — not just the
    # global setting: a row overriding its sources to llm_library found an empty catalog and
    # produced nothing, forever, while reporting ok. And built from EVERY library, not one
    # representative per type, or a row pinned to "4K Movies" would be offered the "Movies" catalog.
    if users and _wants_library_catalog(ctx.config):
        catalog: dict[MediaType, list[dict]] = {MediaType.MOVIE: [], MediaType.SHOW: []}
        seen: dict[MediaType, set[int]] = {MediaType.MOVIE: set(), MediaType.SHOW: set()}
        for section in sections:
            kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
            _emit(ctx, section.title, "cataloguing", {})
            items = ctx.plex.build_library_catalog(section)
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
) -> tuple[list[UserProfile], list[tuple[RowSpec, UserProfile]]]:
    """Deliver every per-person and shared row, all UNPROMOTED. Returns the promotion candidates."""
    to_promote: list[UserProfile] = []
    for user in users:
        user_report = UserRunReport(username=user.username, slug=user.slug)
        # A row swept for this user is part of their story this run — but the swept dict is the
        # run-level record, so a paused user's deletion is never lost just because they have no
        # UserRunReport.
        swept_titles = report.swept_rows.get(user.slug, [])
        report.users.append(user_report)
        started = time.monotonic()
        try:
            delivered = rows._run_user(ctx, user, seed_index, library_index, stored_labels, user_report, demand)
            if delivered:
                to_promote.append(user)
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

    # Shared "popular on this server" rows: built once from aggregate history, delivered UNPROMOTED
    # like the per-person rows so promotion still happens only after the filters are merged.
    shared_to_promote: list[tuple[RowSpec, UserProfile]] = []
    for spec in ctx.config.shared_rows() if users else []:
        _shared_report, agg = rows._run_shared(ctx, spec, users, seed_index, library_index, stored_labels, report)
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
    sync_failed = False
    if not ctx.config.dry_run:
        try:
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
    # why `shortlist run --user <slug>` — the documented rollout command — used to mint a row that
    # nobody's filter hid.
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


def _request_phase(ctx: EngineContext, requests_on: bool, demand: requests_mod.DemandMap, report: RunReport) -> None:
    """Sonarr/Radarr requests for picks the library lacks — dead LAST, after every Plex write is done.

    It touches no Plex object, and running it here (not before the privacy sync) keeps its "never
    affects visibility" guarantee literally true: a slow or hung download app cannot delay the
    share-filter merge that hides freshly-delivered rows. It runs only on real user runs — the
    gated remedy pass (no users) gathered no demand — and respects dry_run itself.
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
    account -> slug map (the server's users table; the CLI's slugs.json), so a user who renames
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
