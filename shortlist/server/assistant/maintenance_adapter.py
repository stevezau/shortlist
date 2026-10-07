"""Closed, approval-gated maintenance plans over existing owned resources."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from shortlist.engine.models import LABEL_PREFIX
from shortlist.server.assistant_auth import Capability
from shortlist.server.db.models import Collection, Server, Setting, User
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services.setup_workflow import complete_setup_in_session, setup_readiness_in_session

from .changes import AccessRequirements, ChangeError, DomainPlan, DomainResult, EffectIntent, fingerprint
from .contracts import StrictModel
from .row_effects import cache_invalidate_step, convergence_effect, reconcile_step


class MaintenanceIntent(StrictModel):
    task: Literal["cache.refresh", "row.cleanup", "uninstall", "people.sync", "setup.complete"]
    row_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_shape(self) -> MaintenanceIntent:
        needs_row = self.task == "row.cleanup"
        if needs_row != (self.row_id is not None):
            raise ValueError(f"{self.task} {'requires' if needs_row else 'does not accept'} row_id")
        return self


class MaintenanceAdapter:
    kind = "maintenance"

    def __init__(self, state) -> None:
        self.state = state

    def prepare(self, session, intent: dict) -> DomainPlan:
        body = MaintenanceIntent.model_validate(intent)
        if body.task == "uninstall":
            raise ChangeError(
                "browser_required",
                "Uninstall restores saved Plex restrictions and has no transaction-owned assistant boundary. "
                "Open Shortlist Settings → Danger zone to preview and confirm it in the owner browser.",
            )
        row_ids: tuple[int, ...] = ()
        person_ids: tuple[int, ...] = ()
        capabilities = {Capability.MAINTENANCE_EXECUTE.value}
        if body.task == "cache.refresh":
            steps = [cache_invalidate_step()]
            dependencies = {"task": fingerprint({"task": body.task})}
            evidence = {"kind": "shortlist_cache", "scope": "Plex discovery reads"}
            description = "Invalidate Shortlist's bounded Plex discovery cache."
        elif body.task == "people.sync":
            people = list(session.scalars(select(User).order_by(User.id)))
            rows = list(session.scalars(select(Collection).order_by(Collection.id)))
            dependencies = {
                "server": fingerprint(
                    {
                        column.name: repr(getattr(server, column.name))
                        for server in session.scalars(select(Server).limit(1))
                        for column in Server.__table__.columns
                    }
                ),
                "people": fingerprint(
                    [
                        {column.name: repr(getattr(person, column.name)) for column in User.__table__.columns}
                        for person in people
                    ]
                ),
                "rows": fingerprint(
                    [
                        {column.name: repr(getattr(row, column.name)) for column in Collection.__table__.columns}
                        for row in rows
                    ]
                ),
                "settings": fingerprint({setting.key: setting.value for setting in session.scalars(select(Setting))}),
            }
            capabilities.update(
                {
                    Capability.PEOPLE_WRITE.value,
                    Capability.ROWS_UPDATE.value,
                    Capability.AUDIENCES_WRITE.value,
                    Capability.RUNS_EXECUTE.value,
                }
            )
            evidence = {
                "kind": "people_sync",
                "scope": "Plex roster reconciliation may update people, privacy filters and owned row collections.",
                "dispatch": "The registered sync.users writer holds the existing Plex writer lock.",
            }
            description = "Synchronize the Plex roster through Shortlist's durable privacy-safe people-sync job."
            return DomainPlan(
                normalized_intent=body.model_dump(mode="json"),
                dependencies=dependencies,
                requirements=AccessRequirements(
                    capabilities=tuple(sorted(capabilities)),
                    dynamic_rows=True,
                    dynamic_audience=True,
                    dynamic_libraries=True,
                    batch_size=1,
                    requires_approval=True,
                ),
                effects=(EffectIntent("sync.users", {}, "maintenance-people.sync"),),
                summary={
                    "description": description,
                    "ownership_evidence": evidence,
                    "protected_maintenance": True,
                    "configuration_diff": {},
                },
            )
        elif body.task == "setup.complete":
            try:
                readiness = setup_readiness_in_session(session, self.state.secrets)
            except Exception as exc:
                raise ChangeError("invalid_selection", "Shortlist could not validate setup readiness.") from exc
            if not (readiness.plex_ownership and readiness.metadata and readiness.libraries and readiness.people):
                raise ChangeError("invalid_selection", "Complete Plex, metadata, library and people setup first.")
            dependencies = {"setup_readiness": fingerprint(readiness.fingerprint_data())}
            capabilities.add(Capability.CONFIG_WRITE.value)
            return DomainPlan(
                normalized_intent=body.model_dump(mode="json"),
                dependencies=dependencies,
                requirements=AccessRequirements(
                    capabilities=tuple(sorted(capabilities)), batch_size=1, requires_approval=True
                ),
                summary={
                    "description": "Mark the validated Shortlist setup workflow complete.",
                    "setup_readiness": readiness.fingerprint_data(),
                    "protected_maintenance": True,
                    "configuration_diff": {"setup.completed": True},
                },
            )
        else:
            row = session.get(Collection, body.row_id)
            if row is None:
                raise ValueError("row not found")
            template = reconcile.row_template(session, row.slug, getattr(self.state, "secrets", None))
            steps = [reconcile_step(row.slug, build=row.build, scope="collection.cleanup", template=template)]
            row_ids = (row.id,)
            people = list(session.scalars(select(User).order_by(User.id)))
            person_ids = tuple(person.id for person in people)
            capabilities.add(Capability.RUNS_EXECUTE.value)
            dependencies = {
                "row": fingerprint(
                    {column.name: repr(getattr(row, column.name)) for column in Collection.__table__.columns}
                ),
                "people": fingerprint([[person.id, person.slug] for person in people]),
                "settings": fingerprint({setting.key: setting.value for setting in session.scalars(select(Setting))}),
            }
            evidence = {
                "kind": "shortlist_ownership_rules",
                "row_id": row.id,
                "row_slug": row.slug,
                "labels": [f"{LABEL_PREFIX}_shared_{row.slug}"]
                if row.build == "shared"
                else [f"{LABEL_PREFIX}_{person.slug}" for person in people],
                "build": row.build,
                "title_template": template,
                "scope": "All known people and every library, including historical deliveries for this row.",
                "limitation": "The actual remote collection set is resolved by Shortlist ownership rules at dispatch.",
            }
            description = (
                "Remove this owned row's delivered collections while keeping its configuration. "
                "Enabled automation may recreate them."
            )
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json"),
            dependencies=dependencies,
            requirements=AccessRequirements(
                capabilities=tuple(sorted(capabilities)),
                row_ids=row_ids,
                person_ids=person_ids,
                dynamic_audience=body.task == "row.cleanup",
                dynamic_libraries=body.task == "row.cleanup",
                batch_size=1,
                requires_approval=True,
            ),
            effects=(convergence_effect(steps, domain="rows", effect_key=f"maintenance-{body.task}"),),
            summary={
                "description": description,
                "ownership_evidence": evidence,
                "protected_maintenance": True,
                "configuration_diff": {},
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = MaintenanceIntent.model_validate(intent)
        if body.task == "uninstall":
            raise ChangeError("browser_required", "Uninstall must be completed in the owner browser.")
        if body.task == "setup.complete":
            try:
                complete_setup_in_session(session, self.state.secrets)
            except ChangeError:
                raise
            except Exception as exc:
                raise ChangeError("invalid_selection", "Shortlist could not complete setup validation.") from exc
            return DomainResult(
                result={"task": body.task, "completed": True},
                audit_diff={"maintenance": {"task": body.task, "setup_completed": True}},
            )
        return DomainResult(
            result={"task": body.task, "row_id": body.row_id, "maintenance_pending": True},
            audit_diff={"maintenance": body.model_dump(mode="json")},
        )


__all__ = ["MaintenanceAdapter", "MaintenanceIntent"]
