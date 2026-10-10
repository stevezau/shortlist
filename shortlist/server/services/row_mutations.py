"""Transaction-owned row writes shared by REST and assistant adapters.

These helpers mutate only the supplied SQLAlchemy session.  They never commit,
open another session, acquire the Plex writer lock, or perform network I/O.
Callers persist the returned convergence steps in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from shortlist.engine.placeholders import uses_season
from shortlist.server.assistant.row_effects import (
    poster_reset_step,
    privacy_sync_step,
    reconcile_step,
    row_rename_step,
    schedule_rebuild_step,
    visibility_step,
)
from shortlist.server.db.models import DEFAULT_SLUG, Collection, CollectionAudience, Theme
from shortlist.server.services import poster_service
from shortlist.server.services.row_changes import (
    POSTER_RESET,
    PRIVACY_SYNC,
    RECONCILE,
    RENAME,
    VISIBILITY,
    PlannedWork,
    plan_row_changes,
)
from shortlist.server.services.row_editing import (
    EXPLORE_COLUMNS,
    REQUEST_COLUMNS,
    TITLE_MOVING_FIELDS,
    CollectionIn,
    apply_row_patch,
    known_seasons,
    projected_snapshot,
    reject_duplicate_name,
    reject_new_person_title_clash,
    reject_season_name_without_seasons,
    row_change,
    row_snapshot,
    serialize_row,
    set_audience,
    stored_instructions,
    stranded_sections,
    unattributed_theme_tokens,
    unique_slug,
    validate_anchor_rows,
    validate_audience_ids,
    validate_explore,
    validate_pairing,
    validate_requests_row,
    validate_row,
    validate_theme,
)


@dataclass(frozen=True)
class DeletedRow:
    """Facts captured before a row disappears and needed by durable effects."""

    id: int
    slug: str
    build: str
    template: str
    anchors_cleared: tuple[str, ...]


def set_ai_paused_in_session(session: Session, collection: Collection, paused: bool) -> None:
    """Pause theme generation without changing saved picks or making external calls."""
    from shortlist.server.services.audit import add_audit

    if collection.theme_id is None:
        raise HTTPException(status_code=422, detail="Only an AI row has AI to pause.")
    if collection.ai_paused != paused:
        collection.ai_paused = paused
        add_audit(session, "collection.ai_pause", "info", slug=collection.slug, paused=paused)


def validate_create_row_in_session(session: Session, secrets, body, *, trusted_theme: Theme | None = None):
    """Validate a proposed row using only reads and return its resolved theme."""
    from shortlist.server.services.season_catalogue import load_catalogue
    from shortlist.server.services.theme_store import spec_from_row

    if body.dry_run:
        raise HTTPException(status_code=422, detail="dry_run is only supported on PATCH and DELETE")
    validate_row(body)
    reject_season_name_without_seasons(
        body.name_template or body.name,
        body.seasons,
        row_has_theme=body.theme_id is not None or trusted_theme is not None,
    )
    catalogue = load_catalogue(session)
    body.seasons = known_seasons(body.seasons, catalogue=catalogue)
    theme = validate_theme(
        session,
        body.theme_id,
        build=body.build,
        seasons=body.seasons,
        rewatch=body.rewatch,
        requests_row=body.requests_row,
        trusted_theme=trusted_theme,
    )
    validate_explore(
        session,
        {column: getattr(body, column) for column in EXPLORE_COLUMNS},
        theme_id=None if theme is None else theme.id,
        own_slug="",
        has_theme=theme is not None,
    )
    template = body.name_template or body.name
    reject_duplicate_name(
        session,
        secrets,
        template,
        build=body.build,
        fallback_name=body.fallback_name,
        media=body.media,
        library_keys=body.library_keys,
        theme=None if theme is None else spec_from_row(theme),
    )
    validate_anchor_rows(session, body, editing_slug="")
    return theme


def create_row_in_session(session: Session, secrets, body) -> Collection:
    """Validate and insert one ``CollectionIn`` without committing or external I/O."""
    from shortlist.engine.models import slugify

    theme = validate_create_row_in_session(session, secrets, body)
    collection = Collection(
        slug=unique_slug(session, slugify(body.name)),
        name=body.name,
        build=body.build,
        audience=body.audience,
        enabled=body.enabled,
        theme_id=None if theme is None else theme.id,
        **{column: getattr(body, column) for column in EXPLORE_COLUMNS},
        schedule=body.schedule.strip(),
        size=body.size,
        media=body.media,
        sort_order=body.sort_order,
        name_template=body.name_template,
        fallback_name=body.fallback_name,
        min_watchers=body.min_watchers,
        request_tag=body.request_tag.strip(),
        candidate_sources=body.candidate_sources,
        watched_pct=body.watched_pct,
        rewatch=body.rewatch,
        rewatch_cooldown_days=body.rewatch_cooldown_days,
        requests_row=body.requests_row,
        requests_window_days=body.requests_window_days,
        requests_tag_pattern=body.requests_tag_pattern.strip(),
        unstarted_only=body.unstarted_only,
        refresh_days=body.refresh_days,
        idle_hold_days=body.idle_hold_days,
        recency=body.recency,
        recent_count=body.recent_count,
        max_seeds=body.max_seeds,
        max_runtime=body.max_runtime,
        min_year=body.min_year,
        max_year=body.max_year,
        min_rating=body.min_rating,
        cold_start=body.cold_start,
        seed_window=body.seed_window,
        pick_order=body.pick_order,
        placement=body.placement,
        show_days=body.show_days,
        seasons=body.seasons,
        season_lead_days=body.season_lead_days,
        season_after_days=body.season_after_days,
        placement_friends=body.placement_friends,
        pin_top=body.pin_top,
        hub_anchor={key: value.model_dump() for key, value in body.hub_anchor.items()},
        library_keys=body.library_keys,
        poster=body.poster.model_dump(),
        prompt=stored_instructions(body.ai_instructions),
        description=body.description,
        sort_title_prefix=body.sort_title_prefix,
        **{column: getattr(body, column) for column in REQUEST_COLUMNS},
    )
    collection.ai_tokens = unattributed_theme_tokens(session, theme, exclude_id=None)
    session.add(collection)
    session.flush()
    set_audience(session, collection, body)
    session.flush()
    return collection


def update_row_in_session(
    session: Session,
    secrets,
    collection_id: int,
    values: dict,
    *,
    library_sections: list | None = None,
    apply: bool = True,
) -> tuple[Collection, list[dict], dict]:
    """Validate and patch one row, returning its ordered durable steps.

    ``library_sections`` is an externally read snapshot supplied before this
    transaction.  A narrowing that cannot be resolved from such a snapshot is
    refused rather than guessing which Plex collections may be deleted.
    """
    from shortlist.server.services.collection_reconcile import row_template, season_title
    from shortlist.server.services.season_catalogue import load_catalogue
    from shortlist.server.services.theme_store import spec_from_row
    from shortlist.server.settings_store import SettingsStore

    unknown = set(values) - set(CollectionIn.model_fields)
    if unknown:
        raise ValueError(f"unknown row fields: {sorted(unknown)}")
    if not values:
        raise ValueError("choose at least one row field to update")
    collection = session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    catalogue = load_catalogue(session)
    current = serialize_row(session, collection, catalogue=catalogue)
    merged = {
        key: current[key]
        for key in CollectionIn.model_fields
        if key in current and key not in {"dry_run", "defer_rename"}
    }
    merged.update({"dry_run": False, "defer_rename": False})
    merged.update(values)
    body = CollectionIn.model_validate(merged)
    sent = set(values)
    validate_row(body)
    if "seasons" in sent:
        body.seasons = known_seasons(body.seasons, catalogue=catalogue)
    validate_anchor_rows(session, body, editing_slug=collection.slug)
    before = row_snapshot(session, collection)
    is_default = collection.slug == DEFAULT_SLUG
    merged_theme_id = body.theme_id
    if is_default and merged_theme_id is not None:
        raise HTTPException(
            status_code=422,
            detail="The default row can't be an AI row — add a new row from the AI template.",
        )
    theme = validate_theme(
        session,
        merged_theme_id,
        build=body.build,
        seasons=body.seasons,
        rewatch=body.rewatch,
        requests_row=body.requests_row,
    )
    validate_explore(
        session,
        {column: getattr(body, column) for column in EXPLORE_COLUMNS},
        theme_id=merged_theme_id,
        own_slug=collection.slug,
        already_avoided=tuple(collection.avoid_rows or ()),
    )
    merged_spec = None if theme is None else spec_from_row(theme)
    if is_default:
        if body.seasons:
            raise HTTPException(
                status_code=422,
                detail="The default row can't follow seasons — add a new row from the Seasonal template.",
            )
        reject_season_name_without_seasons(body.name, [])
    else:
        reject_season_name_without_seasons(
            body.name_template or body.name,
            body.seasons,
            row_has_theme=merged_theme_id is not None,
        )
    generic_title_fields = {"name", "name_template", "fallback_name", "build", "media", "library_keys"}
    if sent & generic_title_fields:
        reject_duplicate_name(
            session,
            secrets,
            row_template(session, collection.slug, secrets) if is_default else (body.name_template or body.name),
            exclude_slug=collection.slug,
            build=body.build,
            fallback_name=body.fallback_name,
            media=body.media,
            library_keys=body.library_keys,
            theme=merged_spec,
        )
    if "seasons" in sent and not is_default:
        template = body.name_template or body.name
        ticked = [slug for slug in body.seasons if slug not in (collection.seasons or [])]
        for season_slug in ticked if uses_season(template) else []:
            reject_duplicate_name(
                session,
                secrets,
                season_title(template, catalogue[season_slug]),
                exclude_slug=collection.slug,
                build=body.build,
                media=body.media,
                library_keys=body.library_keys,
                theme=merged_spec,
            )
    if sent & TITLE_MOVING_FIELDS and not is_default:
        reject_new_person_title_clash(
            session,
            secrets,
            collection,
            body,
            sent,
            media=body.media,
            library_keys=body.library_keys,
            theme=merged_spec,
        )
    validate_pairing(rewatch=body.rewatch, unstarted_only=body.unstarted_only, media=body.media)
    validate_requests_row(
        requests_row=body.requests_row,
        build=body.build,
        rewatch=body.rewatch,
        seasons=body.seasons,
        has_theme=merged_theme_id is not None,
    )
    if sent & {"audience", "audience_user_ids"}:
        validate_audience_ids(session, body)
    narrowing = (before["media"], before["libraries"]) != (body.media, tuple(str(k) for k in body.library_keys))
    if narrowing and library_sections is None:
        raise ValueError("library scope changed but no verified library snapshot was supplied")

    touching_name = before["build"] == "per_person" and not is_default and bool(sent & {"name", "name_template"})
    template_before = (collection.name_template or collection.name) if touching_name else ""
    template_after = template_before
    default_rename_to = ""
    if "name" in sent and is_default:
        previous = str(SettingsStore(session, secrets).get("row.name_template") or "")
        proposed = body.name.strip()
        if proposed and proposed != previous:
            default_rename_to = proposed
            template_before, template_after = previous, proposed
    after_values = body.model_dump(mode="json")
    if not apply:
        projected = projected_snapshot(session, collection, body, sent)
        if touching_name:
            template_after = body.name_template or body.name
        change = row_change(
            before,
            projected,
            template_before=template_before,
            template_after=template_after,
            defer_rename=body.defer_rename,
        )

        steps, diff = _row_effects(change, sent, library_sections, current, after_values)
        return collection, steps, diff
    if theme is not None and "theme_id" in sent and theme.id != collection.theme_id:
        collection.ai_tokens = (collection.ai_tokens or 0) + unattributed_theme_tokens(
            session,
            theme,
            exclude_id=collection.id,
        )
    apply_row_patch(
        session,
        secrets,
        collection,
        body,
        sent,
        is_default=is_default,
        default_rename_to=default_rename_to,
    )
    session.flush()
    after = row_snapshot(session, collection)
    if touching_name:
        template_after = collection.name_template or collection.name
    change = row_change(
        before,
        after,
        template_before=template_before,
        template_after=template_after,
        defer_rename=body.defer_rename,
    )

    steps, diff = _row_effects(change, sent, library_sections, current, after_values)
    return collection, steps, diff


def apply_prevalidated_row_update_in_session(
    session: Session,
    secrets,
    collection_id: int,
    body,
    sent: set[str],
    *,
    library_sections: list | None = None,
) -> tuple[Collection, list[dict], dict]:
    """Apply a REST-validated patch without repeating changed-field collision checks."""
    from shortlist.server.services.season_catalogue import load_catalogue
    from shortlist.server.settings_store import SettingsStore

    collection = session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    before = row_snapshot(session, collection)
    current = serialize_row(session, collection, catalogue=load_catalogue(session))
    after_values = body.model_dump(mode="json")
    is_default = collection.slug == DEFAULT_SLUG
    touching_name = before["build"] == "per_person" and not is_default and bool(sent & {"name", "name_template"})
    template_before = (collection.name_template or collection.name) if touching_name else ""
    template_after = template_before
    default_rename_to = ""
    if "name" in sent and is_default:
        previous = str(SettingsStore(session, secrets).get("row.name_template") or "")
        proposed = body.name.strip()
        if proposed and proposed != previous:
            default_rename_to = proposed
            template_before, template_after = previous, proposed
    theme = session.get(Theme, body.theme_id) if "theme_id" in sent and body.theme_id is not None else None
    if theme is not None and theme.id != collection.theme_id:
        collection.ai_tokens = (collection.ai_tokens or 0) + unattributed_theme_tokens(
            session, theme, exclude_id=collection.id
        )
    apply_row_patch(
        session,
        secrets,
        collection,
        body,
        sent,
        is_default=is_default,
        default_rename_to=default_rename_to,
    )
    session.flush()
    after = row_snapshot(session, collection)
    if touching_name:
        template_after = collection.name_template or collection.name
    change = row_change(
        before,
        after,
        template_before=template_before,
        template_after=template_after,
        defer_rename=body.defer_rename,
    )

    steps, diff = _row_effects(change, sent, library_sections, current, after_values)
    return collection, steps, diff


def _row_effects(change, sent: set[str], library_sections: list | None, current: dict, after_values: dict):
    """The durable steps and the field diff for one row edit.

    Shared by the preview, the REST-validated apply and the full apply so the three can never plan
    different effects for one change: a step one path forgets is a visibility change that never reaches
    Plex (jobs-and-runs-design section 12).
    """

    def stranded() -> set[str]:
        return stranded_sections(
            None,
            old_media=change.media_before,
            old_keys=list(change.libraries_before),
            new_media=change.media_after,
            new_keys=list(change.libraries_after),
            sections=library_sections,
        )

    steps = steps_for_row_plan(plan_row_changes(change, stranded), slug=change.slug, build=change.build_before)
    if sent & {"schedule", "enabled"}:
        steps.append(schedule_rebuild_step())
    diff = {key: {"before": current.get(key), "after": after_values[key]} for key in sorted(sent)}
    return steps, diff


def steps_for_row_plan(plan: list[PlannedWork] | tuple[PlannedWork, ...], *, slug: str, build: str) -> list[dict]:
    """Translate the existing pure row planner into the closed effect schema."""
    steps: list[dict] = []
    for work in plan:
        if work.kind == RECONCILE:
            steps.append(
                reconcile_step(
                    slug,
                    build=build,
                    only_user_ids=work.only_user_ids,
                    in_sections=work.in_sections,
                    scope=work.scope,
                )
            )
        elif work.kind == PRIVACY_SYNC:
            steps.append(privacy_sync_step(work.scope))
        elif work.kind == RENAME:
            steps.append(
                row_rename_step(
                    slug,
                    new_template=work.new_template,
                    old_template=work.old_template,
                    scope=work.scope,
                )
            )
        elif work.kind == POSTER_RESET:
            steps.append(poster_reset_step(slug, build=build, scope=work.scope))
        elif work.kind == VISIBILITY:
            steps.append(visibility_step(slug))
        else:
            raise ValueError(f"unknown planned row effect {work.kind!r}")
    return steps


def _anchored_to(entry: object, gone: str) -> bool:
    return isinstance(entry, dict) and str(entry.get("row") or "").strip() == gone


def _forget_anchor_row(session: Session, gone: str) -> tuple[str, ...]:
    changed = tuple(
        row.slug
        for row in session.query(Collection).all()
        if any(_anchored_to(entry, gone) for entry in (row.hub_anchor or {}).values())
    )
    for row in session.query(Collection).filter(Collection.slug.in_(changed)).all():
        row.hub_anchor = {
            library: entry for library, entry in (row.hub_anchor or {}).items() if not _anchored_to(entry, gone)
        }
    return changed


def delete_row_in_session(session: Session, collection_id: int, *, template: str = "") -> DeletedRow:
    """Delete one row and its local dependants, leaving commit/effects to the caller."""
    collection = session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    result = DeletedRow(
        id=collection.id,
        slug=collection.slug,
        build=collection.build,
        template=template,
        anchors_cleared=_forget_anchor_row(session, collection.slug),
    )
    session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
    poster_service.clear_assets(session, collection.id)
    session.delete(collection)
    session.flush()
    return result


__all__ = [
    "DeletedRow",
    "apply_prevalidated_row_update_in_session",
    "create_row_in_session",
    "delete_row_in_session",
    "steps_for_row_plan",
    "update_row_in_session",
    "validate_create_row_in_session",
]
