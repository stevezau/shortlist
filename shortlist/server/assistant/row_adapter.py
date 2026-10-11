"""Strict, catalog-backed row plans over the shared transaction service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

from loguru import logger
from pydantic import Field, JsonValue, model_validator
from sqlalchemy import select

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.server.assistant_auth import Capability
from shortlist.server.catalogs.templates import ROW_INPUT_DEFAULTS, get_template_definition
from shortlist.server.db.models import Collection, CollectionAudience, Setting, Theme, User
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services.row_mutations import (
    create_row_in_session,
    delete_row_in_session,
    set_ai_paused_in_session,
    update_row_in_session,
    validate_create_row_in_session,
)
from shortlist.server.settings_store import SettingsStore

from .changes import AccessRequirements, DomainPlan, DomainResult, fingerprint
from .contracts import StrictModel
from .row_effects import (
    convergence_effect,
    privacy_sync_step,
    reconcile_step,
    schedule_rebuild_step,
)


class RowIntent(StrictModel):
    """One explicit row creation, patch, or deletion."""

    action: Literal["create", "update", "delete"]
    row_id: int | None = Field(default=None, ge=1)
    template_id: str | None = Field(default=None, min_length=1, max_length=80)
    values: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def validate_shape(self) -> RowIntent:
        if self.action == "create":
            if self.row_id is not None or self.template_id is None:
                raise ValueError("create requires template_id and does not accept row_id")
            try:
                get_template_definition(self.template_id)
            except KeyError:
                raise ValueError(f"unknown row template {self.template_id!r}") from None
            unknown = set(self.values) - (set(ROW_INPUT_DEFAULTS) | {"ai_paused"})
        elif self.action == "update":
            if self.row_id is None or self.template_id is not None or not self.values:
                raise ValueError("update requires row_id and at least one value")
            from shortlist.server.services.row_editing import CollectionIn

            unknown = set(self.values) - (
                (set(CollectionIn.model_fields) | {"ai_paused"}) - {"dry_run", "defer_rename"}
            )
        else:
            if self.row_id is None or self.template_id is not None or self.values:
                raise ValueError("delete accepts only row_id")
            unknown = set()
        if "ai_paused" in self.values and type(self.values["ai_paused"]) is not bool:
            raise ValueError("ai_paused must be a boolean")
        if unknown:
            raise ValueError(f"unknown row fields: {sorted(unknown)}")
        return self


def _json_snapshot(session) -> dict:
    """Hash all local facts that row validation and scope resolution read."""
    rows = [
        {column.name: repr(getattr(row, column.name)) for column in Collection.__table__.columns}
        for row in session.scalars(select(Collection).order_by(Collection.id))
    ]
    people = [
        {"id": user.id, "slug": user.slug, "name": user.display_name, "removed": repr(user.removed_at)}
        for user in session.scalars(select(User).order_by(User.id))
    ]
    themes = [
        {column.name: repr(getattr(theme, column.name)) for column in Theme.__table__.columns}
        for theme in session.scalars(select(Theme).order_by(Theme.id))
    ]
    settings = {row.key: row.value for row in session.scalars(select(Setting).order_by(Setting.key))}
    return {
        "rows": fingerprint(rows),
        "people": fingerprint(people),
        "themes": fingerprint(themes),
        "row_settings": fingerprint(settings),
    }


def _audience_ids(session, row: Collection) -> tuple[int, ...]:
    if row.audience == "everyone":
        return tuple(session.scalars(select(User.id).where(User.removed_at.is_(None)).order_by(User.id)))
    return tuple(
        session.scalars(
            select(CollectionAudience.user_id)
            .where(CollectionAudience.collection_id == row.id)
            .order_by(CollectionAudience.user_id)
        )
    )


class RowAdapter:
    kind = "row"

    def __init__(self, state) -> None:
        self.state = state

    @property
    def secrets(self):
        return getattr(self.state, "secrets", None)

    def _library_sections(self, session, row_id: int, values: dict, *, reuse: bool = False) -> list | None:
        row = session.get(Collection, row_id)
        if row is None:
            return None
        try:
            requested_keys = tuple(str(key) for key in values.get("library_keys", row.library_keys or []))
        except TypeError:
            # The shared mutation service supplies the normal field validation error.
            return None
        current_scope = (row.media, tuple(str(key) for key in (row.library_keys or [])))
        if current_scope == (values.get("media", row.media), requested_keys):
            return None

        snapshot_key = fingerprint({"row_id": row_id, "values": values, "before_scope": current_scope})
        if reuse:
            cached = session.info.pop("assistant_row_library_sections", None)
            if cached is not None and cached[0] == snapshot_key:
                return cached[1]
        snapshot = getattr(self.state, "assistant_library_snapshot", None)
        if snapshot is None:
            snapshot = getattr(self.state, "assistant_library_sections", None)
        if snapshot is None:
            store = SettingsStore(session, self.secrets)
            url, token = store.get("plex.url"), store.get("plex.token")
            if not url or not token:
                raise ValueError("Connect Plex in the owner browser before changing this row's libraries.")
            try:
                client = PlexClient(url, token, timeout=8, follow_redirects=False)
                try:
                    snapshot = [
                        SimpleNamespace(key=str(section.key), type=str(section.type)) for section in client.sections()
                    ]
                finally:
                    transport = getattr(getattr(client, "_server", None), "_session", None)
                    if transport is not None:
                        transport.close()
            except Exception as error:
                logger.warning("assistant row library verification failed ({})", type(error).__name__)
                raise ValueError(
                    "Could not verify current Plex libraries. "
                    "Check the owner's Plex connection and prepare a new change."
                ) from None
        sections = [SimpleNamespace(**item) if isinstance(item, dict) else item for item in snapshot]
        if not reuse:
            # Apply reprojects in the same transaction. Its mutation must use exactly
            # that snapshot, not another network read with a different cleanup scope.
            session.info["assistant_row_library_sections"] = (snapshot_key, sections)
        return sections

    @staticmethod
    def _capabilities(action: str, values: dict, *, build: str, steps: list[dict]) -> set[str]:
        capabilities = {
            {
                "create": Capability.ROWS_CREATE,
                "update": Capability.ROWS_UPDATE,
                "delete": Capability.ROWS_DELETE,
            }[action].value
        }
        kinds = {step["kind"] for step in steps}
        if kinds & {"row.reconcile", "row.rename", "poster.reset", "rows.visibility"}:
            capabilities.add(Capability.RUNS_EXECUTE.value)
        if build == "shared" or "privacy.sync" in kinds or set(values) & {"audience", "audience_user_ids", "build"}:
            capabilities.add(Capability.AUDIENCES_WRITE.value)
        if set(values) & {"schedule", "enabled", "show_days", "seasons", "season_lead_days", "season_after_days"}:
            capabilities.add(Capability.SCHEDULES_WRITE.value)
        if values.get("enabled") is True:
            capabilities.update({Capability.ROWS_ACTIVATE.value, Capability.RUNS_EXECUTE.value})
        if values.get("theme_id") is not None:
            capabilities.add(Capability.THEMES_WRITE.value)
        request_configuration = bool(values.get("requests_row") or values.get("request_tag")) or any(
            value is not None for key, value in values.items() if key.startswith("req_")
        )
        if request_configuration:
            capabilities.add(Capability.REQUESTS_MANAGE.value)
        if values.get("req_auto_send") is True:
            capabilities.add(Capability.REQUESTS_SEND.value)
        return capabilities

    def prepare(self, session, intent: dict, *, trusted_theme: Theme | None = None) -> DomainPlan:
        body = RowIntent.model_validate(intent)
        if trusted_theme is not None and body.action != "create":
            raise ValueError("A transient theme can be projected only for a new row.")
        dependencies = _json_snapshot(session)
        library_sections = None
        if body.action == "create":
            template = get_template_definition(body.template_id or "")
            values = dict(template.effective_values)
            values["enabled"] = body.values.get("enabled", False)
            values.update(body.values)
            from shortlist.server.services.row_editing import CollectionIn

            create_body = CollectionIn.model_validate(
                {key: value for key, value in values.items() if key != "ai_paused"}
            )
            if "ai_paused" in values and create_body.theme_id is None and trusted_theme is None:
                raise ValueError("Only an AI row has AI to pause.")
            final_values = {**create_body.model_dump(mode="json"), "ai_paused": values.get("ai_paused", False)}
            if trusted_theme is not None:
                final_values["theme_id"] = "new-theme"
            validate_create_row_in_session(session, self.secrets, create_body, trusted_theme=trusted_theme)
            row_id = 0
            build = create_body.build
            audience_kind = create_body.audience
            active = bool(create_body.enabled)
            candidate_sources = tuple(create_body.candidate_sources or ())
            audience_ids = (
                tuple(session.scalars(select(User.id).where(User.removed_at.is_(None)).order_by(User.id)))
                if create_body.audience == "everyone"
                else tuple(sorted(set(create_body.audience_user_ids)))
            )
            library_keys = tuple(str(key) for key in create_body.library_keys)
            steps = [schedule_rebuild_step()]
            capabilities = self._capabilities("create", values, build=build, steps=steps)
            effective_sources = candidate_sources or tuple(
                SettingsStore(session, self.secrets).get("candidates.sources") or ()
            )
            poster = final_values.get("poster") or {}
            skip_static_theme_generation = (
                template.id == "describe-a-row"
                and trusted_theme is not None
                and final_values.get("ai_paused") is True
                and final_values.get("theme_mode") == "fixed"
                and "llm_web" not in effective_sources
                and poster.get("mode") not in {"ai", "generate"}
            )
            capabilities.update(
                capability.value
                for capability in template.required_capabilities
                if not (skip_static_theme_generation and capability is Capability.AI_GENERATE)
            )
            normalized = RowIntent(action="create", template_id=template.id, values=values).model_dump(mode="json")
            row_ids: tuple[int, ...] = ()
            dynamic_rows = False
            description = (
                f"Create a disabled row from {template.title}."
                if not values["enabled"]
                else f"Create and activate a row from {template.title}."
            )
            diff = {key: {"before": None, "after": value} for key, value in values.items()}
        elif body.action == "update":
            mutation_values = {key: value for key, value in body.values.items() if key != "ai_paused"}
            if mutation_values:
                library_sections = self._library_sections(session, body.row_id or 0, mutation_values)
                if library_sections is not None:
                    dependencies["library_sections"] = fingerprint(
                        sorted((str(section.key), str(section.type)) for section in library_sections)
                    )
                row, steps, diff = update_row_in_session(
                    session,
                    self.secrets,
                    body.row_id or 0,
                    mutation_values,
                    library_sections=library_sections,
                    apply=False,
                )
            else:
                row = session.get(Collection, body.row_id)
                if row is None:
                    raise ValueError("row not found")
                steps, diff = [], {}
            final_values = {column.name: getattr(row, column.name) for column in Collection.__table__.columns}
            final_values.update(body.values)
            if "ai_paused" in body.values:
                if final_values.get("theme_id") is None:
                    raise ValueError("Only an AI row has AI to pause.")
                diff["ai_paused"] = {"before": bool(row.ai_paused), "after": body.values["ai_paused"]}
            row_id = row.id
            build = body.values.get("build", row.build)
            audience_kind = body.values.get("audience", row.audience)
            active = bool(body.values.get("enabled", row.enabled))
            candidate_sources = tuple(body.values.get("candidate_sources", row.candidate_sources or ()))
            if audience_kind == "everyone":
                audience_ids = tuple(
                    session.scalars(select(User.id).where(User.removed_at.is_(None)).order_by(User.id))
                )
            elif {"audience", "audience_user_ids"} & set(body.values):
                audience_ids = tuple(sorted(set(body.values.get("audience_user_ids", ()))))
            else:
                audience_ids = _audience_ids(session, row)
            library_keys = tuple(str(key) for key in body.values.get("library_keys", row.library_keys or ()))
            capabilities = self._capabilities("update", body.values, build=build, steps=steps)
            normalized = body.model_dump(mode="json")
            row_ids = (row_id,)
            dynamic_rows = False
            description = f"Update row {row_id}."
        else:
            row = session.get(Collection, body.row_id)
            if row is None:
                raise ValueError("row not found")
            row_id, build, audience_kind = row.id, row.build, row.audience
            audience_ids = _audience_ids(session, row)
            library_keys = tuple(str(key) for key in (row.library_keys or []))
            template = reconcile.row_template(session, row.slug, self.secrets)
            steps = [reconcile_step(row.slug, build=row.build, scope="collection.delete", template=template)]
            if row.build == "shared":
                steps.append(privacy_sync_step(f"row '{row.slug}' was deleted"))
            steps.append(schedule_rebuild_step())
            capabilities = self._capabilities("delete", {}, build=build, steps=steps)
            normalized = body.model_dump(mode="json")
            row_ids = (row_id,)
            dynamic_rows = False
            description = f"Delete row {row_id} and remove its delivered collections."
            diff = {"deleted": {"id": row.id, "slug": row.slug, "name": row.name}}

        future_libraries = not library_keys
        dynamic_libraries = future_libraries
        if body.action == "update" and library_sections is not None:
            # Narrowing can remove delivered collections in the old scope. The
            # authority footprint covers both sides, including an old all-library scope.
            previous_library_keys = tuple(str(key) for key in (row.library_keys or []))
            library_keys = tuple(sorted(set(previous_library_keys) | set(library_keys)))
            dynamic_libraries = dynamic_libraries or not previous_library_keys

        destination_ids: tuple[str, ...] = ()
        # These fields change selection, cadence, or the provider/acquisition inputs.
        # Cosmetic ordering/placement alone does not expand standing paid automation.
        recurrence_fields = {
            "enabled",
            "schedule",
            "build",
            "audience",
            "audience_user_ids",
            "library_keys",
            "media",
            "size",
            "candidate_sources",
            "watched_pct",
            "rewatch",
            "rewatch_cooldown_days",
            "unstarted_only",
            "refresh_days",
            "idle_hold_days",
            "recency",
            "recent_count",
            "favourite_count",
            "older_count",
            "max_seeds",
            "cold_start",
            "seed_window",
            "max_runtime",
            "min_year",
            "max_year",
            "min_rating",
            "min_watchers",
            "seasons",
            "season_lead_days",
            "season_after_days",
            "show_days",
            "theme_id",
            "theme_mode",
            "explore_brief",
            "theme_days",
            "refresh_share",
            "repeat_cooldown_days",
            "avoid_rows",
            "ai_instructions",
            "ai_paused",
            "requests_row",
            "requests_window_days",
            "requests_tag_pattern",
            "request_tag",
            "pick_order",
        }
        changed = set(body.values)
        affects_recurrence = (
            body.action == "create"
            or bool(recurrence_fields & changed)
            or any(key.startswith("req_") for key in changed)
        )
        if body.action != "delete" and active:
            store = SettingsStore(session, self.secrets)
            sources = candidate_sources or tuple(store.get("candidates.sources") or ())
            themed_generation = final_values.get("theme_id") is not None and not final_values.get("ai_paused", False)
            poster = final_values.get("poster") or {}
            ai_poster = poster.get("mode") in {"ai", "generate"}
            affects_poster = affects_recurrence or bool(changed & {"poster", "name", "name_template", "fallback_name"})
            paid_generation = (affects_recurrence and ("llm_web" in sources or themed_generation)) or (
                ai_poster and affects_poster
            )
            destinations = set()
            if paid_generation:
                capabilities.add(Capability.AI_GENERATE.value)
                from .generation import provider_destination

                if store.get("curator.provider") not in (None, "", "none"):
                    destinations.add(provider_destination(store))
                if "llm_web" in sources or final_values.get("theme_mode") == "explore" or ai_poster:
                    capabilities.update({Capability.HISTORY_USE.value, Capability.HISTORY_PROVIDERS.value})
                if "llm_web" in sources:
                    search = store.get("llm_web.search_provider")
                    if search == "exa":
                        destinations.add("https://api.exa.ai")
                    elif search == "searxng" and store.get("searxng.url"):
                        destinations.add(str(store.get("searxng.url")).rstrip("/"))
            request_auto_send = final_values.get("req_auto_send")
            auto_send = bool(store.get("requests.auto_send")) if request_auto_send is None else request_auto_send
            if affects_recurrence and bool(store.get("requests.enabled")) and auto_send:
                capabilities.add(Capability.REQUESTS_SEND.value)
                target = store.get("requests.target") or "arr"
                media = final_values.get("media", "both")
                services = (
                    ("overseerr",)
                    if target == "overseerr"
                    else tuple(
                        service
                        for service, kind in (("radarr", "movie"), ("sonarr", "show"))
                        if media in (kind, "both")
                    )
                )
                destinations.update(
                    str(store.get(f"requests.{service}.url")).rstrip("/")
                    for service in services
                    if store.get(f"requests.{service}.url")
                )
            destination_ids = tuple(sorted(destinations))

        effects = (convergence_effect(steps, domain="rows", effect_key=f"row-{body.action}-{row_id}"),) if steps else ()
        return DomainPlan(
            normalized_intent=normalized,
            dependencies=dependencies,
            requirements=AccessRequirements(
                capabilities=tuple(sorted(capabilities)),
                row_ids=row_ids,
                person_ids=audience_ids,
                library_keys=library_keys,
                destination_ids=destination_ids,
                dynamic_rows=dynamic_rows,
                dynamic_audience=audience_kind == "everyone",
                dynamic_libraries=dynamic_libraries,
                batch_size=1,
                work_units=max(1, len(audience_ids)) * max(1, len(library_keys)),
                requires_approval=bool({Capability.AI_GENERATE.value, Capability.REQUESTS_SEND.value} & capabilities),
            ),
            effects=effects,
            summary={
                "description": description,
                "configuration_diff": diff,
                "future_scope": {
                    "includes_future_people": audience_kind == "everyone",
                    "includes_future_libraries": future_libraries,
                    "recurring_schedule": normalized.get("values", {}).get("schedule"),
                },
                "future_effects": {
                    "plex_delivery": body.action != "delete" and active,
                    "provider_generation": Capability.AI_GENERATE.value in capabilities,
                    "request_sends": Capability.REQUESTS_SEND.value in capabilities,
                    "destinations": list(destination_ids),
                    "budget_notice": (
                        "Recurring provider and acquisition work is not capped by the one-operation generation quota; "
                        "this recurring automation change needs exact browser approval."
                        if {Capability.AI_GENERATE.value, Capability.REQUESTS_SEND.value} & capabilities
                        else None
                    ),
                },
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = RowIntent.model_validate(intent)
        if body.action == "create":
            from shortlist.server.services.row_editing import CollectionIn

            row = create_row_in_session(
                session,
                self.secrets,
                CollectionIn.model_validate({key: value for key, value in body.values.items() if key != "ai_paused"}),
            )
            if "ai_paused" in body.values:
                set_ai_paused_in_session(session, row, body.values["ai_paused"])
            return DomainResult(
                result={"row_id": row.id, "slug": row.slug, "enabled": bool(row.enabled)},
                audit_diff={"created": {"id": row.id, "slug": row.slug, "values": body.values}},
                created_row_ids=(row.id,),
            )
        if body.action == "update":
            mutation_values = {key: value for key, value in body.values.items() if key != "ai_paused"}
            if mutation_values:
                row, _steps, diff = update_row_in_session(
                    session,
                    self.secrets,
                    body.row_id or 0,
                    mutation_values,
                    library_sections=self._library_sections(session, body.row_id or 0, mutation_values, reuse=True),
                )
            else:
                row = session.get(Collection, body.row_id)
                if row is None:
                    raise ValueError("row not found")
                diff = {}
            if "ai_paused" in body.values:
                diff["ai_paused"] = {"before": bool(row.ai_paused), "after": body.values["ai_paused"]}
                set_ai_paused_in_session(session, row, body.values["ai_paused"])
            return DomainResult(
                result={"row_id": row.id, "slug": row.slug, "enabled": bool(row.enabled)},
                audit_diff=diff,
            )
        row = session.get(Collection, body.row_id)
        if row is None:
            raise ValueError("row not found")
        template = reconcile.row_template(session, row.slug, self.secrets)
        deleted = delete_row_in_session(session, row.id, template=template)
        return DomainResult(
            result={"row_id": deleted.id, "slug": deleted.slug, "deleted": True},
            audit_diff={"deleted": {"id": deleted.id, "slug": deleted.slug}},
        )


__all__ = ["RowAdapter", "RowIntent"]
