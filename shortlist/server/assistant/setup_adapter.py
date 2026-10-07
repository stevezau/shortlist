"""Atomic creation of an externally authored theme and its inactive row."""

from __future__ import annotations

from pydantic import Field, model_validator

from shortlist.server.db.models import Theme
from shortlist.server.services import theme_store

from .changes import AccessRequirements, DomainPlan, DomainResult
from .contracts import StrictModel
from .row_adapter import RowAdapter, RowIntent
from .theme_adapter import AssistantTheme, ThemeAdapter, ThemeIntent, _resolved_body


class ThemeRowIntent(StrictModel):
    theme: AssistantTheme = Field(description="A resolved editorial theme. Saving it does not invoke a provider.")
    row: RowIntent = Field(
        description="A new row using a catalog template; activation and scheduling are separate reviewed changes."
    )

    @model_validator(mode="after")
    def safe_bundle(self):
        if self.row.action != "create":
            raise ValueError("Setup creates one new theme and one new row.")
        if self.row.values.get("theme_id") is not None:
            raise ValueError("The row uses the new theme from this bundle, not an existing theme ID.")
        if self.row.values.get("enabled") not in (None, False) or self.row.values.get("schedule") not in (None, ""):
            raise ValueError("Create setup drafts disabled and unscheduled; activate after review and preview.")
        return self


class SetupAdapter:
    kind = "setup"

    def __init__(self, state) -> None:
        self.state = state
        self.themes = ThemeAdapter(state.secrets)
        self.rows = RowAdapter(state)

    def prepare(self, session, intent: dict) -> DomainPlan:
        body = ThemeRowIntent.model_validate(intent)
        theme_intent = ThemeIntent(action="create", draft=body.theme)
        theme_plan = self.themes.prepare(session, theme_intent.model_dump(mode="json"))
        projection = Theme(slug="assistant-setup-projection")
        theme_store.write_theme(projection, _resolved_body(session, theme_intent))
        row_intent = body.row.model_copy(
            update={"values": {**body.row.values, "theme_id": None, "enabled": False, "schedule": ""}}
        )
        row_plan = self.rows.prepare(session, row_intent.model_dump(mode="json"), trusted_theme=projection)
        first, second = theme_plan.requirements, row_plan.requirements

        def combined(name):
            return tuple(sorted(set(getattr(first, name)) | set(getattr(second, name))))

        requirements = AccessRequirements(
            capabilities=combined("capabilities"),
            row_ids=combined("row_ids"),
            person_ids=combined("person_ids"),
            library_keys=combined("library_keys"),
            setting_groups=combined("setting_groups"),
            destination_ids=combined("destination_ids"),
            dynamic_rows=first.dynamic_rows or second.dynamic_rows,
            dynamic_audience=first.dynamic_audience or second.dynamic_audience,
            dynamic_libraries=first.dynamic_libraries or second.dynamic_libraries,
            requires_approval=first.requires_approval or second.requires_approval,
            batch_size=first.batch_size + second.batch_size,
            work_units=(first.work_units or 0) + (second.work_units or 0),
            provider_calls=(first.provider_calls or 0) + (second.provider_calls or 0),
        )
        return DomainPlan(
            normalized_intent={"theme": theme_plan.normalized_intent["draft"], "row": row_plan.normalized_intent},
            dependencies={
                **{f"theme:{key}": value for key, value in theme_plan.dependencies.items()},
                **{f"row:{key}": value for key, value in row_plan.dependencies.items()},
            },
            requirements=requirements,
            effects=theme_plan.effects + row_plan.effects,
            summary={
                "description": "Create the saved theme and its disabled, unscheduled row together.",
                "theme": theme_plan.summary,
                "row": row_plan.summary,
                "theme_reference": "The new row follows the theme created by this same operation.",
                "provider_generation": False,
                "plex_delivery": False,
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = ThemeRowIntent.model_validate(intent)
        theme_result = self.themes.apply(
            session, ThemeIntent(action="create", draft=body.theme).model_dump(mode="json")
        )
        theme_id = theme_result.result["theme_id"]
        row_intent = body.row.model_copy(
            update={"values": {**body.row.values, "theme_id": theme_id, "enabled": False, "schedule": ""}}
        )
        row_result = self.rows.apply(session, row_intent.model_dump(mode="json"))
        return DomainResult(
            result={**row_result.result, "theme_id": theme_id, "scheduled": False},
            audit_diff={"theme": theme_result.audit_diff, "row": row_result.audit_diff},
            references={"theme": theme_id, "row": row_result.result["row_id"]},
            created_row_ids=row_result.created_row_ids,
        )
