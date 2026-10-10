"""Bounded person and calendar plans using the same mutations as the owner UI."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import date, datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import and_, select

from shortlist.server.api.seasons import DateRuleIO, PickIO, SeasonIn
from shortlist.server.api.users import BlockSeedBody, UserPatch, UserPrefs
from shortlist.server.db.models import (
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    SeasonDef,
    Setting,
    Theme,
    ThemeHistory,
    User,
)
from shortlist.server.services import context_builder
from shortlist.server.services.person_changes import apply_person_in_session, prepare_person_in_session
from shortlist.server.services.person_row_overrides import (
    RowOverridePatch,
    apply_person_row_override_in_session,
    prepare_person_row_override_in_session,
)
from shortlist.server.services.person_up_next import apply_up_next_in_session, prepare_up_next_in_session
from shortlist.server.services.season_changes import apply_season_in_session, prepare_season_in_session
from shortlist.server.services.theme_models import CollectionIO, TagIO
from shortlist.server.services.theme_store import TitleClash

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
    up_next_theme_id: int | None = Field(
        default=None,
        gt=0,
        description="A saved theme to queue for this person's Explore row; omit to leave it unchanged.",
    )

    @model_validator(mode="after")
    def values_are_present(self):
        if "up_next_theme_id" in self.model_fields_set and self.up_next_theme_id is None:
            raise ValueError("up_next_theme_id selects a saved theme; omit it instead of null")
        if not self.model_fields_set - {"row_id"}:
            raise ValueError("row_overrides entries require muted, row_size, recent_count or up_next_theme_id")
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


def table_snapshot(session, model, *, where=None) -> str:
    # Hash complete authoritative state, including private fields, without returning it to the assistant.
    records = []
    query = select(model).order_by(*model.__table__.primary_key.columns)
    if where is not None:
        query = query.where(where)
    for row in session.scalars(query):
        record = {}
        for column in model.__table__.columns:
            value = getattr(row, column.name)
            record[column.name] = value.isoformat() if isinstance(value, (date, datetime)) else value
        records.append(record)
    return fingerprint(records)


def table_dependencies(session) -> dict[str, str]:
    return {
        model.__tablename__: table_snapshot(session, model)
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

    def __init__(self, secrets=None) -> None:
        self.secrets = secrets

    @contextmanager
    def transaction_lock(self, normalized_intent: dict):
        from shortlist.server.services.theme_rotation import target_lock

        body = PeopleIntent.model_validate(normalized_intent)
        with ExitStack() as locks:
            for row_id in sorted(override.row_id for override in body.row_overrides if override.up_next_theme_id):
                locks.enter_context(target_lock(row_id, body.person_id))
            yield

    def _up_next(self, session, body: PeopleIntent):
        selections = []
        for override in body.row_overrides:
            if override.up_next_theme_id is None:
                continue
            try:
                selections.append(
                    prepare_up_next_in_session(
                        session, override.row_id, body.person_id, override.up_next_theme_id, secrets=self.secrets
                    )
                )
            except TitleClash:
                # The owner may see which private sibling collides; a proposing
                # connection must not learn its title before authorization.
                raise ValueError("The selected theme conflicts with another row title.") from None
            except LookupError as error:
                raise ValueError(str(error)) from None
        return selections

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
                    override.model_dump(mode="json", exclude={"row_id", "up_next_theme_id"}, exclude_unset=True)
                ),
            )
            for override in body.row_overrides
        ]
        selections = self._up_next(session, body)
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
        dependencies = table_dependencies(session)
        if selections:
            capabilities.add("themes.write")
            target_history = and_(
                ThemeHistory.collection_id.in_([selection.collection.id for selection in selections]),
                ThemeHistory.user_id == body.person_id,
                ThemeHistory.state.in_(("current", "next")),
            )
            selected_theme_ids = {selection.theme.id for selection in selections}
            selected_theme_ids.update(selection.collection.theme_id for selection in selections)
            selected_theme_ids.update(
                session.scalars(
                    select(ThemeHistory.theme_id).where(
                        target_history,
                        ThemeHistory.theme_id.is_not(None),
                    )
                )
            )
            dependencies["up_next_themes"] = table_snapshot(session, Theme, where=Theme.id.in_(selected_theme_ids))
            dependencies["up_next_history"] = table_snapshot(session, ThemeHistory, where=target_history)
            diff["up_next"] = [
                {"row_id": selection.collection.id, "person_id": body.person_id, "theme_id": selection.theme.id}
                for selection in selections
            ]
        libraries = {str(key) for row in rows for key in row.library_keys}
        if selections:
            libraries.update(
                str(source["section_key"]) for selection in selections for source in selection.theme.collections or []
            )
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json", exclude_unset=True),
            dependencies=dependencies,
            requirements=AccessRequirements(
                capabilities=tuple(sorted(capabilities)),
                row_ids=tuple(row.id for row in rows),
                person_ids=(body.person_id,),
                library_keys=tuple(sorted(libraries)),
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
                    override.model_dump(mode="json", exclude={"row_id", "up_next_theme_id"}, exclude_unset=True)
                ),
            )
            for override in body.row_overrides
        ]
        apply_person_in_session(session, person_mutation)
        for mutation in override_mutations:
            apply_person_row_override_in_session(session, mutation)
        selections = self._up_next(session, body)
        for selection in selections:
            apply_up_next_in_session(session, selection)
        changed_fields = sorted(person_mutation.changed)
        if selections:
            changed_fields.append("up_next")
        if override_mutations:
            changed_fields.append("row_overrides")
        return DomainResult(
            result={"person_id": body.person_id, "changed_fields": changed_fields},
            audit_diff={
                "person_id": body.person_id,
                "changed_fields": changed_fields,
                "up_next": [
                    {"row_id": selection.collection.id, "theme_id": selection.theme.id} for selection in selections
                ],
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
        dependencies = table_dependencies(session)
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
