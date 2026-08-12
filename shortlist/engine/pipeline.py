"""Run orchestration: the leak-safe ordering of an engine run.

``run()`` reads top to bottom as the ordered sequence of phases it is: build the library indexes,
sweep unhidable rows, deliver every row UNPROMOTED, merge every share filter, promote, then request.
Row construction itself lives in ``rows.py``; this module owns only the ordering and the privacy
guarantees that depend on it.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

from loguru import logger

import shortlist.engine.rows as rows
from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.plextv import FilterWriteRefused
from shortlist.engine.context import EngineContext, _emit
from shortlist.engine.delivery import (
    render_row_name,
    resolve_row_template,
    row_marker,
    sweep_broken_rows,
    target_sections,
)
from shortlist.engine.models import (
    LABEL_PREFIX,
    SHARED_LABEL_PREFIX,
    CollectionDiff,
    HubAnchor,
    MediaType,
    OwnedRow,
    RequestOutcome,
    RequestReport,
    RowSpec,
    RunReport,
    UserProfile,
    UserRunReport,
    UserType,
)
from shortlist.engine.privacy import (
    shared_label_audiences,
    shortlist_labels_in,
    sync_user_restrictions,
    unhidden_rows_visible_to,
)


def run(ctx: EngineContext, users: list[UserProfile]) -> RunReport:
    """Run the pipeline for every enabled user. Users are independent — one failure never
    stops the run (per-user try/except; plex-safety rule 6 resume-safety).

    Write ordering is leak-safe: rows are created/updated UNPROMOTED, then every user's
    share filters are merged, and only then are rows promoted onto shared Home — so a new
    collection is never visible to anyone before the exclusions that hide it exist.
    """
    report = RunReport(started_at=datetime.now(UTC), dry_run=ctx.config.dry_run)
    # The header a log reader needs BEFORE anything else: a run that dies in the index build, or is
    # scoped to nobody, otherwise leaves no trace of having started at all — only per-user stage
    # lines, of which there would be none. "Did 03:30 fire, and over what?" is answered here.
    logger.info(
        "run starting: {} user(s), dry_run={}, label prefix '{}'",
        len(users),
        ctx.config.dry_run,
        LABEL_PREFIX,
    )
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
    stored_labels = {slug: row.label for slug, row in ctx.plex.owned_collections(LABEL_PREFIX).items()}

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

    # The hinge of the whole run: after this line every per-user card is terminal, and everything
    # that follows is server-wide. Without it the activity feed's last line is whichever person
    # happened to finish last, and the minutes of real work after them look like a hang.
    done = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    _emit(ctx, "Shortlist", "users_done", {"done": done, "total": len(users)})

    # Merge the excludes into every share filter BEFORE anything is promoted.
    _emit(ctx, "Shortlist", "filters", {})
    filters_ok = _privacy_sync_phase(ctx, users, stored_labels, report)
    if filters_ok is None:
        # The plex.tv roster could not be read — no filters written, nothing promoted. The sweep
        # above already deleted rows, and the report (already populated) keeps that audit (rule 10).
        return report

    # Only now, with the exclusions in place, promote rows onto shared Home.
    _emit(ctx, "Shortlist", "promoting", {})
    promoted = _promote_phase(ctx, to_promote, shared_to_promote, filters_ok, report)

    # Converge everything the promote phase could NOT reach. Promotion only ever writes flags for
    # users in tonight's run, so a row belonging to anyone paused, disabled, deselected, errored,
    # cancelled, or simply promoted by an older build keeps its flags forever. This walks the rest
    # and takes them off the owner's Home — the one surface nothing can hide.
    _emit(ctx, "Shortlist", "converging", {})
    # `users=[]` is the privacy-sync shape (rule 1: sweep + merge only). It has no authority to
    # DELETE anyone's collection — it was never given a roster to judge against, and its own contract
    # says it can only ever make the server more private.
    _converge_phase(ctx, promoted, report, may_delete=bool(users))
    _emit(
        ctx,
        "Shortlist",
        "converged",
        {"demoted": len(report.converged or []), "removed": len(report.orphans_removed or [])},
    )

    # Order each row's items (the expensive one-move-per-item step) as a best-effort pass AFTER
    # promotion — rows are already delivered, hidden and live, so a slow PMS here degrades only the
    # ordering, never the run. Privacy-neutral (never touches a label, filter, or promotion).
    # The long one: one PMS round-trip per moved item, minutes on a big server. Silence here is
    # what made a healthy run look wedged.
    _emit(ctx, "Shortlist", "ordering", {"collections": len(order_work)})
    _collection_order_phase(ctx, order_work)

    # Position the just-promoted rows in each library's Recommended shelf (must run after promotion —
    # a hub has to be promoted to be movable). Best-effort and privacy-neutral.
    if filters_ok:
        _emit(ctx, "Shortlist", "shelves", {})
        _order_phase(ctx, report)

    # Sonarr/Radarr requests, dead LAST — after every Plex write is done.
    if requests_on:
        _emit(ctx, "Shortlist", "requesting", {"wanted": len(demand)})
    _request_phase(ctx, requests_on, demand, report)

    report.finished_at = datetime.now(UTC)
    ok = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    failed = sum(1 for u in report.users if u.status == "error")
    elapsed = (report.finished_at - report.started_at).total_seconds()
    # Errors called out separately rather than left as "N/M" arithmetic — "3/40 ok" reads as a
    # disaster when 37 people were simply skipped, and as fine when 37 actually failed.
    logger.info(
        "run complete in {:.0f}s: {} ok, {} failed, {} skipped (dry_run={})",
        elapsed,
        ok,
        failed,
        len(report.users) - ok - failed,
        ctx.config.dry_run,
    )
    # The last line of the feed, so "is it still going?" is answerable without reading the header.
    _emit(
        ctx,
        "Shortlist",
        "finished",
        {"ok": ok, "failed": failed, "seconds": round(elapsed)},
    )
    return report


# The real invalidation is the section SIGNATURE (item count + last-updated): the moment the library
# changes, the key changes and this is bypassed. This TTL is only a backstop for the rare change the
# signature can't see (a 1-for-1 swap that doesn't bump updatedAt); 7 days matches the TMDB/Trakt
# caches — a 1-for-1 swap that never bumps updatedAt is rare enough that a fortnight-scale backstop
# still self-heals it, and the signature catches every real change immediately regardless.
INDEX_CACHE_TTL_S = 7 * 24 * 3600


def _library_index(ctx: EngineContext, section) -> dict[int, int]:
    """This section's ``tmdb_id -> ratingKey`` index — from the cross-run cache when unchanged.

    Keyed on the section + a cheap change signature (item count + last-updated); a signature change
    (a title added/removed/edited) misses and re-scans. JSON object keys are strings, so tmdb ids
    round-trip through ``str()``/``int()``. A missing signature or NullCache just always re-scans.
    """
    signature = ctx.plex.section_signature(section)
    # v2 key: the cached payload dropped the old {"index", "episodes"} envelope for the bare index
    # dict. A new prefix makes any old-format entry a clean miss (re-scan) instead of a parse crash.
    cache_key = f"index2:{section.key}:{signature}" if signature else None
    if cache_key and (cached := ctx.index_cache.get(cache_key)):
        index = {int(k): v for k, v in json.loads(cached).items()}
        _emit(ctx, section.title, "indexed (cached)", {"items": len(index)})
        return index
    _emit(ctx, section.title, "indexing", {})
    index = ctx.plex.build_library_index(section)
    if cache_key:
        ctx.index_cache.set(cache_key, json.dumps({str(k): v for k, v in index.items()}), INDEX_CACHE_TTL_S)
    _emit(ctx, section.title, "indexed", {"items": len(index)})
    return index


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
    # Read only the libraries some row targets (media type + library_keys). Retired rows are included
    # so a disabled row is still curated/indexed where it lives; the mute/retire CLEANUP scans every
    # library independently (rows._remove_muted_and_retired), and the leak sweep covers everything else
    # independently too. A library no row uses (e.g. a Sports library) is skipped entirely.
    #
    # The EFFECTIVE specs — per_person_rows() synthesizes the legacy default row when rows aren't
    # managed, so an unconfigured run still reads every library (it targets them all).
    wanted_keys = {
        str(section.key)
        for spec in (*ctx.config.per_person_rows(), *ctx.config.shared_rows(), *ctx.config.retired_rows)
        for section in target_sections(sections, spec)
    }
    # WHICH libraries rows live in, and WHETHER to walk their contents, are two different questions.
    # This used to answer both with one list, so a run with no users — `engine_run(ctx, [])`, i.e. every
    # `privacy.sync` — got `delivery_sections = []` and the shelf-ordering phase then iterated nothing
    # at all. That is the second, independent reason the SFLIX shelf could never be repaired by a job:
    # even with the right rows to move, there were no libraries to move them in (2026-08-12).
    # Naming the sections is pure in-memory filtering of a list we already hold; it is the INDEXING
    # below that costs thousands of PMS reads, and that is what stays gated on there being users.
    ctx.delivery_sections = [section for section in sections if str(section.key) in wanted_keys]
    # Only when there is someone to recommend to. The indexes are read only inside _run_user, so with
    # no users they are thousands of PMS reads thrown away, in front of the sweep, on the one path (a
    # closed gate) where the sweep is the entire point and must not be preceded by anything that can fail.
    index_sections = ctx.delivery_sections if users else []
    for section in index_sections:
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        index = _library_index(ctx, section)
        seed_index.update({rating_key: tmdb_id for tmdb_id, rating_key in index.items()})
        # Every library of a deliverable type is both a recommendation source (union) and a possible
        # delivery target (its own per-section index) — a row picks which ones under library_keys.
        library_index[kind].update(index)
        section_index[section.key] = index
    ctx.section_index = section_index
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
        # Cancelled: skip this user's gather/rank/delivery entirely. Checked here (not mid-user) so
        # a user already in flight finishes cleanly — per-user transactionality means a cancel never
        # leaves a half-applied user. Queued users fall straight through as "skipped".
        if ctx.cancelled():
            user_report.status = "skipped"
            user_report.reason = "The run was cancelled before this person's turn."
            return user, user_report, False
        # A row swept for this user is part of their story this run — but the swept dict is the
        # run-level record, so a paused user's deletion is never lost just because they have no
        # UserRunReport.
        swept_titles = report.swept_rows.get(user.slug, [])
        started = time.monotonic()
        delivered = False
        try:
            # PMS timeouts are retried at the DELIVERY write (idempotent) inside _run_user, so a Plex
            # hiccup no longer re-runs the whole user's gather + ranking (which is what made a slow
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
                # Carry the reason on the live event too — the wizard's first-run screen is where a
                # new owner meets "skipped", and a bare word there reads as a failure (issue #3).
                _emit(ctx, user.slug, "skipped", {}, user_report.reason)
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
    # A cancelled run stops here too — no new shared rows once the user asked to stop. Nor does a
    # run scoped to particular people: their overlap is not "popular on this server", and publishing
    # it to everyone would let a hand-picked subset shape a public row. Shared rows rebuild on the
    # next full run, exactly like a row that's out of scope for a per-row scheduled run.
    build_shared = bool(users) and not ctx.cancelled() and not ctx.config.users_scoped
    shared_specs = [s for s in ctx.config.shared_rows() if ctx.config.should_build(s)] if build_shared else []
    for spec in shared_specs:
        _shared_report, agg = rows._run_shared(
            ctx, spec, users, seed_index, library_index, stored_labels, report, order_work
        )
        if agg is not None:
            shared_to_promote.append((spec, agg))
    return to_promote, shared_to_promote


def _record_unhideable(ctx, user, remote, owned, collections_known, report) -> None:
    """Check what an account we could not write a hide-list for can actually SEE, and record it.

    Only for accounts Plex genuinely refuses (a managed account with a parental profile) — every other
    empty write is either a no-op or already handled as a blocker upstream.

    Deliberately NOT a promotion blocker. Nothing we can do would hide these rows, so stopping the run
    would punish every other user on the server for one account's Plex settings, permanently. The
    honest response is to tell the owner, who can set that account's Restriction Profile to None (the
    path Plex's own documentation gives) or disable it in Shortlist.

    Never raises: this is a diagnostic on top of a sync that already succeeded, and a failed read here
    must not fail the run. A read that fails is simply not reported as clean.

    `owned` is this phase's own FRESH PMS enumeration, not `ctx.delivered_keys`. The ledger is loaded
    when the context is built, so on a first run it holds nothing and every account would measure as
    clean — silence indistinguishable from the bug.
    """
    if ctx.pms_for_user is None or remote is None or not getattr(remote, "restriction_profile", ""):
        return
    if not collections_known or not owned:
        # We could not establish which rows exist, so "sees none of ours" would be an artefact of the
        # failed read, not a finding — and it would be PERSISTED, clearing last night's real one from
        # the badge and the alert. The docstring of `unhidden_rows_visible_to` refuses exactly this
        # (plex-safety rule 4); honouring it means not calling it at all here.
        logger.warning(
            "{}: could not check what this '{}' account can see — the collections read did not "
            "produce a usable picture, so this run reports nothing rather than a false all-clear",
            user.username,
            remote.restriction_profile,
        )
        return
    try:
        as_them = ctx.pms_for_user(user)
        if as_them is None:
            return
        exposed = unhidden_rows_visible_to(as_them, owned, user.slug)
    except Exception as e:
        logger.warning("{}: could not check what this account can see ({})", user.username, type(e).__name__)
        return
    if not exposed:
        logger.debug("{}: '{}' account, and it sees none of our rows", user.username, remote.restriction_profile)
        return
    report.unhideable_rows[user.username] = exposed
    logger.warning(
        "{}: Plex refuses a hide-list for this '{}' account AND it can see {} row(s) that are not "
        "its own — set its Restriction Profile to None (disabling the account in Shortlist does NOT "
        "help: that removes THEIR row, not their view of everyone else's)",
        user.username,
        remote.restriction_profile,
        len(exposed),
    )


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
    # Whether `stored_labels` is a COMPLETE picture of what is on the server. Only a successful fresh
    # enumeration earns it, and only then may a dead shared-row exclude be pruned — "I could not read
    # the collections" and "that collection is gone" are indistinguishable, and one of those readings
    # un-hides a live row.
    collections_known = False
    # Bound BEFORE the try: the enumeration below can raise, and `owned` is read again further down by
    # the unhideable-rows check. Left unbound, that failure path raises NameError inside the privacy
    # phase — turning a handled "could not read the collections" into an unhandled crash.
    owned: dict[str, OwnedRow] = {}
    # Read in a DRY RUN too. The enumeration is read-only, and skipping it made `--dry-run` report the
    # merges while silently omitting the exclude REMOVALS a live run would make — a preview that is
    # missing the only destructive half is worse than no preview (rule 8).
    # The SECOND source `dead_private` needs before it removes another account's private-row exclude:
    # which accounts still have a collection here according to the TITLE MARKER, which is not read
    # through `collection.labels` and so cannot fail the same way (rule 4). None until proven —
    # not knowing must never license a removal. Mapped to slugs once `own_slugs` exists, below.
    marked_accounts: set[int] | None = None
    try:
        ctx.plex.invalidate_collections_cache()
        owned = ctx.plex.owned_collections(LABEL_PREFIX)
        stored_labels.update({slug: row.label for slug, row in owned.items()})
        collections_known = True
        # Same cached listing the enumeration above walked, so this costs no extra PMS reads.
        marked_accounts = ctx.plex.marked_account_ids()
        if not owned:
            # A successful read that returned NOTHING. Correct to carry on — nothing is promoted that
            # shouldn't be, and the prune declines to act on it (privacy.py) — but silent otherwise,
            # and it defers every legitimate un-hide by a cycle. "I added Sarah to the Popular row and
            # she still can't see it" has to be answerable from the log.
            logger.warning(
                "the PMS reported NO shortlist collections — share-filter excludes will be merged but "
                "none removed this pass, since an empty read cannot prove a row is gone"
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
    # Marker evidence, keyed the way the prune compares: slug, not account id. Stays None when the
    # enumeration failed, which is what stops `dead_private` acting on an unknown.
    marker_present_slugs = (
        None if marked_accounts is None else {slug for account, slug in own_slugs.items() if account in marked_accounts}
    )

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
    # One plex.tv write per account, throttled and backing off on a 429 (rule 6) — minutes on a
    # 40-account server. Counted out loud so the feed shows it moving rather than sitting on
    # "filters" for the duration.
    for position, user in enumerate(audience, start=1):
        _emit(ctx, "Shortlist", "filters", {"done": position, "total": len(audience)})
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
                label_prefix=LABEL_PREFIX,
                shared_labels=shared_labels,
                collections_known=collections_known,
                departed_slugs=ctx.departed_slugs,
                marker_present_slugs=marker_present_slugs,
                # A disabled (opted-out) Shortlist account gets even public shared rows hidden, so
                # disabling someone removes them from Shortlist entirely. Non-Shortlist accounts that
                # merely share the server aren't in disabled_account_ids, so they still see public rows.
                hide_all_shared=(
                    ctx.config.hide_shared_from_disabled and user.plex_account_id in ctx.disabled_account_ids
                ),
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
            if not written:
                # Nothing was written for this account. For a profiled managed account that is
                # expected (Plex refuses label filters) — but "expected" was doing too much work: the
                # code assumed such an account sees nothing, so there was nothing to hide. Measured on
                # a real server, an `older_kid` account saw three collections. So find out.
                _record_unhideable(ctx, user, roster.get(user.plex_account_id), owned, collections_known, report)
        except FilterWriteRefused as e:
            # plex.tv permanently refused the write (422). Safe to skip ONLY for an account with a
            # parental PROFILE: Plex declines label restrictions while one is set.
            #
            # It used to add "and such an account sees zero collections anyway, so there is nothing an
            # exclude would have hidden". That is FALSE and `privacy.py` says so with a measurement:
            # true of `little_kid`, not of `older_kid`, one of which listed three collections on a real
            # server (2026-08-11, #76). The sentence mattered because it was the load-bearing
            # justification for skipping an account on a privacy path while promotion proceeds for the
            # whole server. What actually makes the skip acceptable is narrower: nothing we can write
            # would hide these rows, so blocking the run would punish everyone else permanently for one
            # account's Plex settings. So skip, but MEASURE — `_record_unhideable` below reports what
            # the account can really see, which is the only honest version of "expected".
            #
            # Keyed on the profile, NOT on `restricted` — plex.tv reports that for every Plex Home
            # account, profile or not. Since privacy.py now attempts the write for profile-less managed
            # accounts, they reach this handler for the first time; treating their 422 as a known-safe
            # skip would let the run promote every private row while that account holds no excludes at
            # all. That is #20's leak, re-opened, with the check that should catch it turned off.
            # `not profile_known` is the "we could not find out" case, kept separate from "no
            # profile". Both look like an empty string otherwise — so a permanent `/api/home/users`
            # outage would make every profiled account an unknown failure and block promotion for the
            # entire server, nightly, until someone disabled those users by hand (#14's shape again).
            # When the profile is unknown, fall back to the pre-#20 behaviour and trust `restricted`.
            #
            # Asked PER ACCOUNT: a 200 carrying an empty or partial roster is not knowledge about
            # somebody it never mentioned, and treating it as such re-created the very server-wide
            # block this guard exists to prevent.
            remote_user = roster.get(user.plex_account_id)
            profiles_known = ctx.plextv.home_profile_known(user.plex_account_id)
            if remote_user and (remote_user.restriction_profile or (not profiles_known and remote_user.restricted)):
                logger.warning(
                    "{}: plex.tv refused the filter write for a '{}' account — expected, skipping",
                    user.username,
                    remote_user.restriction_profile or "restricted, profile unknown",
                )
                # Measured here too, not just on the `not written` path above. The reachable cell is
                # `restricted=0` with a profile set — the endpoint-disagreement case privacy.py
                # deliberately allows through — which lands here instead, and was the one skip in the
                # phase that reported nothing at all. (A profile-unknown account early-returns inside
                # `_record_unhideable`, so this costs nothing for the other arm.)
                _record_unhideable(ctx, user, remote_user, owned, collections_known, report)
            else:
                sync_failed = True
                report.promotion_blockers.append(f"{user.username} (plex account {user.plex_account_id}): {e}")
                logger.error(
                    "{}: plex.tv 422 on an account with NO parental profile — blocking promotion, "
                    "because nothing else would stop their rows going public",
                    user.username,
                )
        except Exception as e:
            # One user's filter not being written means the rows are not private. Nothing gets
            # promoted this run — including for users whose own sync succeeded.
            sync_failed = True
            message = f"privacy sync for {user.username}: {type(e).__name__}: {e}"
            # Named, not just counted: this is the reason nothing gets promoted, so it has to reach
            # the operator's screen rather than only the container log.
            report.promotion_blockers.append(f"{user.username} (plex account {user.plex_account_id}): {e}")
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
                    missing = shortlist_labels_in(expected, LABEL_PREFIX) - shortlist_labels_in(got, LABEL_PREFIX)
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
) -> set[int]:
    """Promote delivered rows onto shared Home — never before the excludes that hide them exist.

    Promotion runs across EVERY delivery library, not just one per type: promote() is the only call
    that hides a collection from that library's normal browse view (modeUpdate), and a row can now be
    delivered into any library (library_keys). A row promoted in only the lowest-key library would sit
    unhidden — and browse-visible to everyone — in whatever other library it actually landed in.

    Returns the ratingKeys actually promoted, so the converge phase can tell "this row was set
    correctly tonight" from "nothing has touched this row in weeks" and only walk the remainder.
    A collection skipped by an exception mid-loop is correctly absent, so converge picks it up."""
    promoted: set[int] = set()
    for position, user in enumerate(to_promote, start=1):
        _emit(ctx, "Shortlist", "promoting", {"done": position, "total": len(to_promote)})
        user_report = next((r for r in report.users if r.slug == user.slug), None)
        if ctx.config.dry_run:
            logger.info("[dry-run] {}: would promote row to shared Home", user.username)
            continue
        if not filters_ok:
            # Say WHICH account blocked it and why — "a privacy sync failed this run" on its own
            # explains nothing and is exactly what sent a beta user digging through logs.
            why = "; ".join(report.promotion_blockers) or (report.error or "the share-filter sync did not complete")
            logger.warning(
                "{}: promotion skipped — the row stays hidden because Plex would not accept a share "
                "filter this run, so it cannot be proven private. Blocked by: {}",
                user.username,
                why,
            )
            continue
        try:
            # `into=promoted`, not `promoted |= ...`: a PMS failure part-way through this user must
            # still leave behind the ratingKeys already promoted, or converge would see them as
            # untouched and demote rows this run had just correctly set.
            promote_user_rows(ctx, user, user_report.placement_titles if user_report else {}, into=promoted)
        except Exception as e:
            if user_report is not None:
                user_report.status = "error"
                user_report.error = (user_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("{}: promote failed", user.username)

    # Promote the shared rows too — public, so everyone with library access sees them.
    for spec, agg in shared_to_promote if not ctx.config.dry_run and filters_ok else []:
        shared_report = next((r for r in report.users if r.slug == agg.slug), None)
        try:
            # Every library, same reason as the per-user loop above: a shared row whose library_keys
            # narrowed leaves its collection in the dropped library, otherwise never revisited.
            for section in ctx.plex.sections():
                for collection in ctx.plex.find_owned_collections(section, spec.label):
                    _promote_one(ctx, collection, spec)
                    promoted.add(int(collection.ratingKey))
        except Exception as e:
            if shared_report is not None:
                shared_report.status = "error"
                shared_report.error = (shared_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("shared row '{}': promote failed", spec.slug)

    return promoted


def promote_user_rows(
    ctx: EngineContext,
    user: UserProfile,
    placement_titles: dict[str, str] | None = None,
    *,
    placement_keys: dict[int, str] | None = None,
    into: set[int] | None = None,
) -> set[int]:
    """Put every collection under one user's label onto the surfaces its row asks for.

    Split out of ``_promote_phase`` so that un-pausing someone can reuse it. A paused user's rows were
    only DEMOTED (the collections and their labels survive, so every other account's exclude still
    matches), which makes restoring them a re-promote rather than a rebuild — and the flags to set are
    exactly the ones a run would have set.

    Which row a collection belongs to is answered from two sources, and IDENTITY WINS:

    * ``placement_keys`` — {Plex ratingKey -> row slug}, from the delivery ledger. Authoritative, and
      the only thing that works for a ``{top_seed}`` row, whose title is different every run and so
      matches nothing computed. The restore path passes it.
    * ``placement_titles`` — {delivered title -> row slug}, recorded live by a run. What
      ``_promote_phase`` passes, where it is complete by construction.

    With neither, a static-titled row still matches via the rendered-title fallback below, and only a
    ``{top_seed}`` row with no ledger entry falls to ``_promote_one``'s no-spec branch — which shows it
    on its own audience's Home. Right direction for a restore (the row was visible before the pause),
    but not the row's configured placement, which is why the ledger is consulted first.

    Returns the ratingKeys touched, and writes them into ``into`` as it goes when given one — so a
    caller that catches a mid-loop PMS failure still knows which collections were already set. Raises
    on a PMS failure; the caller owns how that is reported.
    """
    # Which row produced each of this user's collections, so promotion honours that row's placement
    # (Home / Library) and pin-to-top. Keyed by the exact title delivery wrote and recorded per library
    # during this user's run (a {top_seed} title differs per library).
    # `per_person_rows()`, NOT `config.rows`: with no rows configured it synthesizes the legacy default
    # spec, which is what every other phase builds from (_build_indexes, delivery). Reading the raw
    # list here meant an unmanaged-rows config had an EMPTY map, so every title lookup missed and every
    # collection fell to the no-spec fallback — placement silently ignored.
    effective_rows = ctx.config.per_person_rows()
    spec_by_slug = {spec.slug: spec for spec in effective_rows}
    placements = {title: spec_by_slug[slug] for title, slug in (placement_titles or {}).items() if slug in spec_by_slug}
    # Fallback for rows that EXIST but got no picks this run (so they're absent from placement_titles):
    # a STATIC-titled row's title is stable, so map it to its spec by that title — otherwise
    # _promote_one would fall to the everywhere-visible default and yank a "Library only" row onto Home
    # for this one run. Dynamic ({top_seed}) titles can't be predicted without picks, so those keep the
    # safe hide-everywhere fallback. resolve_row_template is the shared source of truth for the template
    # precedence delivery also uses — they must not drift.
    marker = row_marker(user.plex_account_id)
    for spec in effective_rows:
        if spec.audience is not None and user.plex_account_id not in spec.audience:
            continue
        title_template = resolve_row_template(spec, user, ctx.config)
        if "{top_seed}" not in title_template:
            # A {library_name} title differs per library, so map one per library — across EVERY library
            # of the row's media type, not just where it delivers now. `library_keys` says where the row
            # goes today; its collections may still sit in a library it was narrowed away from, and
            # since promotion now reaches those, an unmatched one would take the no-spec fallback EVERY
            # run rather than never being touched. A synthesized title matching no collection is inert,
            # and setdefault leaves the recorded per-library titles (placement_titles) winning.
            for section in target_sections(ctx.plex.sections(), replace(spec, library_keys=[])):
                name = render_row_name(title_template, user, [], library_name=getattr(section, "title", "") or "")
                placements.setdefault(name + marker, spec)

    promoted = into if into is not None else set()
    # Every row the user has, in every library — they can have several rows (all sharing their label),
    # and promoting only one would leave the others invisible to the one person meant to see them.
    # EVERY library, not just the ones a row currently targets: `delivery_sections` is narrowed to
    # targeted libraries, so a row whose `library_keys` was narrowed left its old collection stranded in
    # the dropped library — never re-promoted (out of scope) and never demoted either, keeping whatever
    # surfaces it last claimed indefinitely. The sweep and the removal paths already walk
    # `plex.sections()` for exactly this reason.
    # Identity beats a title — but a ratingKey is only trusted for a row this user is actually in the
    # audience for. The title path applies that check above; without the same one here, a ledger entry
    # left by a row whose audience later shrank (and whose reconcile failed) would promote it back with
    # that row's placement. `spec_by_slug` is already per-person-only, so a shared row cannot leak in.
    by_key = {
        key: spec_by_slug[slug]
        for key, slug in (placement_keys or {}).items()
        if slug in spec_by_slug
        and (spec_by_slug[slug].audience is None or user.plex_account_id in spec_by_slug[slug].audience)
    }
    for section in ctx.plex.sections():
        for collection in ctx.plex.find_owned_collections(section, user.label):
            if ctx.config.dry_run:
                # The dry-run guard lives HERE, not in the callers (plex-safety rule 8). `_promote_phase`
                # has its own `continue` further up, so this was invisible until `user.restore` became a
                # second caller — and `PlexClient.promote` has no dry-run branch of its own, so under
                # SHORTLIST_DRY_RUN an un-pause previewed the hiding and performed the showing.
                logger.info("[dry-run] {}: would promote for {}", collection.title, user.username)
                continue
            # Identity first: a ratingKey cannot be wrong, a title can be stale or unrenderable.
            spec = by_key.get(int(collection.ratingKey)) or placements.get(collection.title)
            _promote_one(ctx, collection, spec, user.user_type)
            promoted.add(int(collection.ratingKey))
    return promoted


def _converge_phase(
    ctx: EngineContext, promoted: set[int], report: RunReport, *, may_delete: bool | None = None
) -> None:
    """Take every Shortlist row this run did NOT promote off the owner's Home.

    Promotion is write-only and reaches a collection ONLY when its owner is in tonight's run. So a
    row belonging to anyone paused, disabled, deselected in a scoped run, errored, cancelled — or
    simply promoted by an older build with different rules — keeps whatever flags it last got, for
    ever. That is how 5 other people's rows ended up parked on the maintainer's Home screen (SFLIX,
    2026-07-28): the user-type-aware promote landed on 2026-07-27, but nothing went back for the
    collections it no longer visits.

    Scope is deliberately narrow — ONLY `promotedToOwnHome`, and only ever clearing it:

    * It is the single surface with no defence. The owner is never restricted (rule 5), so no
      `label!=` exclude can hide a row that lands there; every other surface is filtered per account.
    * Clearing it is monotonically private, so it needs no `filters_ok` gate and cannot leak even
      when run against a partially-understood server.

    Anything wider — deleting orphans, converging Recommended, restoring a paused row — can make the
    server LESS private if the picture is wrong, so it stays out until the desired state is derived
    rather than inferred. Walks `plex.sections()` (every library), not `delivery_sections`, because a
    row stranded in a library no enabled row still targets is exactly the case promotion cannot see.
    """
    if not ctx.owner_slug:
        # No owner known (direct engine runs, some tests): every label would look foreign and we would
        # demote the owner's own legitimate row. Do nothing rather than guess.
        logger.debug("converge: owner slug unknown — skipping")
        return
    # Labels ALLOWED on the owner's Home. Not just the owner's own row: a SHARED row is one public
    # collection labelled `shortlist__shared_<rowslug>`, and it legitimately sits on the owner's Home
    # whenever its placement asks for it (_promote_one gives user_type=None `home=spec.show_home`).
    # Matching only the owner's label demoted every shared row on every pass that did not rebuild it
    # — which is most of them: a no-user run, a scoped cron run, a cancelled run, a sync check.
    allowed = {f"{LABEL_PREFIX}_{ctx.owner_slug}".lower()}
    allowed |= {spec.label.lower() for spec in ctx.config.rows if spec.shared and spec.show_home and spec.label}
    prefix = f"{LABEL_PREFIX}_".lower()
    paused_labels = {f"{LABEL_PREFIX}_{slug}".lower() for slug in ctx.paused_slugs}
    shared_prefix = SHARED_LABEL_PREFIX.lower()
    live_shared = {spec.label.lower() for spec in ctx.config.shared_rows() if spec.label}
    # Neither depends on the collection being examined — hoisted out of the loop below rather than
    # rebuilt (rebuilding `known` re-lowercased every slug Shortlist has, per collection scanned).
    allowed_to_delete = ctx.may_delete_orphans if may_delete is None else (may_delete and ctx.may_delete_orphans)
    known = {slug.lower() for slug in ctx.known_slugs.values()}
    demoted: list[str] = []
    deleted: list[str] = []
    try:
        for section in ctx.plex.sections():
            for collection in section.collections():
                if int(collection.ratingKey) in promoted:
                    continue
                label = next((t.tag for t in collection.labels if t.tag.lower().startswith(prefix)), None)
                if label is None:
                    continue  # not ours (rule 4)

                # A DISABLED shared row's collection is retired the same way. `retired_rows` only
                # covers PER-PERSON rows (rows.py filters `not s.shared`), so switching a shared row
                # off left its collection claiming Friends' Home and the Recommended shelf for ever.
                # Non-owners stop seeing it (their filter excludes any label the config no longer
                # declares shared) but the OWNER has no filter, so it sat on their server unchanged.
                retired_shared = label.lower().startswith(shared_prefix) and label.lower() not in live_shared

                # A PAUSED user's row comes off EVERY surface, not just the owner's Home. Pause means
                # "stop showing it", and a paused person is by definition absent from every run, so
                # this is the only place it can happen. The collection and its label stay, so
                # everyone else's exclude still matches and unpausing is a re-promote, not a rebuild.
                if label.lower() in paused_labels or retired_shared:
                    reason = "row switched off" if retired_shared else "paused"
                    # Read first, exactly as the own-home branch does: a preview must list what would
                    # actually change, not every candidate considered.
                    if not ctx.plex.claims_any_surface(collection):
                        continue
                    if ctx.config.dry_run:
                        logger.info("[dry-run] {}: would take off every surface", collection.title)
                        demoted.append(label)
                        continue
                    with ctx.write_lock:
                        if ctx.plex.demote_all(collection, reason=reason):
                            demoted.append(label)
                    continue

                # ORPHAN: a per-person label whose user Shortlist no longer knows. Deleting is what
                # clears it out of the Collections tab; demoting only takes it off the shelves and
                # leaves it there.
                #
                # It does NOT clear the exclude. `privacy.prune` only ever removes shared labels and a
                # person's own label from their own filter — private-row excludes are union-only by
                # design — so `label!=shortlist_ghost` survives in every account's filter whether the
                # collection is deleted or not. An earlier version of this comment claimed deleting
                # was the only way to clear the filters, which was the stated reason for taking the
                # irreversible option.
                #
                # Gated on `may_delete_orphans` AND a non-empty roster, both deliberately. Deleting is
                # the one irreversible action here, and "I could not read the users" is
                # indistinguishable from "this user does not exist" — so an incomplete picture hides
                # rather than destroys. Owner decision 2026-07-28: delete, but only when sure.
                # DELETE authority belongs to the CALLER, not the context. `ctx.may_delete_orphans`
                # only says "the roster read succeeded, so the picture is complete" — it never said
                # "this particular pass is entitled to destroy something". Every path reaching here
                # inherited it, including `privacy.sync`, which documents itself as creating and
                # deleting nothing and fires from routine mutations like disabling one person.
                own_slug = label[len(prefix) :].lower()
                is_orphan = bool(known) and not label.lower().startswith(shared_prefix) and own_slug not in known

                if is_orphan and not allowed_to_delete:
                    if ctx.plex.claims_any_surface(collection):
                        wrote = ctx.config.dry_run or ctx.plex.demote_all(collection, reason="unknown owner")
                        if wrote:
                            demoted.append(label)
                    continue

                if is_orphan:
                    if ctx.config.dry_run:
                        logger.info("[dry-run] {}: would DELETE (no such user)", collection.title)
                    else:
                        with ctx.write_lock:
                            ctx.plex.delete_owned_collection(collection, LABEL_PREFIX)
                    deleted.append(label)
                    continue

                if label.lower() in allowed:
                    continue  # legitimately on the owner's Home
                # Read the hub even in dry-run: the preview an operator reads before running for real
                # has to be the actual list, not every candidate we considered.
                if not ctx.plex.reads_as_on_owner_home(collection):
                    continue
                if ctx.config.dry_run:
                    logger.info("[dry-run] {}: would demote off the owner's Home (converge)", collection.title)
                    demoted.append(label)
                    continue
                with ctx.write_lock:
                    if ctx.plex.demote_own_home(collection):
                        demoted.append(label)
    except Exception:
        # Best-effort: the run's real work is already done and this only ever removes visibility, so a
        # PMS wobble here must not fail the run. Next run converges again.
        logger.exception("converge: could not finish demoting stranded rows — next run retries")
    if deleted:
        report.orphans_removed = sorted(deleted)
        logger.warning(
            "converge: removed {} orphaned collection(s) whose user no longer exists ({})",
            len(deleted),
            ", ".join(sorted(set(deleted))),
        )
    if demoted:
        report.converged = sorted(demoted)
        logger.warning(
            "converge: corrected {} stranded row(s) ({})",
            len(demoted),
            ", ".join(sorted(set(demoted))),
        )


def _promote_one(ctx: EngineContext, collection, spec: RowSpec | None, user_type: UserType | None = None) -> None:
    """Promote one collection with its row's placement, respecting the user type.

    Every person gets their OWN collection, so all three Plex flags are chosen per collection from
    whose row it is: the owner's uses Home, everyone else's uses Friends' Home, and the
    Recommended-shelf flag comes from that same side of the row's placement. That is what keeps
    someone else's row off the owner's Home — and lets the owner keep their own row on the
    Recommended shelf without dragging everyone else's onto it.

    MANAGED users go with SHARED, not with the owner. Plex's own docs are explicit: Home
    (``promotedToOwnHome``) "applies to the server owner", while Shared Users' Home
    (``promotedToSharedHome``) "applies to all shared users, INCLUDING managed users"
    (https://support.plex.tv/articles/manage-recommendations/). Routing a managed user through the
    owner flag would hide their row from them and put it on the owner's Home instead.
    """
    if spec is None:
        # No spec could be matched to this title. This is NOT a rare path — the title->spec map
        # misses routinely (the full-stack suite reaches it for every collection), so whatever this
        # branch does is what most rows get. It therefore keeps each audience's own Home flag: making
        # it claim nothing hid every row in the integration suite, which on a real server means
        # people's rows silently disappearing. Under-showing here is NOT the safe direction.
        #
        # `recommended=False` is still deliberate. That flag is the one surface where the OWNER sees
        # every row (no share filter can hide it from them), so defaulting it on — as this used to —
        # forced rows onto the owner's shelf regardless of placement, including rows switched fully
        # off. Home flags stay per-audience: each only ever shows the row to its own owner.
        if user_type is UserType.OWNER:
            ctx.plex.promote(collection, shared=False, home=True, recommended=False)
        elif user_type is not None:
            # SHARED or MANAGED — both are covered by Shared Users' Home, never the owner's.
            ctx.plex.promote(collection, shared=True, home=False, recommended=False)
        else:
            ctx.plex.promote(collection, shared=True, recommended=False)
        return
    if user_type in (UserType.SHARED, UserType.MANAGED):
        home = False
        shared = spec.show_friends_home
        recommended = spec.show_friends_library
    elif user_type is None:
        # A SHARED row: ONE public collection for everyone rather than one per person, so it carries
        # both Home flags and the union of the two Recommended settings — there is no "whose row is
        # this" to split on.
        home = spec.show_home
        shared = spec.show_friends_home
        recommended = spec.show_library
    else:
        # The owner's (or a managed user's) own collection — only the owner side of the placement.
        home = spec.show_home
        shared = False
        recommended = spec.show_owner_library
    ctx.plex.promote(
        collection,
        shared=shared,
        home=home,
        recommended=recommended,
        pin_top=spec.pin_top,
    )


def _apply_order(ctx: EngineContext, report: RunReport, section, anchor, only_keys: set[int] | None) -> None:
    """One best-effort, gated reorder call + its audit. A shelf reorder is cosmetic and privacy-neutral
    (hubs are already promoted and browse-hidden; only position changes), so a failure never fails the
    run — next run re-applies. Only our hubs move; the anchor is read-only (Kometa coexistence)."""
    try:
        with ctx.write_lock:
            result = ctx.plex.order_owned_hubs(
                section,
                label_prefix=LABEL_PREFIX,
                anchor_title=anchor.anchor_title,
                before=anchor.before,
                to_top=anchor.to_top,
                dry_run=ctx.config.dry_run,
                only_keys=only_keys,
            )
        # Recorded when anything MOVED, verified or not. An unverified pass (Plex took the moves and
        # dropped them) is exactly the case the report has to be able to show, so it is not filtered
        # out here for looking like a failure.
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


def _row_keys_by_slug(ctx: EngineContext, section_key: str) -> dict[str, set[int]]:
    """row slug -> the Plex ratingKeys that row's collections have in this library, from the DURABLE
    delivery ledger.

    The ledger, not this run's report, on purpose. This used to read `report.users[].placement_titles`,
    which only ever holds rows delivered by THE RUN IN PROGRESS — so a `privacy.sync`
    (`engine_run(ctx, [])`, which is what the nightly privacy-sync job and the "Fix privacy" button both
    run) had an empty map, every group came out empty, and the whole ordering pass silently did nothing.
    On SFLIX that was 31 runs in one day reaching this code and issuing not one move (2026-08-12).
    The ledger is written by past runs, so it answers the same question for a run with no users at all.
    """
    out: dict[str, set[int]] = {}
    for (_user_slug, row_slug, key), rating_key in ctx.delivered_keys.items():
        if key == section_key and rating_key:
            out.setdefault(row_slug, set()).add(rating_key)
    return out


def _order_phase(ctx: EngineContext, report: RunReport) -> None:
    """Place each library's Shortlist rows in its Recommended shelf per the configured anchors.

    Each row's effective anchor is its own per-library override (``RowSpec.hub_anchors``) if set, else
    the global default (``EngineConfig.hub_anchors``). When every row in a library resolves to the SAME
    anchor — which is every ordinary server, and every server with one row — they all move together in
    one call that names no rows at all, so it works identically on a full run and on a `privacy.sync`
    with no users. Only when rows genuinely disagree are they partitioned, by delivery-ledger ratingKey.
    """
    if not ctx.config.manage_shelf_order:
        # The owner turned shelf ordering off (a co-managing tool like agregarr/Kometa owns the order),
        # so leave the Recommended shelf exactly as it is — deliver + hide + promote still ran above.
        logger.debug("shelf ordering is off — leaving the Recommended shelf order to Plex / a co-managing tool")
        return
    global_anchors = ctx.config.hub_anchors
    any_override = any(spec.hub_anchors for spec in ctx.config.rows)
    if not global_anchors and not any_override:
        # No explicit anchors configured — default to moving all owned rows to the top of each
        # library's Recommended shelf. Without this, aborted runs or new promotions scatter rows to
        # wherever Plex appends them, which looks broken (issue: some at top, some at bottom).
        for section in ctx.delivery_sections:
            default = HubAnchor(anchor_title="", before=False, to_top=True)
            _apply_order(ctx, report, section, default, only_keys=None)
        return
    for section in ctx.delivery_sections:
        key = str(section.key)
        anchors_by_slug = {}
        for spec in ctx.config.rows:
            effective = spec.hub_anchors.get(key) or global_anchors.get(key)
            if effective is not None:
                anchors_by_slug[spec.slug] = effective
        if not anchors_by_slug:
            # No ROW resolves to an anchor here — but the library may still have one, and rows of ours
            # may still be sitting on its shelf: every row switched off or deleted leaves its retired
            # collections behind, and `ctx.config.rows` is then empty while `rows.hub_anchor` is not.
            # Falling through to `continue` skipped the library in silence, which is the same shape of
            # quiet nothing this whole function was just fixed for.
            default = global_anchors.get(key)
            if default is not None:
                _apply_order(ctx, report, section, default, only_keys=None)
            else:
                logger.debug("hub order: no anchor configured for {} — leaving its shelf alone", section.title)
            continue
        distinct = {(a.to_top, a.anchor_title, a.before) for a in anchors_by_slug.values()}
        unanchored = [spec.slug for spec in ctx.config.rows if spec.slug not in anchors_by_slug]
        if len(distinct) == 1 and not unanchored:
            # Every row here wants the same slot, so there is nothing to tell apart: move ALL our rows
            # in this library with one call. Naming no rows is what makes this independent of who was
            # delivered tonight — the bug that left a run with no users ordering nothing at all.
            _apply_order(ctx, report, section, next(iter(anchors_by_slug.values())), only_keys=None)
            continue
        # Rows genuinely disagree about where they belong (or some have no anchor at all), so ours have
        # to be partitioned. The ledger is the only durable link from a collection back to its row.
        keys_by_slug = _row_keys_by_slug(ctx, key)
        groups: dict[tuple[bool, str, bool], set[int]] = {}
        for slug, effective in anchors_by_slug.items():
            keys = keys_by_slug.get(slug, set())
            if not keys:
                # Nothing delivered for this row here yet (a first run, or a row that has never
                # reached this library). Said out loud rather than skipped in silence — silence here
                # is exactly what made the original bug invisible. The next run places it.
                logger.debug(
                    "hub order: row '{}' has no delivered collection in {} yet — not ordering it this run",
                    slug,
                    section.title,
                )
                continue
            groups.setdefault((effective.to_top, effective.anchor_title, effective.before), set()).update(keys)
        for (to_top, anchor_title, before), keys in groups.items():
            anchor = HubAnchor(anchor_title=anchor_title, before=before, to_top=to_top)
            _apply_order(ctx, report, section, anchor, only_keys=keys)


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
                mdblist=ctx.mdblist,
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
