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
from dataclasses import dataclass, replace
from datetime import date

from loguru import logger

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.delivery import (
    DEFERRED,
    HELD,
    KEPT,
    REBUILD,
    is_name_freeing_helper,
    remove_row_collections,
    rename_or_keep,
    render_row_name,
    reset_row_posters,
    resolve_row_template,
    row_marker,
    rows_can_share_a_library,
    strip_marker,
    titles_other_rows_build,
)
from shortlist.engine.models import (
    LABEL_PREFIX,
    SHARED_LABEL_PREFIX,
    SHARED_SLUG_PREFIX,
    EngineConfig,
    RowSeason,
    RowSpec,
    UserProfile,
    UserType,
)
from shortlist.engine.pipeline import identity_map
from shortlist.engine.placeholders import (
    catalogue_seasons,
    fill_season,
    names_a_seed,
    needs_a_run,
    season_renderings,
    uses_season,
)
from shortlist.server.db.models import DEFAULT_SLUG, Collection, Delivery, Run, User
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.services import jobs
from shortlist.server.services.audit import write_audit
from shortlist.server.services.context_builder import ContextBuilder
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


@dataclass(frozen=True)
class _OtherRows:
    """What this person's OTHER rows are titled, read once so the Plex walk runs outside the session."""

    specs: list[RowSpec]
    global_template: str
    #: {(user slug, row slug) -> {(library key, title)}} as the delivery ledger last recorded them.
    delivered: dict[tuple[str, str], set[tuple[str, str]]]


def _other_rows(session, secrets, slug: str) -> _OtherRows:
    """Every OTHER enabled per-person row: as the specs `titles_other_rows_build` renders, and as the
    titles the delivery ledger last recorded them wearing.

    Only enabled rows: a switched-off row builds nothing, and its own collections are on their way out
    by the same removal this guards.
    """
    account_by_user, audience_by_collection = ContextBuilder._audience_maps(session)
    specs = [
        RowSpec(
            slug=other.slug,
            # Who it builds for, resolved exactly as a run resolves it: a row that builds nothing for a
            # person claims no title of theirs.
            audience=ContextBuilder._subset_audience(other, account_by_user, audience_by_collection),
            # The default row's title is the global template (or that user's own override), which
            # `resolve_row_template` supplies from the profile and config when this is left empty.
            name_template="" if other.slug == DEFAULT_SLUG else (other.name_template or other.name),
            size=0,
            media=other.media,
            library_keys=[str(k) for k in (other.library_keys or [])],
            fallback_name=other.fallback_name or "",
        )
        for other in session.query(Collection).filter_by(enabled=True, build="per_person")
        if other.slug != slug
    ]
    delivered: dict[tuple[str, str], set[tuple[str, str]]] = {}
    slugs = {spec.slug for spec in specs}
    for row in session.query(Delivery).filter(Delivery.title != ""):
        if row.collection_slug in slugs:
            delivered.setdefault((row.user_slug, row.collection_slug), set()).add((row.library_key, row.title))
    return _OtherRows(specs, SettingsStore(session, secrets).get("row.name_template") or "", delivered)


def _claimed_titles(ctx, udata: dict, other_rows: _OtherRows) -> set[tuple[str, str]]:
    """``{(section key, display)}`` another of this person's rows builds under — never this row's (#121).

    All of a person's rows share one label and marker, so every title match below is only as good as
    this: a Movies-only and a TV-only row may share a title, and matching it in every library deleted,
    renamed or reset the other row's collection.
    """
    profile = replace(_profile_of(udata), row_name_template=udata["prefs"].get("row_name_tpl"))
    config = EngineConfig(row_name_template=other_rows.global_template)
    claimed = titles_other_rows_build(ctx.plex.sections(), profile, config, other_rows.specs, slug="")
    # A `{top_seed}` title cannot be rendered without picks, so rendering never claims one — yet two such
    # rows seeded by one watch wear the same title in different libraries. The ledger records what each
    # was last delivered as, in which library, whichever run that was. Here it is read as a CLAIM only, and
    # only for `{top_seed}` rows: a static title is claimed by rendering already, and its ledger title goes
    # stale on a rename, which writes no ledger entry. (Renaming a `{top_seed}` row reads it as the old title,
    # but selects by the ledger KEY — `reconcile_row_rename_iter`.)
    for spec in other_rows.specs:
        if spec.audience is not None and profile.plex_account_id not in spec.audience:
            continue
        template = resolve_row_template(spec, profile, config)
        if needs_a_run(template):
            claimed |= other_rows.delivered.get((udata["slug"], spec.slug), set())
    return claimed


#: A library name no real library has, for `title_key`. NOT the empty string: `render_row_name`
#: collapses a `{library_name}` template to the bare default when there is no library, so an empty
#: probe would report "✨ {library_name} Picked for You" — the DEFAULT row's own template — as
#: occupying DEFAULT_ROW_NAME, and every `{top_seed}` row would then be refused against it. They do
#: not collide: the default row renders "✨ Movies Picked for You" in a real library, the `{top_seed}`
#: row renders the bare default. A non-empty sentinel keeps the two apart.
_PROBE_LIBRARY = "\x00library\x00"

#: Stub whose only job is to let `render_row_name` resolve `{user}`. A non-empty username stops a
#: "{user}" template collapsing to empty — same stub `context_builder._retired_rows` uses.
_PROBE_PROFILE = UserProfile(username="_probe_", plex_account_id=0, user_type=UserType.SHARED)

#: A season no real one is called, for `title_key`, for the same reason as `_PROBE_LIBRARY`: left
#: unfilled, every name using `{season}` renders to "" and every seasonal row would clash with every other.
_PROBE_SEASON = RowSeason(slug="_probe_", name="\x00season\x00", emoji="\x00emoji\x00", anchor=date(2000, 1, 1))


def title_key(template: str) -> str:
    """The identity two rows collide on: what ``template`` RENDERS to, case-folded.

    Rendering rather than comparing the raw template, because `render_row_name` maps several distinct
    templates onto ONE title and each of those pairs is a real collision the raw comparison missed:

    * a ``{top_seed}`` template with no seed renders to "" (issue #84 — no name is invented), so two
      such rows — or one of them and a row literally named "✨ Picked for You" — are one collection
      for everyone who has too little history to seed them;
    * a blank or whitespace-only template does the same, and nothing refused a blank
      ``row.name_template`` (now `_validate_values` does);
    * whitespace is collapsed for a ``{library_name}`` template, so "{library_name}  Picks" and
      "{library_name} Picks" render identically.

    All three are template-local — no Plex read, no library list. What it still cannot see is two
    templates that differ only OUTSIDE the placeholder and happen to agree in one library
    ("{library_name} Picks" vs "Movies Picks"), which would need the real library names.
    """
    probe = fill_season(template or "", _PROBE_SEASON)
    return render_row_name(probe, _PROBE_PROFILE, [], library_name=_PROBE_LIBRARY).casefold()


def title_keys(template: str) -> set[str]:
    """Every key ``template`` can collide on: `title_key` itself, plus, for a seasonal name, the title it
    renders to in each season. In December `{season} picks` IS "Christmas picks", so a plain row with that
    name would share its collection. Every catalogue season rather than only the row's own: refusing a
    few names too many is recoverable, one collection for two rows is not."""
    keys = {title_key(template)} | {title_key(rendering) for rendering in season_renderings(template or "")}
    return {key for key in keys if key}


def _title_keys(session, collection: Collection, secrets) -> set[str]:
    """Every title this row can end up with: from its own template, and from its fallback name.

    Both, because a row now has two ways to be named (issue #84) and either can collide. Empties are
    dropped — a `{top_seed}` row with no fallback renders to nothing for a person with no watch, and
    "no title" cannot clash with "no title": neither row is built for them.
    """
    keys = title_keys(row_template(session, collection.slug, secrets)) | {title_key(collection.fallback_name or "")}
    return {k for k in keys if k}


def row_titled_from(
    session,
    template: str,
    *,
    secrets=None,
    exclude_slug: str = "",
    build: str = "",
    fallback_name: str = "",
    media: str = "both",
    library_keys=(),
) -> Collection | None:
    """The first of `rows_titled_from`, or None."""
    clashes = rows_titled_from(
        session,
        template,
        secrets=secrets,
        exclude_slug=exclude_slug,
        build=build,
        fallback_name=fallback_name,
        media=media,
        library_keys=library_keys,
    )
    return clashes[0] if clashes else None


def rows_titled_from(
    session,
    template: str,
    *,
    secrets=None,
    exclude_slug: str = "",
    build: str = "",
    fallback_name: str = "",
    media: str = "both",
    library_keys=(),
) -> list[Collection]:
    """The rows whose collections are ALREADY titled from ``template`` in a library this row could reach.

    The clash test for every path that sets a row title. Two rows resolving to one template render to
    one title, and `delivery._find_this_rows_collection` matches a per-person row by title alone (they
    all share the `shortlist_<userslug>` label) — so the second row to deliver finds the first's
    collection and overwrites its picks, `context_builder._delivered_keys` drops the ratingKey both now
    claim, and removing either row deletes the collection the other is still using.

    Compares the EFFECTIVE template via `row_template`, never the `name` column, because for the
    DEFAULT row those are two different strings: its column reads "✨ Picked for You" (migration 0001)
    while its title comes from the global `row.name_template`, "✨ {library_name} Picked for You". A
    column-level check therefore saw no clash when the row-template gallery's "Picked for You" tile —
    which writes that global string into `name` — was added to a server that still had the default row,
    and the two silently shared one collection per user per library.

    ``exclude_slug`` is the row being edited, which must not clash with itself.

    ``media`` and ``library_keys`` are where the incoming row builds; a row that can never build in any
    of the same libraries is skipped (issue #121). A title identifies a per-person row only within one
    library: delivery matches within the library it writes to, and the removal, rename, poster and
    placement paths refuse a title another row builds under there. The defaults ("both", every
    library) overlap everything, which is the old server-wide check. `rows_can_share_a_library` is
    static and errs towards "they can", so a library added to the server later cannot reopen the trap.

    ``build`` is the build of the row being written; a row of the OTHER build is skipped. A shared row
    and a per-person row cannot become one collection however alike their titles: they carry different
    invisible markers (`row_marker(0)` vs `row_marker(account_id)`, delivery.py:290) and file under
    different labels (`shortlist__shared_<slug>` vs `shortlist_<userslug>`), and
    `_find_this_rows_collection` only ever searches within one label. Refusing that pair was a false
    positive whose message asserted a collision that cannot happen. Two rows of the SAME build are
    still refused — including two shared rows, whose labels do differ, because one Plex library
    holding two identically-titled collections is a trap for the owner even when the engine copes.

    What this cannot see: the per-user `row_name_tpl` override, which `resolve_row_template` places
    between a row's own template and the global one. It applies only to the default row, and only for
    the one user who set it, so a clash through that door is per-user and invisible to a server-wide
    check.
    """
    # Both of the incoming row's possible titles, for the same reason `_title_keys` collects both.
    wanted_keys = title_keys(template) | {k for k in (title_key(fallback_name),) if k}
    # An unrenderable template has no title to collide on. Since issue #84 that includes every
    # `{top_seed}` template, which renders to "" without picks — an improvement: they all used to
    # render the same substitute name and so were refused against each other and against any row
    # genuinely titled that. A `{top_seed}` row's real collision is between two PEOPLE-less renders
    # at delivery time, which `_run_user` logs when it happens.
    if not wanted_keys:
        return []
    clashes: list[Collection] = []
    for other in session.query(Collection).all():
        if other.slug == exclude_slug:
            continue
        if build and other.build and other.build != build:
            continue
        if not rows_can_share_a_library(media, library_keys, other.media or "both", other.library_keys or []):
            continue
        if _title_keys(session, other, secrets) & wanted_keys:
            clashes.append(other)
    return clashes


def _ledger_titles(session, slug: str) -> dict[tuple[str, str], str]:
    """{(user slug, library key) -> the title this row was last delivered under there}, from the ledger.

    Read only as the OLD title of a `{top_seed}` row being renamed, which nothing else can supply.
    """
    return {
        (row.user_slug, row.library_key): row.title
        for row in session.query(Delivery).filter_by(collection_slug=slug)
        if row.title
    }


def _ledger_keys(session, slug: str) -> dict[str, set[int]]:
    """{user slug -> the Plex ratingKeys the ledger says this row built for them}.

    The PRIMARY way a per-person collection is found. It is the only source that survives a title
    changing, which a ``{top_seed}`` row's does every single run — rendering the template cannot help
    there, and the run breakdown it used to fall back on is scoped to one run and erased by
    ``DELETE /api/runs``.

    Empty for a row delivered before the ledger existed, or never delivered at all; the title-based
    sources below then carry it, exactly as they did before.

    A ratingKey another row also claims for that person is DROPPED, through the same
    `pipeline.identity_map` a run uses. These keys select collections to delete, and an ambiguous one is
    reachable: a leftover copy of this row that a same-titled row in that library adopted is recorded
    under both (issue #121). Dropped, it falls to the title match, which refuses the other row's title.
    """
    rows = list(session.query(Delivery).filter(Delivery.rating_key != 0))
    unambiguous = identity_map({(d.user_slug, d.collection_slug, d.library_key): d.rating_key for d in rows})
    keys: dict[str, set[int]] = {}
    for user_slug, by_key in unambiguous.items():
        for rating_key, row_slug in by_key.items():
            if row_slug == slug:
                keys.setdefault(user_slug, set()).add(rating_key)
    dropped = {d.rating_key for d in rows if d.collection_slug == slug} - {k for ks in keys.values() for k in ks}
    if dropped:
        # A `{top_seed}` row has no other handle, so its collection may now stay on the server — say so,
        # or a row that survived its own removal leaves no trace of why.
        logger.warning(
            "row '{}': {} ledger key(s) are also claimed by another row, so they will not select anything "
            "for removal — those collections are matched by title only",
            slug,
            len(dropped),
        )
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
    # A seasonal name is the same case: it renders tonight's season, and the collection may still wear the
    # last one it was built for (discussion #124).
    if not template or needs_a_run(template):
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
            # The ledger records collections that EXIST, as the per-person branch below keeps it — and only after
            # a real removal. A kept key is handed dead to the row's next delivery, credits plays to a collection
            # that is gone (`watch_events._shared_on_plex`), and, once Plex reuses it, makes another row's key
            # ambiguous so that row loses its handle too.
            if not dry_run:
                with state.sessions() as session:
                    _forget_deliveries(session, slug, {f"{SHARED_SLUG_PREFIX}_{slug}"}, in_sections)
                    session.commit()
        return dry_run
    with state.sessions() as session:
        keys_by_user = _ledger_keys(session, slug)
        titles_by_user = _delivered_titles_by_user(session, slug)
        users = _users_data(session)
        if template is None:
            template = row_template(session, slug, state.secrets)
        other_rows = _other_rows(session, state.secrets, slug)

    def remove_for(user: dict, displays: set[str]) -> None:
        rating_keys = keys_by_user.get(user["slug"], set())
        if not displays and not rating_keys:
            return
        removed.extend(
            remove_row_collections(
                ctx.plex,
                ctx.config,
                label=f"{LABEL_PREFIX}_{user['slug']}",
                displays=displays,
                rating_keys=rating_keys,
                dry_run=dry_run,
                in_sections=in_sections,
                claimed_titles=_claimed_titles(ctx, user, other_rows),
            )
        )
        # The ledger records collections that EXIST. Only after a real removal — a dry run changed nothing,
        # and forgetting there would leave the next live attempt with no ledger to address by. Per person, as
        # soon as theirs returns: a walk that fails on someone later must not keep entries for collections it
        # already deleted, since the nightly sweep of a shared row's per-person copies tries every such key.
        if not dry_run:
            with state.sessions() as session:
                _forget_deliveries(session, slug, {user["slug"]}, in_sections)
                session.commit()

    _walk_row_collections(
        ctx,
        users,
        slug=slug,
        template=template,
        titles_by_user=titles_by_user,
        action=remove_for,
        only_user_ids=only_user_ids,
    )
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
        other_rows = _other_rows(session, state.secrets, slug)

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
                claimed_titles=_claimed_titles(ctx, user, other_rows),
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


async def preview_row_removal(
    state,
    *,
    slug: str,
    build: str,
    only_user_ids: set[int] | None = None,
    in_sections: set[str] | None = None,
    template: str | None = None,
) -> tuple[list[str], str | None]:
    """Which collections a removal WOULD strip, without removing them. Returns ``(titles, error)``.

    The read-only sibling of :func:`run_reconcile`, which cannot answer this: it takes neither
    ``in_sections`` — the whole subject of a NARROWING preview, where the row keeps the libraries it
    still targets — nor ``template``, which the delete preview needs because the row it names is
    about to stop existing.

    ``dry_run=True`` is passed, never computed. `_reconcile_row_removal`'s chokepoint may only
    STRENGTHEN it (``ctx.config.dry_run or dry_run``), so nothing — safe mode, a setting, a future
    caller — can turn this into a deletion. Nothing is audited either: plex-safety rule 10 records
    writes, and this makes none.

    Runs the walk in an executor because it is blocking Plex I/O across every library. It takes no
    lock, and needs none: ``jobs.plex_writer_lock`` serialises Plex WRITES and is held AROUND
    `_reconcile_row_removal` by the job worker rather than inside it, so a preview can neither
    deadlock against a live run nor perform the writes that lock exists to order.

    Args:
        state: The app state, for the Plex context and DB sessions.
        slug: The row whose collections would go.
        build: The row's build — ``shared`` goes by its own label, ``per_person`` per user.
        only_user_ids: Limit to these users' copies; ``None`` means everyone.
        in_sections: Limit to these section keys; ``None`` means every library.
        template: Override the title template read from the DB, for a row about to be deleted.

    Returns:
        The display titles that would be removed, and a redacted error string if the walk failed
        part-way (the titles found before it did are still returned).
    """
    removed: list[str] = []

    def _work() -> None:
        _reconcile_row_removal(
            state,
            slug=slug,
            build=build,
            dry_run=True,
            removed=removed,
            only_user_ids=only_user_ids,
            template=template,
            in_sections=in_sections,
        )

    try:
        await asyncio.get_running_loop().run_in_executor(None, _work)
    except Exception as e:
        return removed, redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    return removed, None


def reconcile_row_rename_iter(
    state,
    *,
    slug: str,
    new_template: str,
    old_template: str | None = None,
    old_display_names: dict[str, str] | None = None,
    build: str = "per_person",
    dry_run: bool = False,
    holds_writer_lock: bool = False,
):
    """Rename a row's collections on Plex, yielding one event per user renamed (for SSE streaming).

    ``holds_writer_lock`` is for a caller already inside the Plex writer lock (the roster sync). Everyone
    else renames outside it, so freeing a refused name waits while a run or writer job is writing.

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
    — with ``"next_run": True`` when nothing was renamed now and the next run gives it the name, in which
    case ``new`` may be the raw template (a ``{top_seed}`` name has no title until a run picks the seed) —
    and {"user", "library", "error"} for a per-collection PMS failure.
    At the end yields {"done": True, "total": n}.
    """
    may_free_name = None if holds_writer_lock else (lambda: not jobs.plex_writer_busy(state))
    with state.sessions() as session:
        users_data = _users_data(session)
        other_rows = _other_rows(session, state.secrets, slug)
        # Only an old `{top_seed}` name is matched through the ledger (see the walk), so only then is it read.
        seeded_old = names_a_seed(old_template or "") or (
            slug == DEFAULT_SLUG and any(names_a_seed(u["prefs"].get("row_name_tpl") or "") for u in users_data)
        )
        ledger_titles = _ledger_titles(session, slug) if seeded_old else {}
        ledger_keys = _ledger_keys(session, slug) if seeded_old else {}
    ctx = state.run_service.build_context(dry_run=dry_run, plex_only=True)
    dry_run = ctx.config.dry_run or dry_run  # the chokepoint may force a preview ON, never off
    total = 0

    if build == "shared":
        # A shared row is ONE collection carrying `shortlist__shared_<row>`, not one per person under
        # `shortlist_<slug>`. Walking the per-user labels found nothing and reported "renamed 0" —
        # a success message for work that never happened, while the collection on Plex kept its old
        # title and the database said otherwise.
        label = f"{SHARED_LABEL_PREFIX}{slug}"
        seasonal = uses_season(new_template) or uses_season(old_template or "")
        for section in ctx.plex.sections():
            lib_name = getattr(section, "title", "") or ""
            # The label alone identifies a shared row's collection, so a plain name needs no old title. A
            # seasonal one does: it is the only way to know which season the collection wears.
            renamed = (
                _renamed_titles(old_template or "", new_template, _shared_profile(), _shared_profile(), lib_name)
                if seasonal
                else None
            )
            new_display = (
                "" if seasonal else render_row_name(new_template, _shared_profile(), [], library_name=lib_name)
            )
            if not seasonal and not new_display:  # unnameable — see render_row_name
                continue
            owned = [c for c in ctx.plex.find_owned_collections(section, label) if not is_name_freeing_helper(c.title)]
            # A shared row wears `row_marker(0)`, as delivery writes it (`deliver_rows`), and delivery finds it
            # again only by that marked title or by the marker. When a marked one is here, it is the row, and an
            # unmarked collection under the same label is a copy an older rename left: renamed first, it would
            # take the name and the next run would adopt it. A lone unmarked one IS the row, and gets its marker back.
            if any(c.title.endswith(row_marker(0)) for c in owned):
                owned = [c for c in owned if c.title.endswith(row_marker(0))]
            for collection in owned:
                old_title = strip_marker(collection.title)
                if renamed is not None:
                    if old_title not in renamed:
                        continue
                    new_display = renamed[old_title]
                    if new_display is None:  # a `{top_seed}` name: a shared row has no seed to be named after
                        continue
                if collection.title == new_display + row_marker(0):
                    continue
                try:
                    outcome = (
                        rename_or_keep(
                            ctx.plex,
                            collection,
                            new_display + row_marker(0),
                            _shared_profile(),
                            section,
                            label=label,
                            # Names a helper only: ours by marker even if its label write fails.
                            marker=row_marker(0),
                            read_spare_item=lambda c=collection: next(iter(c.items()), None),
                            may_free_name=may_free_name,
                        )
                        if not dry_run
                        else None
                    )
                    if outcome in (KEPT, HELD):
                        yield {
                            "user": slug,
                            "display_name": "Everyone",
                            "library": lib_name,
                            "error": _refusal(outcome, new_display, lib_name),
                        }
                        continue
                    event = {
                        "user": slug,
                        "display_name": "Everyone",
                        "old": old_title,
                        "new": new_display,
                        "libraries": [lib_name],
                    }
                    if outcome in (REBUILD, DEFERRED):
                        event["next_run"] = True
                    else:
                        total += 1
                    yield event
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
        claimed = _claimed_titles(ctx, udata, other_rows)
        for section in ctx.plex.sections():
            lib_name = getattr(section, "title", "") or ""
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
            if not effective_old:
                logger.warning(
                    "rename: skipping {} — no previous title to match on, so this row's collections "
                    "cannot be told apart from their other rows'",
                    udata["slug"],
                )
                continue
            renamed = _renamed_titles(
                effective_old,
                effective_template,
                old_profile,
                profile,
                lib_name,
                recorded=ledger_titles.get((udata["slug"], str(section.key))),
            )
            if not renamed:  # unnameable — see render_row_name and `_renamed_titles`
                continue
            for collection in ctx.plex.find_owned_collections(section, label):
                current_title = collection.title
                # Scope to THIS row: only rename collections whose stripped title matches what this
                # row USED to render as.
                old_display = strip_marker(current_title)
                if old_display not in renamed:
                    continue
                if names_a_seed(effective_old) and int(collection.ratingKey) not in ledger_keys.get(
                    udata["slug"], set()
                ):
                    # Its old title came from the ledger, which a static rename does not update — so the
                    # title can be worn by another collection by now. The ledger KEY says which is this row's.
                    continue
                new_display = renamed[old_display]
                if new_display is not None and current_title == new_display + marker:
                    continue
                if (str(section.key), old_display) in claimed:
                    # Another of this person's rows builds here under that very title (issue #121), so
                    # this is ITS collection, however well the old title matches.
                    continue
                if new_display is None:
                    # Named after a watch only a run can pick. Delivery finds this copy by its ledger key and
                    # retitles it then, so say so rather than count it as nothing to do.
                    yield {
                        "user": udata["slug"],
                        "display_name": profile.display_name,
                        "old": old_display,
                        "new": effective_template,
                        "libraries": [lib_name],
                        "next_run": True,
                    }
                    continue
                new_with_marker = new_display + marker
                try:
                    outcome = (
                        rename_or_keep(
                            ctx.plex,
                            collection,
                            new_with_marker,
                            profile,
                            section,
                            label=label,
                            marker=marker,
                            read_spare_item=lambda c=collection: next(iter(c.items()), None),
                            may_free_name=may_free_name,
                        )
                        if not dry_run
                        else None
                    )
                    if outcome in (KEPT, HELD):
                        yield {
                            "user": udata["slug"],
                            "display_name": profile.display_name,
                            "library": lib_name,
                            "error": _refusal(outcome, new_display, lib_name),
                        }
                        continue
                    event = {
                        "user": udata["slug"],
                        "display_name": profile.display_name,
                        "old": strip_marker(current_title),
                        "new": new_display,
                        "libraries": [lib_name],
                    }
                    if outcome in (REBUILD, DEFERRED):
                        # Plex lets only a new collection share a name their row in another library has, and a
                        # rename has no titles to build one from; or freeing the name waits for a run that is
                        # writing. Either way the next run gives the row this name.
                        event["next_run"] = True
                    else:
                        total += 1
                    yield event
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


def _renamed_titles(
    old_template: str,
    new_template: str,
    old_profile: UserProfile,
    profile: UserProfile,
    library_name: str,
    *,
    recorded: str | None = None,
) -> dict[str, str | None]:
    """{title the row may be wearing -> the title it takes}, for one library. None: the next run names it.

    One pair for a plain name. A seasonal name (discussion #124) renders only with a season, and a row keeps
    the title of the season it was last built for — out of season too — so it is one pair per catalogue
    season, old and new filled with the SAME season. A plain name becoming seasonal gets no pair: nothing
    says which season the collection should take, and guessing could put Valentine's Day on it in December.
    The next run names it.

    A `{top_seed}` name renders to nothing without picks. As the OLD name, the title it wears is `recorded`:
    what the ledger says this person's copy was last delivered as in this library. As the NEW name, it has no
    title until a run picks the seed, so every title the row may be wearing maps to None.
    """
    seeded = names_a_seed(new_template)
    if seeded and old_template == new_template and old_profile.display_name == profile.display_name:
        return {}  # renders exactly as before, so nothing is taking a new name
    seasons = catalogue_seasons() if uses_season(old_template) else [None]
    pairs: dict[str, str | None] = {}
    for season in seasons:
        old = render_row_name(fill_season(old_template, season), old_profile, [], library_name=library_name)
        new = (
            None
            if seeded
            else render_row_name(fill_season(new_template, season), profile, [], library_name=library_name)
        )
        if old and new != "":
            pairs.setdefault(old, new)
    if recorded and names_a_seed(old_template):
        new = None if seeded else render_row_name(new_template, profile, [], library_name=library_name)
        if new != "":
            pairs.setdefault(recorded, new)
    return pairs


def _refusal(outcome: str, name: str, library: str) -> str:
    """Why a rename left the old name, in words that are true for this outcome."""
    if outcome == HELD:
        return (
            f"Plex refused '{name}' in {library}: something in that library already has that name, so the row "
            "keeps its old name there."
        )
    return (
        f"Plex refused '{name}' in {library} and Shortlist could not free the name, so the row keeps its old "
        "name there for now. The next run tries again."
    )


async def run_row_rename_from_plex(
    state,
    *,
    slug: str,
    new_template: str,
    old_template: str,
    scope: str,
    old_display_names: dict[str, str] | None = None,
    holds_writer_lock: bool = False,
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
            holds_writer_lock=holds_writer_lock,
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
        "{} '{}': renamed {} collection(s){}",
        scope,
        slug,
        sum(1 for entry in entries if not entry.get("next_run")),
        f" then FAILED: {error}" if error else "",
    )
    return entries, error
