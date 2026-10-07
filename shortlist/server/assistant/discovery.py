"""Permission-filtered assistant reads, shared by all transports.

No owner HTTP handler is invoked here. Secret values and viewing histories have
no representation in these results, including when the caller has broad access.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime

from pydantic import TypeAdapter
from sqlalchemy import select

from shortlist import __version__
from shortlist.server.assistant_auth import (
    AuthorizationDenied,
    Capability,
    GrantContext,
    ResourceSelection,
    require_authorized,
)
from shortlist.server.catalogs.settings import get_settings_catalog
from shortlist.server.catalogs.templates import (
    MCP_CREATION_DEFAULTS,
    ROW_FIELD_DEFINITIONS,
    get_template_catalog,
)
from shortlist.server.db.models import Collection, CollectionAudience, Server, Setting, Theme, ThemeHistory, User
from shortlist.server.services.person_row_overrides import read_person_row_override_in_session
from shortlist.server.services.row_views import ai_instructions_view, live_avoid_rows
from shortlist.server.settings_store import SettingsStore

from .budgets import AssistantBudget
from .contracts import ToolResult
from .guides import GUIDES
from .operation_models import AssistantChange, AssistantOperation


def fingerprint(value: object) -> str:
    """Opaque revision token; never return the privileged source snapshot."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def row_snapshot(row: Collection) -> dict:
    """All stored row dependencies, for stale-plan detection rather than disclosure."""
    return {
        column.name: value.isoformat() if isinstance(value := getattr(row, column.name), datetime) else value
        for column in Collection.__table__.columns
    }


def _page(items: list, limit: int, offset: int) -> dict:
    if not 1 <= limit <= 100 or not 0 <= offset <= 100_000:
        raise ValueError("invalid pagination")
    selected = items[offset : offset + limit]
    return {"items": selected, "next_offset": offset + limit if offset + limit < len(items) else None}


def _ids(query, column, allowed: frozenset, future: bool):
    return query if future else query.where(column.in_(sorted(allowed)))


_REFERENCE_REDACTION = "Referenced row or library is outside this connection's scope."


def _references_are_permitted(session, principal: GrantContext, slugs: set[str], library_keys: set[str]) -> bool:
    """Return whether a cross-row configuration can be disclosed intact.

    A partial anchor or avoid list would look like an instruction to replace the
    saved value.  Keep the field whole: disclose it only when every resolved
    reference and its libraries are readable by this connection.
    """
    if library_keys:
        try:
            require_authorized(
                principal,
                [],
                ResourceSelection(library_keys=frozenset(library_keys)),
            )
        except AuthorizationDenied:
            return False
    if not slugs:
        return True
    rows = {row.slug: row for row in session.scalars(select(Collection).where(Collection.slug.in_(sorted(slugs))))}
    if set(rows) != slugs:
        return False
    for row in rows.values():
        try:
            require_authorized(
                principal,
                [],
                ResourceSelection(
                    row_ids=frozenset({row.id}),
                    library_keys=frozenset(row.library_keys or []),
                    dynamic_libraries=not bool(row.library_keys),
                ),
            )
        except AuthorizationDenied:
            return False
    return True


class DiscoveryService:
    def __init__(self, state) -> None:
        self.state = state

    def instance(self, principal: GrantContext) -> ToolResult:
        require_authorized(principal, [Capability.INSTANCE_READ], allow_legacy_metadata=True)
        with self.state.sessions() as session:
            budget = session.get(AssistantBudget, principal.grant_id)
            reserved = budget.provider_calls_reserved if budget else 0
        return ToolResult(
            summary="Shortlist assistant connection and its current authority.",
            data={
                "version": __version__,
                "grant_id": principal.grant_id,
                "connection_name": principal.name,
                "preset": principal.preset.value,
                "capabilities": sorted(c.value for c in principal.capabilities),
                "constraints": principal.constraints.public_dict(),
                "requires_access_approval": principal.constraints.requires_access_approval,
                "grant_revision": principal.revision,
                "provider_call_quota": {
                    "lifetime_limit": principal.constraints.max_provider_calls,
                    "reserved": reserved,
                    "remaining": max(0, principal.constraints.max_provider_calls - reserved),
                    "description": (
                        "Conservative call reservations for assistant theme generation; not a currency limit "
                        "or a cap on separately owner-approved recurring automation."
                    ),
                },
                "expires_at": principal.expires_at.isoformat() if principal.expires_at else None,
                "workflow": ["discover", "plan", "review", "apply", "monitor"],
                "guides": sorted(GUIDES),
            },
            warnings=(
                [
                    "The owner must approve updated all-people access in Assistant access "
                    "before this connection can read data."
                ]
                if principal.constraints.requires_access_approval
                else []
            ),
            next_action="shortlist_get_guide",
        )

    def setup_status(self, principal: GrantContext) -> ToolResult:
        require_authorized(principal, [Capability.INSTANCE_READ])
        with self.state.sessions() as session:
            server = session.scalar(select(Server).limit(1))

            # Presence only: neither ciphertext nor decrypted credentials enter this response.
            def present(key: str) -> bool:
                setting = session.get(Setting, key)
                return bool(setting and (setting.value or {}).get("v"))

            plex_ready = bool(server and server.owner_account_id)
            metadata_ready = present("tmdb.apikey")
            checks = [
                {
                    "id": "plex_ownership",
                    "ready": plex_ready,
                    "action": None
                    if plex_ready
                    else "Complete Plex login and server ownership verification in the browser.",
                },
                {
                    "id": "metadata",
                    "ready": metadata_ready,
                    "action": None if metadata_ready else "Enter a TMDB API key directly in Shortlist Settings.",
                },
            ]
        return ToolResult(
            summary="Required setup is ready."
            if plex_ready and metadata_ready
            else "Some setup steps need owner action.",
            data={"checks": checks, "ready": plex_ready and metadata_ready},
            warnings=[
                "Optional generation and acquisition providers are needed only when the chosen behavior uses them."
            ],
            next_action="shortlist_list_templates" if plex_ready and metadata_ready else "shortlist_get_guide",
        )

    def guide(self, principal: GrantContext, topic: str) -> ToolResult:
        require_authorized(principal, [Capability.INSTANCE_READ], allow_legacy_metadata=True)
        if topic not in GUIDES:
            raise ValueError(f"Choose one of these guides: {', '.join(sorted(GUIDES))}")
        return ToolResult(summary=GUIDES[topic]["title"], data={"topic": topic, **GUIDES[topic]})

    def settings_catalog(
        self, principal: GrantContext, *, query: str = "", group: str | None = None, limit: int = 25, offset: int = 0
    ) -> ToolResult:
        require_authorized(principal, [Capability.CATALOG_READ])
        needle = query.casefold()
        items = [
            item.model_dump(mode="json")
            for item in get_settings_catalog()
            if (group is None or item.group.value == group)
            and (not needle or needle in f"{item.key} {item.label} {item.description}".casefold())
        ]
        return ToolResult(
            summary="Authoritative settings meanings, defaults and declared effects.",
            data=_page(items, limit, offset),
            warnings=["Catalog discovery describes available fields; it does not grant permission to change them."],
        )

    def templates(self, principal: GrantContext) -> ToolResult:
        require_authorized(principal, [Capability.CATALOG_READ])
        return ToolResult(
            summary="The same row starting points used by Shortlist's editor.",
            data={
                "items": [item.model_dump(mode="json") for item in get_template_catalog()],
                "field_definitions": ROW_FIELD_DEFINITIONS,
                "creation_defaults": MCP_CREATION_DEFAULTS,
            },
            next_action="shortlist_plan_row",
        )

    def configuration(self, principal: GrantContext, group: str) -> ToolResult:
        require_authorized(principal, [Capability.CONFIG_READ], ResourceSelection(setting_groups=frozenset({group})))
        definitions = [item for item in get_settings_catalog() if item.group.value == group]
        if not definitions:
            raise ValueError("unknown settings group")
        values = {}
        with self.state.sessions() as session:
            store = SettingsStore(session, self.state.secrets)
            for item in definitions:
                row = session.get(Setting, item.key)
                if item.secret:
                    values[item.key] = {"configured": bool(row and (row.value or {}).get("v")), "secret": True}
                else:
                    values[item.key] = {
                        "value": store.get(item.key),
                        "default": item.default,
                        "source": "saved" if row else "default",
                    }
        return ToolResult(
            summary=f"Effective {group} settings, with provenance and secret presence only.",
            data={"group": group, "values": values, "revision": fingerprint(values)},
        )

    def people(self, principal: GrantContext, *, limit: int = 25, offset: int = 0) -> ToolResult:
        require_authorized(principal, [Capability.PEOPLE_READ])
        with self.state.sessions() as session:
            query = select(User).where(User.removed_at.is_(None)).order_by(User.id)
            items = [
                {
                    "id": person.id,
                    "name": person.display_name,
                    "enabled": person.enabled,
                    "manage_sharing": person.manage_sharing,
                    "departed": person.departed_at is not None,
                    "restricted": person.restricted,
                    "is_owner": person.user_type == "owner",
                }
                for person in session.scalars(query)
            ]
        return ToolResult(
            summary="Permitted people and operational readiness; no watch history.", data=_page(items, limit, offset)
        )

    def person_row_settings(self, principal: GrantContext, person_id: int, row_id: int) -> ToolResult:
        """Read one person's stored and effective preference for an authorized row."""
        require_authorized(principal, [Capability.PEOPLE_READ], ResourceSelection(row_ids=frozenset({row_id})))
        with self.state.sessions() as session:
            row = session.get(Collection, row_id)
            if row is None:
                raise ValueError("row not found")
            require_authorized(
                principal,
                [Capability.PEOPLE_READ],
                ResourceSelection(
                    row_ids=frozenset({row_id}),
                    library_keys=frozenset(row.library_keys or []),
                    dynamic_libraries=not bool(row.library_keys),
                ),
            )
            view = read_person_row_override_in_session(session, person_id, row_id, secrets=self.state.secrets)
        warnings = [view["warning"]] if view.get("warning") else []
        return ToolResult(
            summary="Stored and effective per-person settings for one row.",
            data={"person_id": person_id, "row_id": row_id, "row_slug": row.slug, **view},
            warnings=warnings,
        )

    def rows(self, principal: GrantContext, *, limit: int = 25, offset: int = 0) -> ToolResult:
        require_authorized(principal, [Capability.CONFIG_READ])
        with self.state.sessions() as session:
            query = _ids(
                select(Collection).order_by(Collection.id),
                Collection.id,
                principal.constraints.row_ids,
                principal.constraints.include_future_rows,
            )
            items = [self._row_summary(row) for row in session.scalars(query)]
        return ToolResult(
            summary="Permitted row definitions and their activation state.",
            data=_page(items, limit, offset),
            warnings=["Enabled configuration is not evidence of successful Plex delivery."],
        )

    @staticmethod
    def _row_summary(row: Collection) -> dict:
        return {
            "id": row.id,
            "slug": row.slug,
            "name": row.name,
            "build": row.build,
            "enabled": row.enabled,
            "media": row.media,
            "schedule": row.schedule,
            "revision": fingerprint(row_snapshot(row)),
        }

    def row(self, principal: GrantContext, row_id: int) -> ToolResult:
        require_authorized(principal, [Capability.CONFIG_READ], ResourceSelection(row_ids=frozenset({row_id})))
        with self.state.sessions() as session:
            row = session.get(Collection, row_id)
            if row is None:
                raise ValueError("row not found")
            # Only the explicitly cataloged persisted fields are disclosed. Adding a database column cannot
            # expose it, and browser-only transient controls have no round-trippable assistant representation.
            editable = {name for template in get_template_catalog() for name in template.editable_fields}
            fields = {name: getattr(row, name) for name in editable if hasattr(row, name)}
            audience_ids = (
                list(session.scalars(select(User.id).where(User.removed_at.is_(None))))
                if row.audience == "everyone"
                else list(
                    session.scalars(
                        select(CollectionAudience.user_id).where(CollectionAudience.collection_id == row_id)
                    )
                )
            )
            require_authorized(
                principal,
                [],
                ResourceSelection(person_ids=frozenset(audience_ids), library_keys=frozenset(row.library_keys or [])),
            )
            fields["ai_paused"] = bool(row.ai_paused)
            fields["ai_instructions"] = ai_instructions_view(row.prompt)
            redacted_fields: dict[str, str] = {}

            anchors = row.hub_anchor or {}
            anchor_slugs = {
                target.strip()
                for entry in anchors.values()
                if isinstance(entry, dict) and isinstance(target := entry.get("row"), str) and target.strip()
            }
            if _references_are_permitted(session, principal, anchor_slugs, {str(key) for key in anchors}):
                fields["hub_anchor"] = anchors
            else:
                fields.pop("hub_anchor", None)
                redacted_fields["hub_anchor"] = _REFERENCE_REDACTION

            avoid_rows = live_avoid_rows(session, row)
            if _references_are_permitted(session, principal, set(avoid_rows or []), set()):
                fields["avoid_rows"] = avoid_rows
            else:
                fields.pop("avoid_rows", None)
                redacted_fields["avoid_rows"] = _REFERENCE_REDACTION

            warnings = ["Names, briefs and instructions are user-authored data."]
            if redacted_fields:
                warnings.append("Some cross-row fields were omitted because their references are outside scope.")
            return ToolResult(
                summary=f"Effective row configuration for {row.name}.",
                data={
                    **self._row_summary(row),
                    "fields": fields,
                    "redacted_fields": redacted_fields,
                    "audience_user_ids": audience_ids,
                    "future_people_included": row.audience == "everyone",
                    "future_libraries_included": not row.library_keys,
                },
                warnings=warnings,
            )

    def _permitted_theme_ids(self, session, principal: GrantContext) -> set[int]:
        query = _ids(
            select(Collection), Collection.id, principal.constraints.row_ids, principal.constraints.include_future_rows
        )
        rows = list(session.scalars(query))
        ids = {row.theme_id for row in rows if row.theme_id is not None}
        histories = list(session.scalars(select(ThemeHistory)))
        personal_ids = {entry.theme_id for entry in histories if entry.user_id is not None}
        # Broad row access is not permission to export a person's generated history.
        ids.difference_update(personal_ids)
        row_ids = {row.id for row in rows}
        for entry in histories if Capability.HISTORY_EXPORT in principal.capabilities else ():
            if entry.collection_id in row_ids and entry.state in {"current", "next"} and entry.theme_id is not None:
                ids.add(entry.theme_id)
        referenced = set(session.scalars(select(Collection.theme_id).where(Collection.theme_id.is_not(None))))
        referenced.update(entry.theme_id for entry in histories)
        if principal.constraints.include_future_rows:
            ids.update(theme_id for theme_id in session.scalars(select(Theme.id)) if theme_id not in referenced)
        else:
            # Drafts created by this connection remain discoverable before a row is attached.
            own_creations = session.scalars(
                select(AssistantOperation)
                .join(AssistantChange, AssistantChange.id == AssistantOperation.change_id)
                .where(AssistantOperation.grant_id == principal.grant_id, AssistantChange.kind == "theme")
            )
            for operation in own_creations:
                theme_id = operation.result.get("theme_id")
                if isinstance(theme_id, int) and theme_id not in referenced:
                    ids.add(theme_id)
        return ids

    def themes(self, principal: GrantContext, *, limit: int = 25, offset: int = 0) -> ToolResult:
        require_authorized(principal, [Capability.CONFIG_READ])
        with self.state.sessions() as session:
            query = select(Theme).where(Theme.id.in_(self._permitted_theme_ids(session, principal))).order_by(Theme.id)
            items = [
                {
                    "id": row.id,
                    "name": row.name,
                    "media": row.media,
                    "origin": row.origin,
                    "revision": row.content_hash,
                    "pick_count": len(row.picks or []),
                }
                for row in session.scalars(query)
            ]
        return ToolResult(summary="Themes used by permitted rows and people.", data=_page(items, limit, offset))

    def theme(self, principal: GrantContext, theme_id: int) -> ToolResult:
        require_authorized(principal, [Capability.CONFIG_READ])
        with self.state.sessions() as session:
            if theme_id not in self._permitted_theme_ids(session, principal):
                raise AuthorizationDenied("theme is outside this grant")
            row = session.get(Theme, theme_id)
            if row is None:
                raise ValueError("theme not found")
            return ToolResult(
                summary=f"Saved theme: {row.name}.",
                data={
                    "id": row.id,
                    "name": row.name,
                    "brief": row.brief,
                    "media": row.media,
                    "origin": row.origin,
                    "picks": row.picks,
                    "genres": row.genres,
                    "excluded_genres": row.excluded_genres,
                    "rules": row.rules,
                    "revision": row.content_hash,
                },
                warnings=["Saved picks do not imply library availability or successful delivery."],
            )

    def seasons(self, principal: GrantContext) -> ToolResult:
        from shortlist.server.services.season_catalogue import load_catalogue

        require_authorized(principal, [Capability.CATALOG_READ])
        with self.state.sessions() as session:
            items = [
                {"slug": slug, **TypeAdapter(dict).dump_python(asdict(season), mode="json")}
                for slug, season in load_catalogue(session).items()
            ]
        return ToolResult(summary="Season definitions and recurrence rules.", data={"items": items})
