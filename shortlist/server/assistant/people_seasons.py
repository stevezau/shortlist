"""Bounded person and calendar plans using the same mutations as the owner UI."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import select

from shortlist.server.api.seasons import CollectionIO, DateRuleIO, PickIO, SeasonIn, TagIO
from shortlist.server.api.users import BlockSeedBody, UserPatch, UserPrefs
from shortlist.server.db.models import Collection, CollectionAudience, CollectionUserOverride, SeasonDef, Setting, User
from shortlist.server.services import context_builder
from shortlist.server.services.person_changes import apply_person_in_session, prepare_person_in_session
from shortlist.server.services.person_row_overrides import (
    RowOverridePatch,
    apply_person_row_override_in_session,
    prepare_person_row_override_in_session,
)
from shortlist.server.services.season_changes import apply_season_in_session, prepare_season_in_session

from .changes import AccessRequirements, DomainPlan, DomainResult, fingerprint
from .contracts import StrictModel


class PersonBlockedSeed(BlockSeedBody):
    model_config = ConfigDict(extra="forbid", strict=True)


class PersonPrefs(UserPrefs):
    model_config = ConfigDict(extra="forbid", strict=True)
    excluded_genres: list[str] | None = Field(default=None, max_length=100)
    blocked_seeds: list[int | PersonBlockedSeed] | None = Field(default=None, max_length=200)


class PersonPatch(UserPatch):
    model_config = ConfigDict(extra="forbid", strict=True)
    prefs: PersonPrefs | None = None


class PersonRowOverride(RowOverridePatch, StrictModel):
    """One person's sparse settings for one applicable per-person row."""

    row_id: int = Field(gt=0)

    @model_validator(mode="after")
    def values_are_present(self):
        if not self.model_fields_set - {"row_id"}:
            raise ValueError("row_overrides entries require muted, row_size or recent_count")
        return self


class PeopleIntent(StrictModel):
    person_id: int = Field(gt=0)
    patch: PersonPatch
    row_overrides: list[PersonRowOverride] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def override_rows_are_distinct(self):
        row_ids = [override.row_id for override in self.row_overrides]
        if len(set(row_ids)) != len(row_ids):
            raise ValueError("row_overrides cannot repeat a row_id")
        return self


class SeasonDateRule(DateRuleIO):
    model_config = ConfigDict(extra="forbid", strict=True)


class SeasonTag(TagIO):
    model_config = ConfigDict(extra="forbid", strict=True)


class SeasonCollection(CollectionIO):
    model_config = ConfigDict(extra="forbid", strict=True)


class SeasonPick(PickIO):
    model_config = ConfigDict(extra="forbid", strict=True)


class SeasonDefinition(SeasonIn):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    rule: SeasonDateRule
    tags: list[SeasonTag] = Field(default_factory=list, max_length=20)
    collections: list[SeasonCollection] = Field(default_factory=list, max_length=10)
    picks: list[SeasonPick] = Field(default_factory=list, max_length=200)


class SeasonsIntent(StrictModel):
    action: Literal["create", "update", "delete"]
    slug: str | None = Field(default=None, min_length=1, max_length=64)
    definition: SeasonDefinition | None = None

    @model_validator(mode="after")
    def consistent_action(self):
        if (self.action == "create") != (self.slug is None):
            raise ValueError("Create omits slug; update and delete require it.")
        if (self.action == "delete") != (self.definition is None):
            raise ValueError("Create and update require a definition; delete omits it.")
        return self


def _snapshot(session, model) -> str:
    # Hash complete authoritative state, including private fields, without returning it to the assistant.
    records = []
    for row in session.scalars(select(model).order_by(*model.__table__.primary_key.columns)):
        record = {}
        for column in model.__table__.columns:
            value = getattr(row, column.name)
            record[column.name] = value.isoformat() if isinstance(value, (date, datetime)) else value
        records.append(record)
    return fingerprint(records)


def _dependencies(session) -> dict[str, str]:
    return {
        model.__tablename__: _snapshot(session, model)
        for model in (
            Collection,
            CollectionAudience,
            CollectionUserOverride,
            SeasonDef,
            Setting,
            User,
        )
    }


class PeopleAdapter:
    kind = "people"

    def prepare(self, session, intent: dict) -> DomainPlan:
        from .row_effects import convergence_effect

        body = PeopleIntent.model_validate(intent)
        person_mutation = prepare_person_in_session(session, body.person_id, body.patch)
        override_mutations = [
            prepare_person_row_override_in_session(
                session,
                body.person_id,
                override.row_id,
                RowOverridePatch.model_validate(
                    override.model_dump(mode="json", exclude={"row_id"}, exclude_unset=True)
                ),
            )
            for override in body.row_overrides
        ]
        # Person-wide changes can affect any row they receive. Share-management changes also change
        # whether this person sees other people's rows, so every extant row belongs in the contract.
        rows = (
            list(session.scalars(select(Collection).order_by(Collection.id)))
            if person_mutation.changed
            else [session.get(Collection, mutation.collection_id) for mutation in override_mutations]
        )
        capabilities = {"people.write"}
        visibility = bool(set(person_mutation.changed) & {"enabled", "manage_sharing"}) or "paused" in (
            body.patch.prefs.model_fields_set if body.patch.prefs else set()
        )
        if rows:
            capabilities.add("rows.update")
        if visibility:
            capabilities.add("audiences.write")
        # Never echo existing blocked seed titles or inferred taste through a preference diff.
        diff = {
            key: value if key != "prefs" else {"changed_fields": sorted(body.patch.prefs.model_fields_set)}
            for key, value in person_mutation.changed.items()
        }
        if override_mutations:
            diff["row_overrides"] = [
                {"row_id": mutation.collection_id, "changed_fields": sorted(mutation.changed)}
                for mutation in override_mutations
            ]
        steps = tuple(step for mutation in (person_mutation, *override_mutations) for step in mutation.steps)
        if any(step["kind"] == "row.reconcile" for step in steps):
            capabilities.add("runs.execute")
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json", exclude_unset=True),
            dependencies=_dependencies(session),
            requirements=AccessRequirements(
                capabilities=tuple(sorted(capabilities)),
                row_ids=tuple(row.id for row in rows),
                person_ids=(body.person_id,),
                library_keys=tuple(sorted({str(key) for row in rows for key in row.library_keys})),
                dynamic_libraries=any(not row.library_keys for row in rows),
                batch_size=max(1, len(override_mutations)),
            ),
            effects=(convergence_effect(steps, domain="people"),) if steps else (),
            summary={
                "description": "Update this person's preferences and persist required Plex follow-up work.",
                "person_id": body.person_id,
                "configuration_diff": diff,
                "affected_row_ids": [row.id for row in rows],
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = PeopleIntent.model_validate(intent)
        person_mutation = prepare_person_in_session(session, body.person_id, body.patch)
        override_mutations = [
            prepare_person_row_override_in_session(
                session,
                body.person_id,
                override.row_id,
                RowOverridePatch.model_validate(
                    override.model_dump(mode="json", exclude={"row_id"}, exclude_unset=True)
                ),
            )
            for override in body.row_overrides
        ]
        apply_person_in_session(session, person_mutation)
        for mutation in override_mutations:
            apply_person_row_override_in_session(session, mutation)
        changed_fields = sorted(person_mutation.changed)
        if override_mutations:
            changed_fields.append("row_overrides")
        return DomainResult(
            result={"person_id": body.person_id, "changed_fields": changed_fields},
            audit_diff={
                "person_id": body.person_id,
                "changed_fields": changed_fields,
                "row_overrides": [
                    {"row_id": mutation.collection_id, "changed_fields": sorted(mutation.changed)}
                    for mutation in override_mutations
                ],
            },
        )


class SeasonsAdapter:
    kind = "seasons"

    def __init__(self, state) -> None:
        self.state = state

    def prepare(self, session, intent: dict) -> DomainPlan:
        from .row_effects import convergence_effect

        body = SeasonsIntent.model_validate(intent)
        mutation = prepare_season_in_session(session, body.action, slug=body.slug, body=body.definition)
        rows = [session.get(Collection, row_id) for row_id in mutation.following_ids]
        people = set()
        for row in rows:
            if row.audience == "everyone":
                people.update(session.scalars(select(User.id).where(User.removed_at.is_(None))))
            else:
                people.update(
                    session.scalars(
                        select(CollectionAudience.user_id).where(CollectionAudience.collection_id == row.id)
                    )
                )
        libraries = {str(key) for row in rows for key in row.library_keys}
        if body.definition:
            libraries.update(c.section_key for c in body.definition.collections)
        dependencies = _dependencies(session)
        dependencies["calendar_day"] = fingerprint(context_builder.local_now().date().isoformat())
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json", exclude_unset=True),
            dependencies=dependencies,
            requirements=AccessRequirements(
                capabilities=("seasons.write", "rows.update") if rows else ("seasons.write",),
                row_ids=mutation.following_ids,
                person_ids=tuple(sorted(people)),
                library_keys=tuple(sorted(libraries)),
                batch_size=1,
            ),
            effects=(convergence_effect(mutation.steps, domain="seasons"),) if mutation.steps else (),
            summary={
                "description": f"{body.action.title()} a custom season and persist required visibility work.",
                "slug": mutation.slug,
                "affected_row_ids": list(mutation.following_ids),
                "visibility_passes": len(mutation.steps),
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = SeasonsIntent.model_validate(intent)
        mutation = prepare_season_in_session(session, body.action, slug=body.slug, body=body.definition)
        apply_season_in_session(session, self.state, mutation)
        return DomainResult(
            result={"slug": mutation.slug, "action": body.action},
            audit_diff={"slug": mutation.slug, "action": body.action, "affected_row_ids": list(mutation.following_ids)},
        )
