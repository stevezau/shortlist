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
from collections.abc import Callable
from dataclasses import replace

from loguru import logger

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.delivery import (
    DEFAULT_ROW_NAME,
    remove_row_collections,
    render_row_name,
    reset_row_posters,
    row_marker,
    strip_marker,
)
from shortlist.engine.models import LABEL_PREFIX, SHARED_LABEL_PREFIX, UserProfile, UserType
from shortlist.server.db.models import DEFAULT_SLUG, Collection, Delivery, Run, User
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.services.audit import write_audit
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


def _shared_profile() -> UserProfile:
    """The synthetic profile a SHARED row's title renders from — the same one the engine uses, so a
    `{user}` placeholder resolves identically here and at delivery."""
    return UserProfile(
        username="Everyone",
        plex_account_id=0,
        user_type=UserType.SHARED,
        slug="everyone",
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


def _walk_row_collections(
    ctx,
    users: list[dict],
    *,
    slug: str,
    template: str,
    titles_by_user: dict[int, dict[str, str]],
    action: Callable[[dict, set[str]], None],
    only_user_ids: set[int] | None = None,
) -> None:
    """Call ``action(user, displays)`` for each user, with every title THIS row could be wearing.

    The one place the "which collection is this row's?" question is answered for a per-person row,
    shared by the removal and poster-reset passes: the titles the row's own template renders to for
    that user, unioned with whatever the latest run recorded (the only source that can name a
    ``{top_seed}`` row). Each caller decides what an empty set means for it.
    """
    for user in users:
        if only_user_ids is not None and user["id"] not in only_user_ids:
            continue
        # The DEFAULT row has no per-collection template, so each user's title is their own
        # `row_name_tpl` override or the global one — the same precedence delivery resolves.
        override = user["prefs"].get("row_name_tpl") if slug == DEFAULT_SLUG else None
        displays = _rendered_titles(ctx, user, override or template, slug)
        displays |= set(titles_by_user.get(user["id"], {}))  # the library is only of interest to rename
        action(user, displays)


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
) -> bool:
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
    gate-exempt. Runs in an executor.

    Returns the EFFECTIVE dry-run value — the one the context imposed, which is not the one the caller
    passed whenever ``SHORTLIST_DRY_RUN`` is set. Callers audit what came back, never what they sent."""
    ctx = state.run_service.build_context(dry_run=dry_run, plex_only=True)
    # The chokepoint ORs SHORTLIST_DRY_RUN in, so the context's value is the one that governs below.
    # `or dry_run` is the floor: it may force a preview ON, never off — a caller who asked to be shown
    # what this WOULD delete must never have it deleted.
    dry_run = ctx.config.dry_run or dry_run
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
        return dry_run
    with state.sessions() as session:
        keys_by_user = _ledger_keys(session, slug)
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = _users_data(session)
        if template is None:
            template = row_template(session, slug, state.secrets)
    swept: set[str] = set()

    def remove_for(user: dict, displays: set[str]) -> None:
        rating_keys = keys_by_user.get(user["slug"], set())
        if not displays and not rating_keys:
            return
        swept.add(user["slug"])
        removed.extend(
            remove_row_collections(
                ctx.plex,
                ctx.config,
                label=f"{LABEL_PREFIX}_{user['slug']}",
                displays=displays,
                rating_keys=rating_keys,
                dry_run=dry_run,
                in_sections=in_sections,
            )
        )

    _walk_row_collections(
        ctx,
        users,
        slug=slug,
        template=template,
        titles_by_user=titles_by_user,
        action=remove_for,
        only_user_ids=only_user_ids,
    )
    # The ledger records collections that EXIST. Only after a real removal — a dry run changed nothing,
    # and forgetting there would leave the next live attempt with no ledger to address by.
    if swept and not dry_run:
        with state.sessions() as session:
            _forget_deliveries(session, slug, swept, in_sections)
            session.commit()
    return dry_run


def _reconcile_poster_reset(state, *, slug: str, build: str, reset: list[str]) -> bool:
    """Revert a row's Plex collections to their default artwork after it switches to 'Plex default'.

    Shared rows go by their own label (one membership, any title); per-person rows are found by the
    same union `_reconcile_row_removal` uses — the titles this row's template renders to, plus whatever
    the latest run recorded — scoped to that user's own label, so it only ever touches OUR collections.
    Cosmetic + privacy-neutral, so gate-exempt. Runs in an executor.

    Returns the effective dry-run value, so the caller audits what actually governed the writes."""
    # This path is otherwise always live, so the only thing that can make it a preview is safe mode —
    # read back off the context rather than calling `force_dry_run()` here (one idiom, rule 8).
    ctx = state.run_service.build_context(dry_run=False, plex_only=True)
    dry_run = ctx.config.dry_run
    if build == "shared":
        reset.extend(
            reset_row_posters(
                ctx.plex, ctx.config, label=f"{SHARED_LABEL_PREFIX}{slug}", displays=None, dry_run=dry_run
            )
        )
        return dry_run
    with state.sessions() as session:
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = _users_data(session)
        template = row_template(session, slug, state.secrets)

    def reset_for(user: dict, displays: set[str]) -> None:
        if not displays:
            return
        reset.extend(
            reset_row_posters(
                ctx.plex,
                ctx.config,
                label=f"{LABEL_PREFIX}_{user['slug']}",
                displays=displays,
                dry_run=dry_run,
            )
        )

    _walk_row_collections(ctx, users, slug=slug, template=template, titles_by_user=titles_by_user, action=reset_for)
    return dry_run


async def run_poster_reset(state, *, slug: str, build: str, scope: str) -> tuple[list[str], str | None]:
    """Run ``_reconcile_poster_reset`` in an executor and audit it (rule 10). Best-effort — a Plex
    outage is recorded, never fatal to the PATCH. Returns ``(reset_library_titles, error)``."""
    reset: list[str] = []
    error: str | None = None
    # Seeded with what safe mode would impose, so an audit written after a failure BEFORE the context
    # was built still records the truth rather than a default.
    dry_run = force_dry_run()

    def _work() -> None:
        nonlocal dry_run
        dry_run = _reconcile_poster_reset(state, slug=slug, build=build, reset=reset)

    try:
        await asyncio.get_running_loop().run_in_executor(None, _work)
    except Exception as e:
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    write_audit(state, scope, "info", slug=slug, poster_reset=reset, dry_run=dry_run, error=error)
    logger.info("{} '{}': reset {} poster(s){}", scope, slug, len(reset), f" then FAILED: {error}" if error else "")
    return reset, error


async def run_reconcile(
    state, *, slug: str, build: str, dry_run: bool, scope: str, only_user_ids: set[int] | None = None
) -> tuple[list[str], str | None]:
    """Run ``_reconcile_row_removal`` in an executor and audit it (rule 10) — even a mid-loop failure
    records what was already removed. Returns ``(removed, error)``."""
    removed: list[str] = []
    error: str | None = None
    # The EFFECTIVE value, not the caller's: `build_context` ORs SHORTLIST_DRY_RUN in below this, so
    # auditing the parameter recorded a preview as a real deletion. Seeded the same way for the case
    # where the executor raises before a context exists.
    effective_dry_run = force_dry_run() or dry_run

    def _work() -> None:
        nonlocal effective_dry_run
        effective_dry_run = _reconcile_row_removal(
            state, slug=slug, build=build, dry_run=dry_run, removed=removed, only_user_ids=only_user_ids
        )

    try:
        await asyncio.get_running_loop().run_in_executor(None, _work)
    except Exception as e:  # a destructive write is never silent: audit the partial removal, then surface it
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    write_audit(state, scope, "warning", slug=slug, removed=removed, dry_run=effective_dry_run, error=error)
    logger.warning("{} '{}': {} collection(s){}", scope, slug, len(removed), f" then FAILED: {error}" if error else "")
    return removed, error


def reconcile_row_rename_iter(
    state,
    *,
    slug: str,
    new_template: str,
    old_template: str | None = None,
    old_display_names: dict[str, str] | None = None,
    build: str = "per_person",
    dry_run: bool = False,
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
    ctx = state.run_service.build_context(dry_run=dry_run, plex_only=True)
    dry_run = ctx.config.dry_run or dry_run  # the chokepoint may force a preview ON, never off
    total = 0

    if build == "shared":
        # A shared row is ONE collection carrying `shortlist__shared_<row>`, not one per person under
        # `shortlist_<slug>`. Walking the per-user labels found nothing and reported "renamed 0" —
        # a success message for work that never happened, while the collection on Plex kept its old
        # title and the database said otherwise.
        label = f"{SHARED_LABEL_PREFIX}{slug}"
        for section in ctx.plex.sections():
            lib_name = getattr(section, "title", "") or ""
            new_display = render_row_name(new_template, _shared_profile(), [], library_name=lib_name)
            if new_display == DEFAULT_ROW_NAME:
                continue
            for collection in ctx.plex.find_owned_collections(section, label):
                if collection.title == new_display:
                    continue
                try:
                    if not dry_run:
                        collection.editTitle(new_display)
                    total += 1
                    yield {
                        "user": slug,
                        "display_name": "Everyone",
                        "old": collection.title,
                        "new": new_display,
                        "libraries": [lib_name],
                    }
                except Exception as e:  # pragma: no cover - PMS failure shape
                    yield {"user": slug, "library": lib_name, "error": redact(str(e))}
        yield {"done": True, "total": total}
        return

    for udata in users_data:
        override = udata["prefs"].get("row_name_tpl") if slug == DEFAULT_SLUG else None
        effective_template = override or new_template
        effective_old = override or old_template
        profile = _profile_of(udata)
        # The profile the OLD title was rendered from. Identical to `profile` except after a rename of
        # the PERSON rather than the row, where the only thing that moved is what `{user}` renders to.
        was = old_display_names.get(udata["slug"]) if old_display_names else None
        old_profile = replace(profile, nickname=was) if was else profile
        label = f"{LABEL_PREFIX}_{udata['slug']}"
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
            # MANDATORY scoping. Every one of a person's rows shares the single label
            # `shortlist_<slug>`, so without the old title there is nothing distinguishing this row's
            # collection from their others — and renaming "whatever we find" would retitle a DIFFERENT
            # row's collection to this row's name. That row is then addressable by nothing: its ledger
            # entry points at a collection wearing another row's title, so the next run builds a
            # duplicate beside it and the original stays labelled and promoted for ever.
            #
            # `old_display` was allowed to be None whenever `effective_old` was falsy — and "" is
            # falsy, which both `RenameRequest.old_template`'s default and the PATCH's
            # `old_template or ""` produce. Skip instead: renaming nothing is recoverable, renaming
            # the wrong row is not.
            if not old_display:
                logger.warning(
                    "rename: skipping {} — no previous title to match on, so this row's collections "
                    "cannot be told apart from their other rows'",
                    udata["slug"],
                )
                continue
            for collection in ctx.plex.find_owned_collections(section, label):
                current_title = collection.title
                if current_title == new_with_marker:
                    continue
                # Scope to THIS row: only rename collections whose stripped title matches what this
                # row USED to render as.
                if strip_marker(current_title) != old_display:
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
    write_audit(state, scope, "info", slug=slug, renames=entries, new_template=new_template, error=error)
    logger.info(
        "{} '{}': renamed {} collection(s){}", scope, slug, len(entries), f" then FAILED: {error}" if error else ""
    )
    return entries, error
