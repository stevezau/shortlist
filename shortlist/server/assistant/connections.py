"""Named browser credential handoffs; no credential ever crosses the assistant boundary."""

from __future__ import annotations

import secrets
import time
from typing import Literal

from pydantic import Field
from sqlalchemy import select, text

from shortlist.server.assistant_auth import AuthorizationDenied, Capability, ResourceSelection, require_authorized
from shortlist.server.assistant_auth.repository import require_current_grant_in_session
from shortlist.server.db.models import CacheRow, Server, Setting
from shortlist.server.services.connection_checks import READ_ONLY_PROBES, probe_read_only_connection
from shortlist.server.settings_store import SettingsStore

from .contracts import StrictModel, ToolResult
from .discovery import fingerprint

ConnectionKind = Literal[
    "plex",
    "tmdb",
    "curator",
    "tautulli",
    "trakt",
    "mdblist",
    "overseerr",
    "radarr",
    "sonarr",
    "notify",
    "exa",
    "searxng",
]

# Fixed browser destinations and database keys; neither can be supplied as a URL by a caller.
_CONNECTIONS = {
    "plex": ("plex", ("plex.url", "plex.token"), ("plex.token",)),
    "tmdb": ("tmdb", ("tmdb.apikey",), ("tmdb.apikey",)),
    "curator": (
        "llm",
        ("curator.provider", "curator.api_key", "curator.model", "curator.openai_base_url"),
        ("curator.api_key", "curator.openai_base_url"),
    ),
    "tautulli": ("tautulli", ("tautulli.url", "tautulli.apikey"), ("tautulli.apikey",)),
    "trakt": ("trakt", ("trakt.client_id",), ("trakt.client_id",)),
    "mdblist": ("mdblist", ("requests.mdblist.apikey",), ("requests.mdblist.apikey",)),
    "overseerr": ("overseerr", ("requests.overseerr.url", "requests.overseerr.apikey"), ("requests.overseerr.apikey",)),
    "radarr": ("radarr", ("requests.radarr.url", "requests.radarr.apikey"), ("requests.radarr.apikey",)),
    "sonarr": ("sonarr", ("requests.sonarr.url", "requests.sonarr.apikey"), ("requests.sonarr.apikey",)),
    "notify": (
        "notify",
        ("notify.webhook.url", "notify.webhook.auth_header_name", "notify.webhook.auth_header_value"),
        ("notify.webhook.url",),
    ),
    "exa": ("llm", ("llm_web.search_provider", "exa.apikey", "exa.search_type"), ("exa.apikey",)),
    "searxng": (
        "llm",
        ("llm_web.search_provider", "searxng.url", "searxng.username", "searxng.password"),
        ("searxng.url",),
    ),
}
_FLOW_KIND = "assistant_connection"
_LIFETIME = 1800


class ConnectionInput(StrictModel):
    service: ConnectionKind = Field(
        description="A named browser connection card. Credentials must be entered there by the owner."
    )


class ConnectionStatusInput(StrictModel):
    flow_id: str = Field(
        min_length=1,
        max_length=100,
        description="Opaque ID returned by start_connection, scoped to this connection and grant revision.",
    )


class ConnectionService:
    def __init__(self, state) -> None:
        self.state = state

    @staticmethod
    def _snapshot(session, service: str) -> tuple[str, bool]:
        if service not in _CONNECTIONS:
            raise ValueError("Choose a supported connection card.")
        _, keys, readiness_keys = _CONNECTIONS[service]
        values = {key: row.value if (row := session.get(Setting, key)) else None for key in keys}
        configured = any(bool(values.get(key) and values[key].get("v")) for key in readiness_keys)
        return fingerprint(values), configured

    @staticmethod
    def _identity(principal) -> dict:
        return {
            "grant_id": principal.grant_id,
            "owner_account_id": principal.owner_account_id,
            "client_id": principal.client_id,
            "grant_revision": principal.revision,
        }

    def start(self, principal, service: str) -> ToolResult:
        require_authorized(principal, [Capability.CONNECTIONS_MANAGE])
        now = time.time()
        flow_id = secrets.token_urlsafe(24)
        with self.state.sessions() as session:
            revision, configured = self._snapshot(session, service)
            active = list(
                session.scalars(select(CacheRow).where(CacheRow.kind == _FLOW_KIND, CacheRow.expires_at > now))
            )
            if sum(row.value.get("grant_id") == principal.grant_id for row in active) >= 20:
                raise ValueError("This connection already has 20 active handoffs. Complete them or wait for expiry.")
            session.add(
                CacheRow(
                    kind=_FLOW_KIND,
                    key=flow_id,
                    expires_at=now + _LIFETIME,
                    value={**self._identity(principal), "service": service, "initial_revision": revision},
                )
            )
            session.commit()
        browser_url = (
            self.state.assistant_auth.oauth.resource.removesuffix("/mcp")
            + "/settings/connections#connection-"
            + _CONNECTIONS[service][0]
        )
        return ToolResult(
            summary="Open Shortlist's browser connection card and enter credentials directly there.",
            data={
                "flow_id": flow_id,
                "service": service,
                "browser_url": browser_url,
                "configured": configured,
                "expires_in_seconds": _LIFETIME,
                "connectivity_verified": False,
            },
            warnings=["Do not paste credentials into chat. Configuration presence is not proof of connectivity."],
            next_action=browser_url,
        )

    def status(self, principal, flow_id: str) -> ToolResult:
        require_authorized(principal, [Capability.CONNECTIONS_MANAGE])
        with self.state.sessions() as session:
            flow = session.get(CacheRow, (_FLOW_KIND, flow_id))
            if flow is None or any(flow.value.get(key) != value for key, value in self._identity(principal).items()):
                raise AuthorizationDenied("Connection handoff is outside this grant.")
            if flow.expires_at <= time.time():
                return ToolResult(
                    summary="This browser handoff expired. Start another if needed.", data={"status": "expired"}
                )
            revision, configured = self._snapshot(session, flow.value["service"])
            changed = revision != flow.value["initial_revision"]
            return ToolResult(
                summary="Saved connection presence; no credentials or remote authorization result.",
                data={
                    "service": flow.value["service"],
                    "status": "configuration_changed" if changed else "waiting_for_owner",
                    "configured": configured,
                    "connectivity_verified": False,
                },
                next_action="shortlist_check_connection" if configured else "shortlist_start_connection",
            )

    def check(self, principal, service: str) -> ToolResult:
        require_authorized(principal, [Capability.CONNECTIONS_MANAGE])
        if service not in READ_ONLY_PROBES:
            return ToolResult(
                summary="This connection's live test requires a separate explicit browser action.",
                data={"service": service, "checked": False},
                warnings=[
                    "Provider tests may generate billable output, search tests may be billed, "
                    "and notification tests send a real message."
                ],
                next_action="shortlist_start_connection",
            )
        with self.state.sessions() as session:
            session.execute(text("BEGIN"))
            owner = session.scalar(select(Server.owner_account_id).limit(1))
            if owner is None:
                raise AuthorizationDenied("The verified owner is no longer available.")
            principal = require_current_grant_in_session(session, principal, current_owner_account_id=owner)
            require_authorized(principal, [Capability.CONNECTIONS_MANAGE])
            _, configured = self._snapshot(session, service)
            if not configured:
                return ToolResult(
                    summary="Enter this connection's credentials in Shortlist first.",
                    data={"service": service, "checked": False, "configured": False},
                    next_action="shortlist_start_connection",
                )
            store = SettingsStore(session, self.state.secrets)
            fixed = {
                "tmdb": "https://api.themoviedb.org/3",
                "trakt": "https://api.trakt.tv",
                "mdblist": "https://api.mdblist.com",
            }
            url_key = next((key for key in _CONNECTIONS[service][1] if key.endswith(".url")), None)
            destination = str(store.get(url_key) or "").rstrip("/") if url_key else fixed[service]
            require_authorized(principal, [], ResourceSelection(destination_ids=frozenset({destination})))
            # Freeze only this approved service's settings before any constructor can contact it.
            values = {key: store.get(key) for key in _CONNECTIONS[service][1]}
        try:
            probe_read_only_connection(service, values.get)
            ok = True
        except Exception:
            # Remote errors can contain request URLs, API keys or other private response data.
            ok = False
        return ToolResult(
            summary="The saved service responded successfully."
            if ok
            else "The saved service check failed. See Shortlist's browser settings for diagnostics.",
            data={"service": service, "configured": True, "checked": True, "ok": ok, "generation_performed": False},
            warnings=[
                "This is an API read and may count against the service's request quota. "
                "It does not prove later delivery will succeed."
            ],
        )
