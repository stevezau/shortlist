"""One optional MCP resource server within the existing application lifespan."""

from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from loguru import logger
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from shortlist import __version__
from shortlist.server.assistant_auth.runtime import AssistantAuthSettings, build_assistant_auth_runtime

from .changes import ChangeService
from .guides import SERVER_INSTRUCTIONS
from .settings_adapter import SettingsAdapter
from .theme_adapter import ThemeAdapter
from .tools import register_tools


def settings_from_environment(environ: dict[str, str], *, base_path: str) -> AssistantAuthSettings:
    """Canonical origin is explicit deployment configuration, never inferred from Host headers."""
    resource = environ.get("SHORTLIST_MCP_URL", "").strip().rstrip("/")
    if not resource:
        return AssistantAuthSettings(enabled=False, issuer="", resource="")
    if urlsplit(resource).path != f"{base_path}/mcp":
        raise ValueError("SHORTLIST_MCP_URL must end with APP_BASE_PATH followed by /mcp")
    settings = AssistantAuthSettings(
        enabled=True, issuer=resource.removesuffix("/mcp") + "/assistant/oauth", resource=resource
    )
    settings.validate()
    return settings


def build_change_service(state) -> ChangeService:
    """Wire the closed domain registry; each adapter has a strict typed intent."""
    from .maintenance_adapter import MaintenanceAdapter
    from .people_seasons import PeopleAdapter, SeasonsAdapter
    from .request_adapter import RequestAdapter
    from .row_adapter import RowAdapter
    from .run_adapter import ConfiguredRunAdapter, RunAdapter
    from .setup_adapter import SetupAdapter

    adapters = [
        SettingsAdapter(state.secrets),
        ThemeAdapter(state.secrets),
        PeopleAdapter(state.secrets),
        SeasonsAdapter(state),
        RowAdapter(state),
        RequestAdapter(state),
        MaintenanceAdapter(state),
        RunAdapter(state),
        ConfiguredRunAdapter(state),
        SetupAdapter(state),
    ]
    return ChangeService(state.sessions, {adapter.kind: adapter for adapter in adapters})


def build_mcp_server(state, settings: AssistantAuthSettings) -> tuple[MCPServer, object]:
    """Build the official SDK resource server and its stateless HTTP application."""
    server = MCPServer(
        "Shortlist",
        title="Shortlist assistant",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        description="Set up and manage private Plex recommendation rows through scoped, reviewed changes.",
        auth=AuthSettings(
            issuer_url=settings.issuer,
            resource_server_url=settings.resource,
            required_scopes=["instance.read"],
            validate_token_resource=True,
        ),
        token_verifier=state.assistant_auth.token_verifier,
    )
    register_tools(server, state)
    parsed = urlsplit(settings.resource)
    transport = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=524_288,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[parsed.netloc],
            allowed_origins=[f"{parsed.scheme}://{parsed.netloc}"],
        ),
    )
    return server, transport


@asynccontextmanager
async def assistant_lifespan(app):
    """Enter the mounted SDK runtime explicitly, and close it before the database pool."""
    state = app.state
    state.assistant_auth = None
    state.assistant_transport = None
    state.assistant_changes = None
    state.assistant_configuration_error = None
    try:
        settings = settings_from_environment(dict(os.environ), base_path=state.base_path)
    except ValueError as error:
        state.assistant_configuration_error = str(error)
        logger.error("Assistant access is disabled: its canonical URL configuration is invalid")
        yield
        return
    if not settings.enabled:
        yield
        return
    hash_key = hmac.new(
        state.session_secret.encode(), b"shortlist-assistant-credential-hashes-v1", hashlib.sha256
    ).digest()
    state.assistant_auth = build_assistant_auth_runtime(
        sessions=state.sessions, credential_hash_key=hash_key, settings=settings
    )
    state.assistant_changes = build_change_service(state)
    server, transport = build_mcp_server(state, settings)
    async with server.session_manager.run():
        state.assistant_transport = transport
        try:
            yield
        finally:
            state.assistant_transport = None


class AssistantEndpoint:
    """Reserve /mcp ahead of the SPA, including when the feature is disabled."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        transport = getattr(self.app.state, "assistant_transport", None)
        if transport is None:
            response = JSONResponse({"error": "assistant_access_disabled"}, status_code=503)
            await response(scope, receive, send)
            return
        await transport(scope, receive, send)
