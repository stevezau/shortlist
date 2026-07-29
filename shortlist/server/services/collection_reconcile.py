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
from dataclasses import replace
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
    strip_marker,
)
from shortlist.engine.models import SHARED_LABEL_PREFIX, UserProfile, UserType
from shortlist.server.db.models import DEFAULT_SLUG, Collection, Delivery, Event, Run, User
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.settings_store import SettingsStore


def _delivered_titles_by_user(session, slug: str) -> dict[int, dict[str, str]]:
    """{user_id → {delivered Plex title → the library it was delivered in}} for THIS row, from the
    persisted breakdown of the latest completed run.

    A SECONDARY source of candidate titles, never the only one. Two things make it unreliable on its
    own, and relying on it was the root of a whole family of "the config changed and Plex did not"
    bugs: rows have their own crons, so the latest run is routinely scoped to one row (delete row B the
    morning after row A ran and this returns nothing at all), and `DELETE /api/runs` empties it
    outright while claiming to change nothing on Plex.

    It is still worth reading because it covers the one case rendering cannot: a `{top_seed}` template
    renders a different title every run, so the recorded title is the only way to recognise it.
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


def row_template(session, slug: str, secrets=None) -> str:
    """The name template a row's collections are titled from, resolved the way delivery resolves it.

    The DEFAULT row deliberately has no per-collection template — its title IS the global
    ``row.name_template`` setting, because a per-collection one would beat each user's own
    ``row_name_tpl`` override. Every other row uses its own template, falling back to its plain name.
    Empty when the row no longer exists (already deleted): the caller then has only the recorded titles.
    """
    collection = session.query(Collection).filter_by(slug=slug).first()
    if slug == DEFAULT_SLUG:
        return SettingsStore(session, secrets).get("row.name_template") or ""
    return (collection.name_template or collection.name) if collection else ""


def _ledger_keys(session, slug: str) -> dict[str, set[int]]:
    """{user slug -> the Plex ratingKeys the ledger says this row built for them}.

    The PRIMARY way a per-person collection is found. It is the only source that survives a title
    changing, which a ``{top_seed}`` row's does every single run — rendering the template cannot help
    there, and the run breakdown it used to fall back on is scoped to one run and erased by
    ``DELETE /api/runs``.

    Empty for a row delivered before the ledger existed, or never delivered at all; the title-based
    sources below then carry it, exactly as they did before.
    """
    keys: dict[str, set[int]] = {}
    for row in session.query(Delivery).filter_by(collection_slug=slug):
        if row.rating_key:
            keys.setdefault(row.user_slug, set()).add(row.rating_key)
    return keys


def forget_user_deliveries(session, user_slug: str) -> None:
    """Drop every ledger row for one person — their whole label was just removed from Plex.

    `user.cleanup` deletes ALL of a user's collections at once (disable, or leaving the share), which
    the per-row `_forget_deliveries` never sees. Without this the ledger keeps pointing at ratingKeys
    that no longer exist: harmless for correctness (a removal still has to find the collection under
    one of OUR labels first, so a stale key cannot reach anything) but it grows for ever and makes the
    audit lie about what is on the server. Found by testing a disable against a real PMS.
    """
    session.query(Delivery).filter_by(user_slug=user_slug).delete(synchronize_session=False)


def _forget_deliveries(
    session, slug: str, user_slugs: set[str] | None = None, in_sections: set[str] | None = None
) -> None:
    """Drop ledger rows for collections that no longer exist, so it stays a record of what IS.

    Scoped to exactly what the sweep could have removed. ``in_sections`` matters most: a NARROWED row
    only removes the libraries it walked away from, so forgetting the whole row would drop the entry
    for a collection that is still live — and for a `{top_seed}` row that entry is the only thing that
    could ever address it again, since its title cannot be re-rendered and a blank schedule means no
    run will re-populate the ledger.

    A stale key is BOUNDED, not inert. It no longer only narrows a removal: `promote_user_rows` reads
    the ledger too, so a key naming the wrong collection writes that row's placement flags. It still
    cannot reach a foreign collection (every candidate is found under one of OUR labels first) nor past
    a share filter, and `_refuse_a_different_server` rules out a ledger from another machine — but
    "harmless" is too strong, which is why forgetting is scoped as tightly as it is.
    """
    query = session.query(Delivery).filter_by(collection_slug=slug)
    if user_slugs is not None:
        query = query.filter(Delivery.user_slug.in_(user_slugs))
    if in_sections is not None:
        query = query.filter(Delivery.library_key.in_(in_sections))
    query.delete(synchronize_session=False)


def _profile_of(udata: dict) -> UserProfile:
    """The engine profile a title renders from. `nickname` matters: without it a `{user}` row renders
    to the PLEX username here and to the nickname at delivery, so every computed title would miss."""
    return UserProfile(
        username=udata["username"],
        plex_account_id=udata["plex_account_id"],
        user_type=UserType(udata["user_type"]),
        slug=udata["slug"],
        nickname=udata["nickname"],
    )


def _users_data(session) -> list[dict]:
    """Every user as a plain dict, so the Plex walk below runs outside the session.

    EVERY user, not just the enabled ones: a disabled user whose cleanup job has not landed yet still
    has collections on the server, and skipping them strands a title no run will write again.
    """
    return [
        {
            "id": u.id,
            "slug": u.slug,
            "username": u.username,
            "nickname": u.nickname or u.friendly_name,
            "plex_account_id": u.plex_account_id,
            "user_type": u.user_type,
            "prefs": u.prefs or {},
        }
        for u in session.query(User).all()
    ]


def _rendered_titles(ctx, udata: dict, template: str, slug: str) -> set[str]:
    """Every title THIS row's template renders to for this user, one per library.

    The primary way a per-person collection is identified. All of a user's rows share one label, so the
    title is the only discriminator — and computing it from the template works whatever the run history
    says, which is the whole point.

    Empty for a `{top_seed}` (or blank) template: with no picks to render from, both collapse to the
    bare default title, which would match EVERY row rather than this one. Those fall back to the
    recorded titles — the same split `_promote_phase` makes for the same reason.
    """
    if not template or "{top_seed}" in template:
        return set()
    profile = _profile_of(udata)
    titles = {
        render_row_name(template, profile, [], library_name=getattr(section, "title", "") or "")
        for section in ctx.plex.sections()
    }
    logger.debug("row '{}': {} renders to {}", slug, udata["slug"], sorted(titles))
    return titles


def _write_audit(state, scope: str, level: str, **message) -> None:
    """Write one reconcile audit Event with a UTC timestamp and commit (plex-safety rule 10). The
    caller passes its distinctive message fields; this owns the shared timestamp + persistence."""
    with state.sessions() as session:
        session.add(Event(scope=scope, level=level, message={**message, "at": datetime.now(UTC).isoformat()}))
        session.commit()


def _reconcile_row_removal(
    state,
    *,
    slug: str,
    build: str,
    dry_run: bool,
    removed: list[str],
    only_user_ids: set[int] | None = None,
    template: str | None = None,
    in_sections: set[str] | None = None,
) -> None:
    """Remove a row's collections from Plex. Accumulates the display titles into the ``removed``
    out-param (so a mid-loop PMS failure still leaves the partial list for the audit).

    Shared rows go by their own label (one membership). Per-person rows share ONE label per user across
    all of their rows, so the title is the only discriminator, and each user's collection is found by
    the union of two sources:

    * what the row's own name template RENDERS to for them, per library — the primary source, because
      it is computed from the row's config and so works whatever the run history happens to hold;
    * what the latest run recorded for this row — the fallback that covers a ``{top_seed}`` title,
      which is different every run and therefore cannot be rendered.

    The union is the fix: with only the recorded titles, deleting row B the morning after row A ran
    removed nothing and audited it as "removed 0", and for a deleted row there is no second chance.
    Both sources are scoped to the user's own label, so neither can reach another user's row or a
    foreign (Kometa) collection.

    The template is read from the DB by default, so every door — build flip, audience shrink, row
    disabled, the manual cleanup button — gets it for free. ``template`` overrides that for the DELETE
    path, where the row is about to stop existing: a job replayed after a crash would otherwise find no
    row, resolve no template, and fall back to the recorded titles alone.

    ``only_user_ids`` limits the per-person sweep to specific users (audience-shrink cleanup); ``None``
    means everyone (delete-row / manual cleanup). ``in_sections`` limits it to specific libraries, for
    a row that was NARROWED rather than removed — its ``media`` went from both to movie, or a library
    left ``library_keys`` — where the collections it still uses must survive. Removal only, so
    gate-exempt. Runs in an executor."""
    dry_run = force_dry_run() or dry_run  # honour SHORTLIST_DRY_RUN safe-mode
    ctx = state.run_service.build_context(dry_run=dry_run)
    if build == "shared":
        # A shared row is one collection for everyone; who SEES it is a share-filter concern handled
        # by the privacy pass the caller queues, not a per-user collection to remove here.
        if only_user_ids is None:
            removed.extend(
                remove_row_collections(
                    ctx.plex,
                    ctx.config,
                    label=f"{SHARED_LABEL_PREFIX}{slug}",
                    displays=None,
                    dry_run=dry_run,
                    in_sections=in_sections,
                )
            )
        return
    with state.sessions() as session:
        keys_by_user = _ledger_keys(session, slug)
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = _users_data(session)
        if template is None:
            template = row_template(session, slug, state.secrets)
    swept: set[str] = set()
    for user in users:
        if only_user_ids is not None and user["id"] not in only_user_ids:
            continue
        # The DEFAULT row has no per-collection template, so each user's title is their own
        # `row_name_tpl` override or the global one — the same precedence delivery resolves.
        override = user["prefs"].get("row_name_tpl") if slug == DEFAULT_SLUG else None
        displays = _rendered_titles(ctx, user, override or template, slug)
        displays |= set(titles_by_user.get(user["id"], {}))  # the library is only of interest to rename
        rating_keys = keys_by_user.get(user["slug"], set())
        if not displays and not rating_keys:
            continue
        swept.add(user["slug"])
        removed.extend(
            remove_row_collections(
                ctx.plex,
                ctx.config,
                label=f"{ctx.config.label_prefix}_{user['slug']}",
                displays=displays,
                rating_keys=rating_keys,
                dry_run=dry_run,
                in_sections=in_sections,
            )
        )
    # The ledger records collections that EXIST. Only after a real removal — a dry run changed nothing,
    # and forgetting there would leave the next live attempt with no ledger to address by.
    if swept and not dry_run:
        with state.sessions() as session:
            _forget_deliveries(session, slug, swept, in_sections)
            session.commit()


def _reconcile_poster_reset(state, *, slug: str, build: str, reset: list[str]) -> None:
    """Revert a row's Plex collections to their default artwork after it switches to 'Plex default'.

    Shared rows go by their own label (one membership, any title); per-person rows are found by the
    same union `_reconcile_row_removal` uses — the titles this row's template renders to, plus whatever
    the latest run recorded — scoped to that user's own label, so it only ever touches OUR collections.
    Cosmetic + privacy-neutral, so gate-exempt. Runs in an executor."""
    dry_run = force_dry_run()  # honour SHORTLIST_DRY_RUN safe-mode (this path is otherwise always live)
    ctx = state.run_service.build_context(dry_run=dry_run)
    if build == "shared":
        reset.extend(
            reset_row_posters(
                ctx.plex, ctx.config, label=f"{SHARED_LABEL_PREFIX}{slug}", displays=None, dry_run=dry_run
            )
        )
        return
    with state.sessions() as session:
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = _users_data(session)
        template = row_template(session, slug, state.secrets)
    for user in users:
        override = user["prefs"].get("row_name_tpl") if slug == DEFAULT_SLUG else None
        displays = _rendered_titles(ctx, user, override or template, slug)
        displays |= set(titles_by_user.get(user["id"], {}))
        if not displays:
            continue
        reset.extend(
            reset_row_posters(
                ctx.plex,
                ctx.config,
                label=f"{ctx.config.label_prefix}_{user['slug']}",
                displays=displays,
                dry_run=dry_run,
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
    label, which never changes here). For the DEFAULT row a user's own ``row_name_tpl`` override wins
    over ``new_template`` (matching delivery's ``resolve_row_template``), so an override user is
    re-rendered from their own template and left untouched. A ``{library_name}`` template IS renamed
    here — it renders to a stable per-library title, and the breakdown records which library each old
    title came from, so the new title is re-rendered in that SAME library. Only titles that render to
    the default with no picks
    (a ``{top_seed}`` template, or a blank one) are skipped — their title changes every run anyway, and
    the next run's delivery already renames the sole-row case. Runs in an executor.

    Accumulates one ``{user, old, new, libraries}`` entry per user actually renamed into ``entries``,
    so the audit can answer "whose row went from what to what, in which libraries" (rule 10)."""
    with state.sessions() as session:
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = session.query(User).all()
    dry_run = force_dry_run()  # honour SHORTLIST_DRY_RUN safe-mode (this path is otherwise always live)
    ctx = state.run_service.build_context(dry_run=dry_run)
    for user in users:
        old_titles = titles_by_user.get(user.id, {})  # {delivered title -> library it was delivered in}
        if not old_titles:
            continue
        # The DEFAULT row has no per-collection template, so delivery resolves each user's title as
        # their own `row_name_tpl` override or the global template (resolve_row_template's second tier).
        # Re-render with the SAME per-user template here, or a user who set a personal name would have
        # their collection renamed to the global template — clobbering their override until the next run.
        override = (user.prefs or {}).get("row_name_tpl") if slug == DEFAULT_SLUG else None
        effective_template = override or new_template
        profile = UserProfile(
            username=user.username,
            plex_account_id=user.plex_account_id,
            user_type=UserType(user.user_type),
            slug=user.slug,
            # Without this a `{user}` row renders to the PLEX username here and to the nickname at
            # delivery — so the reconcile would compute the wrong "new" title and either rename to
            # something no run will ever write, or decide nothing changed and leave a stale copy.
            nickname=user.nickname or user.friendly_name,
        )
        marker = row_marker(user.plex_account_id)
        for old_display, library_title in old_titles.items():
            # Re-render in the SAME library the old title was delivered in, so a {library_name} row's
            # "✨ Movies Picked for You" is renamed to its new Movies title, not to some other library's.
            new_display = render_row_name(effective_template, profile, [], library_name=library_title)
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
                dry_run=dry_run,
            )
            if libraries:
                entries.append({"user": user.slug, "old": old_display, "new": new_display, "libraries": libraries})


def reconcile_row_rename_iter(
    state,
    *,
    slug: str,
    new_template: str,
    old_template: str | None = None,
    old_display_names: dict[str, str] | None = None,
):
    """Rename a row's collections on Plex, yielding one event per user renamed (for SSE streaming).

    Finds collections directly from Plex (by label), not from run history — so it works even after
    runs are cleared. For each user: finds their collections by label on Plex, identifies this row's
    collection by its old rendered title, computes the new title from the template, and renames if
    different.

    When ``old_template`` is provided (the template BEFORE the rename), it is used to identify which
    collection on Plex belongs to this row (per-person rows share one label for ALL rows, so title is
    the only discriminator). Without it, all collections under the user's label are candidates — safe
    on single-row servers but may misfire on multi-row ones.

    ``old_display_names`` ({user slug -> their PREVIOUS display name}) covers the case where the
    template did not change but what it renders to did: a nickname edit, or a Tautulli rename picked up
    by a user sync. Without it, ``{user}`` would render the NEW name on both sides, match nothing, and
    leave the old-titled collection on the server for the next run to duplicate.

    Yields: {"user": slug, "display_name": str, "old": old_title, "new": new_title, "libraries": [...]}
    and {"user", "library", "error"} for a per-collection PMS failure.
    At the end yields {"done": True, "total": n}.
    """
    with state.sessions() as session:
        users_data = _users_data(session)
    dry_run = force_dry_run()
    ctx = state.run_service.build_context(dry_run=dry_run)
    total = 0
    for udata in users_data:
        override = udata["prefs"].get("row_name_tpl") if slug == DEFAULT_SLUG else None
        effective_template = override or new_template
        effective_old = override or old_template
        profile = _profile_of(udata)
        # The profile the OLD title was rendered from. Identical to `profile` except after a rename of
        # the PERSON rather than the row, where the only thing that moved is what `{user}` renders to.
        was = old_display_names.get(udata["slug"]) if old_display_names else None
        old_profile = replace(profile, nickname=was) if was else profile
        label = f"{ctx.config.label_prefix}_{udata['slug']}"
        marker = row_marker(udata["plex_account_id"])
        for section in ctx.plex.sections():
            lib_name = getattr(section, "title", "") or ""
            new_display = render_row_name(effective_template, profile, [], library_name=lib_name)
            if new_display == DEFAULT_ROW_NAME:
                continue
            new_with_marker = new_display + marker
            old_display = (
                render_row_name(effective_old, old_profile, [], library_name=lib_name) if effective_old else None
            )
            for collection in ctx.plex.find_owned_collections(section, label):
                current_title = collection.title
                if current_title == new_with_marker:
                    continue
                # Scope to THIS row: if we know the old template, only rename collections whose
                # stripped title matches what this row USED to render as.
                if old_display and strip_marker(current_title) != old_display:
                    continue
                try:
                    if not dry_run:
                        collection.editTitle(new_with_marker)
                    total += 1
                    yield {
                        "user": udata["slug"],
                        "display_name": profile.display_name,
                        "old": strip_marker(current_title),
                        "new": new_display,
                        "libraries": [lib_name],
                    }
                except Exception as e:
                    # Yielded, not just logged: one user's PMS failure must not stop the other users'
                    # renames, but it must still reach the audit and the SSE stream. Swallowing it
                    # recorded "renamed 0 collections" for a run that actually failed — indistinguishable
                    # from "nothing needed renaming", which is the one thing an operator must be able to
                    # tell apart. Redacted: a PMS error can carry a tokened URL (rule 9).
                    message = redact(f"{type(e).__name__}: {e}")
                    logger.warning("{}: rename failed in {} ({})", udata["slug"], lib_name, message)
                    yield {"user": udata["slug"], "library": lib_name, "error": message}
    yield {"done": True, "total": total}


async def run_row_rename_from_plex(
    state,
    *,
    slug: str,
    new_template: str,
    old_template: str,
    scope: str,
    old_display_names: dict[str, str] | None = None,
) -> tuple[list[dict], str | None]:
    """Rename a row's collections by reading Plex, not run history. Audited (rule 10), best-effort.

    The same work ``run_row_rename`` does, addressed the RIGHT way: ``reconcile_row_rename_iter``
    enumerates each user's collections from the server by label and identifies this row's by the title
    the OLD template renders to, so it does not care whether a run happened recently, was scoped to a
    different row, or has since been cleared from history. Used wherever the caller knows the previous
    template — which every rename door does, since it has to compare to know a rename happened at all.
    """
    entries: list[dict] = []
    failures: list[str] = []
    error: str | None = None

    def _collect() -> None:
        for event in reconcile_row_rename_iter(
            state,
            slug=slug,
            new_template=new_template,
            old_template=old_template,
            old_display_names=old_display_names,
        ):
            if event.get("error"):
                failures.append(f"{event.get('user', '?')}: {event['error']}")
            elif not event.get("done"):
                entries.append(event)

    try:
        await asyncio.get_running_loop().run_in_executor(None, _collect)
    except Exception as e:
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    # A per-user PMS failure is still a failure. Reported alongside whatever DID get renamed, so the
    # audit distinguishes "nothing needed doing" from "some of it could not be done".
    if failures and error is None:
        error = "; ".join(failures)
    _write_audit(state, scope, "info", slug=slug, renames=entries, new_template=new_template, error=error)
    logger.info(
        "{} '{}': renamed {} collection(s){}", scope, slug, len(entries), f" then FAILED: {error}" if error else ""
    )
    return entries, error


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
