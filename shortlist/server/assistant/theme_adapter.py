"""Plan a resolved, externally authored theme without invoking a provider."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import select

from shortlist.server.api.themes import RulesIO, ThemeIn, ThemePickIO, ThemeSaveIn, _refuse_unusable
from shortlist.server.assistant_auth import Capability
from shortlist.server.db.models import CacheRow, Collection, CollectionAudience, Theme, ThemeHistory, User
from shortlist.server.services import theme_store

from .changes import AccessRequirements, DomainPlan, DomainResult, fingerprint
from .contracts import StrictModel
from .discovery import row_snapshot


class AssistantRules(RulesIO):
    model_config = ConfigDict(extra="forbid", strict=True)


class AssistantPick(ThemePickIO):
    model_config = ConfigDict(extra="forbid", strict=True)
    tmdb_id: int = Field(gt=0, description="TMDB ID verified by shortlist_search_titles, for this exact media type.")
    origin: Literal["owner"] = "owner"


class AssistantTheme(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    emoji: str | None = Field(default=None, max_length=8)
    brief: str = Field(
        default="", max_length=1000, description="The theme's editorial intent; treated as untrusted descriptive text."
    )
    media: list[Literal["movie", "show"]] = Field(min_length=1, max_length=2)
    picks: list[AssistantPick] = Field(default_factory=list, max_length=200)
    genres: list[str] = Field(default_factory=list, max_length=10)
    excluded_genres: list[str] = Field(default_factory=list, max_length=10)
    rules: AssistantRules = Field(default_factory=AssistantRules)


class ThemeIntent(StrictModel):
    action: Literal["create", "update"]
    theme_id: int | None = Field(default=None, gt=0)
    draft: AssistantTheme

    @model_validator(mode="after")
    def coherent_action(self):
        if (self.action == "update") != (self.theme_id is not None):
            raise ValueError("update needs theme_id; create must omit it")
        return self


def _resolved_body(session, intent: ThemeIntent) -> ThemeSaveIn:
    data = intent.draft.model_dump(mode="json")
    for pick in data["picks"]:
        row = session.get(CacheRow, ("assistant_titles", f"{pick['media']}:{pick['tmdb_id']}"))
        if row is None or row.expires_at <= time.time():
            raise ValueError("Resolve every supplied title with shortlist_search_titles before planning its theme.")
        pick["title"] = row.value["title"]
        pick["year"] = row.value.get("year")
    draft = ThemeIn.model_validate(data)
    _refuse_unusable(draft)
    return ThemeSaveIn(draft=draft, tokens=0)


class ThemeAdapter:
    kind = "theme"

    def __init__(self, secrets) -> None:
        self.secrets = secrets

    def prepare(self, session, intent: dict) -> DomainPlan:
        body = ThemeIntent.model_validate(intent)
        resolved = _resolved_body(session, body)
        existing = session.get(Theme, body.theme_id) if body.theme_id else None
        if body.theme_id and existing is None:
            raise ValueError("theme not found")
        references = (
            list(session.scalars(select(Collection).where(Collection.theme_id == body.theme_id))) if existing else []
        )
        history = (
            list(
                session.scalars(
                    select(ThemeHistory).where(
                        ThemeHistory.theme_id == body.theme_id, ThemeHistory.state.in_(("current", "next"))
                    )
                )
            )
            if existing
            else []
        )
        known_rows = {row.id: row for row in references}
        for entry in history:
            row = session.get(Collection, entry.collection_id)
            if row:
                known_rows[row.id] = row
        people = {entry.user_id for entry in history if entry.user_id is not None}
        for row in known_rows.values():
            if row.audience == "everyone":
                people.update(session.scalars(select(User.id).where(User.removed_at.is_(None))))
            else:
                people.update(
                    session.scalars(
                        select(CollectionAudience.user_id).where(CollectionAudience.collection_id == row.id)
                    )
                )
        projection = Theme(id=body.theme_id, slug=existing.slug if existing else "draft")
        theme_store.write_theme(projection, resolved)
        if existing:
            theme_store.reject_title_clashes(session, self.secrets, projection)
        capabilities = {Capability.THEMES_WRITE.value, Capability.CONFIG_READ.value}
        if history:
            capabilities.add(Capability.HISTORY_EXPORT.value)
        # Updating a shared definition affects every current/next consumer, never just the named row.
        dependencies = {
            "theme": fingerprint(
                {column.name: str(getattr(existing, column.name)) for column in Theme.__table__.columns}
            )
            if existing
            else fingerprint(None),
            "rows": fingerprint([row_snapshot(row) for row in known_rows.values()]),
            "history": fingerprint([[entry.id, entry.collection_id, entry.user_id, entry.state] for entry in history]),
            "roster": fingerprint(sorted(people)),
            "resolved_draft": fingerprint(resolved.draft.model_dump(mode="json")),
        }
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json"),
            dependencies=dependencies,
            requirements=AccessRequirements(
                capabilities=tuple(sorted(capabilities)),
                row_ids=tuple(sorted(known_rows)),
                person_ids=tuple(sorted(people)),
                library_keys=tuple(sorted({str(key) for row in known_rows.values() for key in row.library_keys or []})),
                dynamic_rows=bool(existing and not known_rows),
                dynamic_audience=any(row.audience == "everyone" for row in known_rows.values()),
                dynamic_libraries=any(not row.library_keys for row in known_rows.values()),
                batch_size=len(resolved.draft.picks),
            ),
            summary={
                "description": f"{body.action.title()} the saved theme {body.draft.name}.",
                "pick_count": len(resolved.draft.picks),
                "draft": resolved.draft.model_dump(mode="json"),
                "affected_row_ids": sorted(known_rows),
                "provider_generation": False,
                "source": "external_assistant",
                "delivery": "Content changes take effect when an affected row next builds.",
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = ThemeIntent.model_validate(intent)
        existing = session.get(Theme, body.theme_id) if body.theme_id else None
        row = theme_store.save_theme(session, self.secrets, _resolved_body(session, body), existing=existing)
        row.origin = "assistant"
        session.flush()
        return DomainResult(
            result={
                "theme_id": row.id,
                "name": row.name,
                "revision": row.content_hash,
                "provider_generation": False,
                "origin": "assistant",
            },
            audit_diff={"theme_id": row.id, "action": body.action, "origin": "assistant"},
            references={"theme": row.id},
        )
