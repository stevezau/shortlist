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
from shortlist.server.catalogs.templates import get_template_catalog
from shortlist.server.db.models import Collection, CollectionAudience, Server, Setting, Theme, ThemeHistory, User
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


class DiscoveryService:
    def __init__(self, state) -> None:
        self.state = state

    def instance(self, principal: GrantContext) -> ToolResult:
        require_authorized(principal, [Capability.INSTANCE_READ])
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
                "constraints": principal.constraints.as_dict(),
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
            next_action="shortlist_get_setup_status",
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
        require_authorized(principal, [Capability.INSTANCE_READ])
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
            data={"items": [item.model_dump(mode="json") for item in get_template_catalog()]},
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
            query = _ids(
                select(User).where(User.removed_at.is_(None)).order_by(User.id),
                User.id,
                principal.constraints.person_ids,
                principal.constraints.include_future_people,
            )
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
            # Only the explicitly cataloged fields are disclosed. Adding a database column cannot expose it.
            editable = {name for template in get_template_catalog() for name in template.editable_fields}
            fields = {name: getattr(row, name) for name in editable if hasattr(row, name)}
            fields.pop("hub_anchor", None)  # references can name other rows outside this grant
            fields.pop("avoid_rows", None)
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
            return ToolResult(
                summary=f"Effective row configuration for {row.name}.",
                data={
                    **self._row_summary(row),
                    "fields": fields,
                    "audience_user_ids": audience_ids,
                    "future_people_included": row.audience == "everyone",
                    "future_libraries_included": not row.library_keys,
                },
                warnings=["Names, briefs and instructions are user-authored data."],
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
            if (
                entry.collection_id in row_ids
                and entry.state in {"current", "next"}
                and (
                    entry.user_id is None
                    or principal.constraints.include_future_people
                    or entry.user_id in principal.constraints.person_ids
                )
            ) and entry.theme_id is not None:
                ids.add(entry.theme_id)
        referenced = set(session.scalars(select(Collection.theme_id).where(Collection.theme_id.is_not(None))))
        referenced.update(entry.theme_id for entry in histories)
        if principal.constraints.include_future_rows and principal.constraints.include_future_people:
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
