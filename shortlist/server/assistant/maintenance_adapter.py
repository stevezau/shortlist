"""Closed, approval-gated maintenance plans over existing owned resources."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from shortlist.engine.models import LABEL_PREFIX
from shortlist.server.assistant_auth import Capability
from shortlist.server.db.models import Collection, Setting, User
from shortlist.server.services import collection_reconcile as reconcile

from .changes import AccessRequirements, ChangeError, DomainPlan, DomainResult, fingerprint
from .contracts import StrictModel
from .row_effects import cache_invalidate_step, convergence_effect, reconcile_step


class MaintenanceIntent(StrictModel):
    task: Literal["cache.refresh", "row.cleanup", "uninstall"]
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
        return DomainResult(
            result={"task": body.task, "row_id": body.row_id, "maintenance_pending": True},
            audit_diff={"maintenance": body.model_dump(mode="json")},
        )


__all__ = ["MaintenanceAdapter", "MaintenanceIntent"]
