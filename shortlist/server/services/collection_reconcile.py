"""On-demand Plex reconciles for config changes (row delete/rename/build-flip/audience-shrink).

These run OUTSIDE the nightly pipeline, in response to an owner editing a row, so they live in a
service rather than in the API router (matching run_service). Every one is privacy-neutral or
removal-only — it either deletes an owned collection or retitles one in place, never creates or
promotes a row, never touches an exclude or share filter — so it is gate-exempt (plex-safety rule 1,
third exception). Each is audited (rule 10) and best-effort: a Plex outage is recorded, never fatal
to the request.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from loguru import logger

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.delivery import (
    DEFAULT_ROW_NAME,
    remove_row_collections,
    rename_row_collections,
    render_row_name,
    reset_row_posters,
    row_marker,
)
from shortlist.engine.models import SHARED_LABEL_PREFIX, UserProfile, UserType
from shortlist.server.db.models import Event, Run, User


def _delivered_titles_by_user(session, slug: str) -> dict[int, dict[str, str]]:
    """{user_id → {delivered Plex title → the library it was delivered in}} for THIS row, from the
    persisted breakdown.

    Both reconcile paths (remove/rename) find a user's collection by the exact title the last run
    wrote for the row, scoped to that user — never by a foreign (Kometa) or another user's row. The
    library is carried so rename can re-render a per-library ({library_name}) title in the SAME library
    it was delivered in; removal ignores it and matches on the titles alone.
    """
    latest = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
    result: dict[int, dict[str, str]] = {}
    for ru in latest.users if latest else []:
        titles = {
            e["row_title"]: e.get("library_title", "")
            for e in (ru.breakdown or [])
            if e.get("row_slug") == slug and e.get("row_title")
        }
        if titles:
            result[ru.user_id] = titles
    return result


def _write_audit(state, scope: str, level: str, **message) -> None:
    """Write one reconcile audit Event with a UTC timestamp and commit (plex-safety rule 10). The
    caller passes its distinctive message fields; this owns the shared timestamp + persistence."""
    with state.sessions() as session:
        session.add(Event(scope=scope, level=level, message={**message, "at": datetime.now(UTC).isoformat()}))
        session.commit()


def _reconcile_row_removal(
    state, *, slug: str, build: str, dry_run: bool, removed: list[str], only_user_ids: set[int] | None = None
) -> None:
    """Remove a row's collections from Plex. Accumulates the display titles into the ``removed``
    out-param (so a mid-loop PMS failure still leaves the partial list for the audit).

    Shared rows go by their own label (one membership); per-person rows are pinned per user by the
    exact title the last run delivered for THIS row (its persisted breakdown), scoped to that user's
    own label — so it can never reach another user's row or a foreign (Kometa) collection.
    ``only_user_ids`` limits the per-person sweep to specific users (audience-shrink cleanup); ``None``
    means everyone (delete-row / manual cleanup). Removal only, so gate-exempt. Runs in an executor."""
    ctx = state.run_service.build_context(dry_run=dry_run)
    if build == "shared":
        # A shared row is one collection for everyone; who SEES it is a share-filter concern handled
        # by the next run's privacy sync, not a per-user collection to remove here.
        if only_user_ids is None:
            removed.extend(
                remove_row_collections(
                    ctx.plex, ctx.config, label=f"{SHARED_LABEL_PREFIX}{slug}", displays=None, dry_run=dry_run
                )
            )
        return
    with state.sessions() as session:
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = session.query(User).all()
    for user in users:
        if only_user_ids is not None and user.id not in only_user_ids:
            continue
        displays = set(titles_by_user.get(user.id, {}))  # the delivered titles; the library is only for rename
        if not displays:
            continue
        removed.extend(
            remove_row_collections(
                ctx.plex, ctx.config, label=f"{ctx.config.label_prefix}_{user.slug}", displays=displays, dry_run=dry_run
            )
        )


def _reconcile_poster_reset(state, *, slug: str, build: str, reset: list[str]) -> None:
    """Revert a row's Plex collections to their default artwork after it switches to 'Plex default'.

    Shared rows go by their own label (one membership, any title); per-person rows are pinned per user
    by the exact titles the last run delivered for THIS row, scoped to that user's own label — so it
    only ever touches OUR collections. Cosmetic + privacy-neutral, so gate-exempt. Runs in an executor."""
    ctx = state.run_service.build_context(dry_run=False)
    if build == "shared":
        reset.extend(
            reset_row_posters(ctx.plex, ctx.config, label=f"{SHARED_LABEL_PREFIX}{slug}", displays=None, dry_run=False)
        )
        return
    with state.sessions() as session:
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = session.query(User).all()
    for user in users:
        displays = set(titles_by_user.get(user.id, {}))
        if not displays:
            continue
        reset.extend(
            reset_row_posters(
                ctx.plex, ctx.config, label=f"{ctx.config.label_prefix}_{user.slug}", displays=displays, dry_run=False
            )
        )


async def run_poster_reset(state, *, slug: str, build: str, scope: str) -> tuple[list[str], str | None]:
    """Run ``_reconcile_poster_reset`` in an executor and audit it (rule 10). Best-effort — a Plex
    outage is recorded, never fatal to the PATCH. Returns ``(reset_library_titles, error)``."""
    reset: list[str] = []
    error: str | None = None
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _reconcile_poster_reset(state, slug=slug, build=build, reset=reset)
        )
    except Exception as e:
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    _write_audit(state, scope, "info", slug=slug, poster_reset=reset, error=error)
    logger.info("{} '{}': reset {} poster(s){}", scope, slug, len(reset), f" then FAILED: {error}" if error else "")
    return reset, error


async def run_reconcile(
    state, *, slug: str, build: str, dry_run: bool, scope: str, only_user_ids: set[int] | None = None
) -> tuple[list[str], str | None]:
    """Run ``_reconcile_row_removal`` in an executor and audit it (rule 10) — even a mid-loop failure
    records what was already removed. Returns ``(removed, error)``."""
    removed: list[str] = []
    error: str | None = None
    try:
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _reconcile_row_removal(
                state, slug=slug, build=build, dry_run=dry_run, removed=removed, only_user_ids=only_user_ids
            ),
        )
    except Exception as e:  # a destructive write is never silent: audit the partial removal, then surface it
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    _write_audit(state, scope, "warn", slug=slug, removed=removed, dry_run=dry_run, error=error)
    logger.warning("{} '{}': {} collection(s){}", scope, slug, len(removed), f" then FAILED: {error}" if error else "")
    return removed, error


def _reconcile_row_rename(state, *, slug: str, new_template: str, entries: list[dict]) -> None:
    """Rename a per-person row's collections IN PLACE for every user who has it — multi-row users would
    otherwise keep the old-named copy alongside the one the next run builds under the new name.

    Each user's collection is found by the exact title the last run delivered for THIS row (its
    persisted breakdown), scoped to that user's own label, and renamed to the freshly-rendered new
    title (same account marker). Privacy-neutral, so gate-exempt (the hiding filter is keyed on the
    label, which never changes here). A ``{library_name}`` template IS renamed here — it renders to a
    stable per-library title, and the breakdown records which library each old title came from, so the
    new title is re-rendered in that SAME library. Only titles that render to the default with no picks
    (a ``{top_seed}`` template, or a blank one) are skipped — their title changes every run anyway, and
    the next run's delivery already renames the sole-row case. Runs in an executor.

    Accumulates one ``{user, old, new, libraries}`` entry per user actually renamed into ``entries``,
    so the audit can answer "whose row went from what to what, in which libraries" (rule 10)."""
    with state.sessions() as session:
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = session.query(User).all()
    ctx = state.run_service.build_context(dry_run=False)
    for user in users:
        old_titles = titles_by_user.get(user.id, {})  # {delivered title -> library it was delivered in}
        if not old_titles:
            continue
        profile = UserProfile(
            username=user.username,
            plex_account_id=user.plex_account_id,
            user_type=UserType(user.user_type),
            slug=user.slug,
        )
        marker = row_marker(user.plex_account_id)
        for old_display, library_title in old_titles.items():
            # Re-render in the SAME library the old title was delivered in, so a {library_name} row's
            # "✨ Movies Picked for You" is renamed to its new Movies title, not to some other library's.
            new_display = render_row_name(new_template, profile, [], library_name=library_title)
            if new_display == DEFAULT_ROW_NAME:
                # A {top_seed}/blank template renders to the default with no picks — its title changes
                # every run anyway, so the next run's delivery renames the sole-row case. Skip here.
                logger.debug("rename reconcile: '{}' renders to the default title with no picks — left for a run", slug)
                continue
            if old_display == new_display:
                continue  # this user's title didn't actually change (e.g. a {user} template)
            libraries = rename_row_collections(
                ctx.plex,
                ctx.config,
                label=f"{ctx.config.label_prefix}_{user.slug}",
                marker=marker,
                old_display=old_display,
                new_display=new_display,
                dry_run=False,
            )
            if libraries:
                entries.append({"user": user.slug, "old": old_display, "new": new_display, "libraries": libraries})


async def run_row_rename(state, *, slug: str, new_template: str, scope: str) -> tuple[list[dict], str | None]:
    """Run ``_reconcile_row_rename`` in an executor and audit it with per-user old→new detail (rule 10).
    Best-effort — a Plex outage is logged, never fatal to the PATCH. Returns ``(rename_entries, error)``."""
    entries: list[dict] = []
    error: str | None = None
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _reconcile_row_rename(state, slug=slug, new_template=new_template, entries=entries)
        )
    except Exception as e:
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    # renames: per user {user, old, new, libraries} — answers rule 10's "whose row, what→what".
    _write_audit(state, scope, "info", slug=slug, renames=entries, new_template=new_template, error=error)
    total = sum(len(e["libraries"]) for e in entries)
    logger.info(
        "{} '{}': renamed {} collection(s) for {} user(s){}",
        scope,
        slug,
        total,
        len(entries),
        f" then FAILED: {error}" if error else "",
    )
    return entries, error
