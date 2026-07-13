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

import rowarr.engine.rows as rows
from rowarr.engine import requests as requests_mod
from rowarr.engine.clients.plex_pms import PlexClient
from rowarr.engine.clients.plextv import PlexTvClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import Curator
from rowarr.engine.delivery import row_marker, sweep_broken_rows
from rowarr.engine.history import HistorySource
from rowarr.engine.models import (
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
    # slug -> {(tmdb_id, media_type)}: the staleness guard. Keyed on the PAIR because TMDB ids
    # are unique only within a namespace — movie 550 and TV 550 are different titles.
    recent_picks: dict[str, set[tuple[int, MediaType]]] = field(default_factory=dict)
    # plex account id -> the slug Rowarr assigned that account, for EVERY user it knows (not just
    # tonight's). This is how "whose row is this?" is answered. It cannot be answered from a name:
    # people rename themselves, and two display names can slugify to the same string — either
    # would silently hand one account another's row.
    known_slugs: dict[int, str] = field(default_factory=dict)
    progress: Callable[[str, str, dict], None] | None = None  # (user_slug, stage, counts) -> None


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

    sections = ctx.plex.sections()
    targets = ctx.plex.sections_by_type()
    seed_index, library_index = _build_indexes(ctx, users, sections, targets)

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
    _promote_phase(ctx, targets, to_promote, shared_to_promote, filters_ok, report)

    # Sonarr/Radarr requests, dead LAST — after every Plex write is done.
    _request_phase(ctx, requests_on, demand, report)

    report.finished_at = datetime.now(UTC)
    ok = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    logger.info("run complete: {}/{} users ok (dry_run={})", ok, len(report.users), ctx.config.dry_run)
    return report


def _build_indexes(
    ctx: EngineContext, users: list[UserProfile], sections: list, targets: dict
) -> tuple[dict[MediaType, dict[int, int]], dict[MediaType, dict[int, int]]]:
    """Build the two library indexes a run reads from.

    Two indexes, because they answer different questions.

    `seed_index` covers EVERY library: it turns what a user WATCHED into a TMDB id, and people
    watch films in "4K Movies" too. Narrowing it would silently give those users no seeds, no
    candidates, and an empty row — while the run still reported success.

    `library_index` covers only the libraries we deliver to: it decides what may be RECOMMENDED,
    and a pick from a library that never gets a collection could never be shown to anyone.
    """
    target_keys = {section.key for section in targets.values()}
    seed_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    library_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    # Only when there is someone to recommend to. Both indexes walk every item in every library,
    # and both are read only inside _run_user — so with no users this is thousands of PMS reads
    # thrown away, in front of the sweep, on the one path (a closed gate) where the sweep is the
    # entire point and must not be preceded by anything that can fail.
    for section in sections if users else []:
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        index = ctx.plex.build_library_index(section)
        seed_index[kind].update(index)
        if section.key in target_keys:
            library_index[kind].update(index)
        else:
            logger.info(
                "library '{}': rows are built in '{}' instead, but watches here still count",
                section.title,
                targets[kind].title,
            )
    return seed_index, library_index


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
    seed_index: dict[MediaType, dict[int, int]],
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
    """Merge Rowarr's excludes into every share filter. Returns whether promotion may proceed, or
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
    # consider its owner "not enabled in Rowarr" or "not in tonight's run". Syncing only the
    # processed users is how, on a live server, 45 of 48 accounts ended up able to see three other
    # people's private rows: only the three Rowarr managed had excludes written at all. It is also
    # why `rowarr run --user <slug>` — the documented rollout command — used to mint a row that
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
    # The profiles the adapter handed us are authoritative (their slug is the one in Rowarr's own
    # records); `known_slugs` covers everyone else Rowarr knows but isn't processing tonight. An
    # account in neither owns no row, and is therefore excluded from every one of them.
    own_slugs = {**ctx.known_slugs, **{u.plex_account_id: u.slug for u in users}}

    audience = _server_audience(users, roster, own_slugs)
    # Every CONFIGURED shared row: label -> its audience (None = public, seen by all). This is the
    # authoritative "what is a shared row", so the exclusion classifies by config, never by the
    # label string — a private row is never mistaken for a shared one, and a stale shared collection
    # not in the config is excluded (hidden) rather than treated as public.
    shared_labels = {spec.label.lower(): spec.audience for spec in ctx.config.shared_rows() if spec.label}
    reports = {r.slug: r for r in report.users}
    for user in audience:
        user_report = reports.get(user.slug)
        try:
            own_slug = own_slugs.get(user.plex_account_id)
            written = sync_user_restrictions(
                ctx.plextv,
                user,
                roster.get(user.plex_account_id),  # .get: a user Rowarr knows may be off the share
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
    targets: dict,
    to_promote: list[UserProfile],
    shared_to_promote: list[tuple[RowSpec, UserProfile]],
    filters_ok: bool,
    report: RunReport,
) -> None:
    """Promote delivered rows onto shared Home — never before the excludes that hide them exist."""
    for user in to_promote:
        user_report = next(r for r in report.users if r.slug == user.slug)
        if ctx.config.dry_run:
            logger.info("[dry-run] {}: would promote row to shared Home", user.username)
            continue
        if not filters_ok:
            logger.warning("{}: promotion skipped — a privacy sync failed this run", user.username)
            continue
        try:
            # Every row the user has, in every library — they can have several rows (all sharing
            # their label), and promoting only one would leave the others invisible to the one
            # person meant to see them.
            for section in targets.values():
                for collection in ctx.plex.find_owned_collections(section, user.label):
                    ctx.plex.promote(collection, shared=True)
        except Exception as e:
            user_report.status = "error"
            user_report.error = (user_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("{}: promote failed", user.username)

    # Promote the shared rows too — public, so everyone with library access sees them.
    for spec, agg in shared_to_promote if not ctx.config.dry_run and filters_ok else []:
        shared_report = next((r for r in report.users if r.slug == agg.slug), None)
        try:
            for section in targets.values():
                for collection in ctx.plex.find_owned_collections(section, spec.label):
                    ctx.plex.promote(collection, shared=True)
        except Exception as e:
            if shared_report is not None:
                shared_report.status = "error"
                shared_report.error = (shared_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("shared row '{}': promote failed", spec.slug)


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
                ctx.config.requests, ctx.tmdb, demand, dry_run=ctx.config.dry_run
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

    Rowarr's own user list is not the answer: it holds the people we build rows FOR, while the
    people rows must be hidden FROM is every account the server is shared with. A user Rowarr has
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
                # The slug Rowarr already gave this account — NOT one derived from their current
                # name. Empty for an account Rowarr has never seen, in which case UserProfile
                # derives one; that is safe precisely because such an account owns no row for the
                # derived slug to be wrong about.
                slug=known_slugs.get(account_id, ""),
            )
        )
    return audience
