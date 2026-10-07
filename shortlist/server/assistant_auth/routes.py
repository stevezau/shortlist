"""FastAPI routes for browser owner management and OAuth public endpoints."""

from __future__ import annotations

import json
import secrets
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shortlist.server.auth import _check_csrf, read_session

from .authlib_adapter import ASGIAuthorizationServer, PreparedOAuthRequest
from .oauth import OAuthService
from .policy import AuthorizationDenied
from .repository import AssistantAuthRepository, GrantRemovalConflict, GrantUpdateConflict, GrantUpdateNotFound
from .types import ASSISTANT_CAPABILITIES, Capability, GrantConstraints, GrantPreset, GrantSummary


class BrowserOwner(BaseModel):
    """Owner identity established exclusively from the signed browser session."""

    account_id: int


_SAFE_OAUTH_ERRORS = frozenset(
    {
        "access_denied",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "invalid_target",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_response_type",
    }
)


def _safe_oauth_error(response: JSONResponse) -> JSONResponse:
    """Return a browser-safe error when Authlib cannot produce a redirect."""
    status_code = response.status_code if 400 <= response.status_code < 500 else 400
    error = "authorization_failed"
    try:
        payload = json.loads(response.body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and payload.get("error") in _SAFE_OAUTH_ERRORS:
        error = payload["error"]
    return JSONResponse({"error": error}, status_code=status_code)


def require_browser_owner(request: Request) -> BrowserOwner:
    """Require owner cookie + CSRF and reject every bearer credential.

    The legacy owner dependency intentionally accepts an all-powerful API token.
    Grant administration and consent need stronger provenance: an interactive
    browser owner session. Assistant and legacy bearer tokens both fail here.
    """
    if request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="assistant access changes require the owner browser session")
    _check_csrf(request)
    browser_session = read_session(request)
    owner_account_id = request.app.state.owner_account_id()
    if browser_session is None:
        raise HTTPException(status_code=401, detail="not signed in — use Login with Plex")
    if owner_account_id is None or browser_session.get("account_id") != owner_account_id:
        raise HTTPException(status_code=403, detail="only the server owner can manage assistant access")
    return BrowserOwner(account_id=owner_account_id)


BrowserOwnerDep = Annotated[BrowserOwner, Depends(require_browser_owner)]


class ConstraintsIn(BaseModel):
    """Strict owner-selected resource bounds for a grant."""

    model_config = ConfigDict(extra="forbid")

    row_ids: set[int] = Field(default_factory=set)
    library_keys: set[str] = Field(default_factory=set)
    setting_groups: set[str] = Field(default_factory=set)
    destination_ids: set[str] = Field(default_factory=set)
    include_future_rows: bool = True
    include_future_libraries: bool = True
    max_batch_size: int | None = Field(default=25, ge=1, le=1000)
    max_work_per_operation: int | None = Field(default=None, ge=1, le=100_000)
    max_provider_calls: int = Field(default=0, ge=0, le=100)

    def to_domain(self) -> GrantConstraints:
        has_rows = bool(self.row_ids)
        has_libraries = bool(self.library_keys)
        future_rows_supplied = "include_future_rows" in self.model_fields_set
        future_libraries_supplied = "include_future_libraries" in self.model_fields_set
        include_future_rows = self.include_future_rows if future_rows_supplied else not has_rows
        include_future_libraries = self.include_future_libraries if future_libraries_supplied else not has_libraries
        return GrantConstraints(
            row_ids=frozenset(self.row_ids),
            library_keys=frozenset(self.library_keys),
            setting_groups=frozenset(self.setting_groups),
            destination_ids=frozenset(self.destination_ids),
            include_future_rows=include_future_rows,
            include_future_libraries=include_future_libraries,
            max_batch_size=self.max_batch_size,
            max_work_per_operation=self.max_work_per_operation,
            max_provider_calls=self.max_provider_calls,
            include_future_people=True,
        )


class GrantCreateIn(BaseModel):
    """Owner-approved named assistant grant."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    preset: GrantPreset
    capabilities: set[Capability] | None = None
    constraints: ConstraintsIn = Field(default_factory=ConstraintsIn)
    expires_in_days: int | None = Field(default=90, ge=1, le=365)


class ConstraintsPatchIn(BaseModel):
    """Sparse owner-selected changes to a grant's resource bounds."""

    model_config = ConfigDict(extra="forbid")

    row_ids: set[int] = Field(default_factory=set)
    library_keys: set[str] = Field(default_factory=set)
    setting_groups: set[str] = Field(default_factory=set)
    destination_ids: set[str] = Field(default_factory=set)
    include_future_rows: bool = False
    include_future_libraries: bool = False
    max_batch_size: int | None = Field(default=None, ge=1, le=1000)
    max_work_per_operation: int | None = Field(default=None, ge=1, le=100_000)
    max_provider_calls: int = Field(default=0, ge=0, le=100)

    def explicit_values(self) -> dict[str, object]:
        """Return only fields the browser explicitly supplied, including nulls and empties."""
        return {field: getattr(self, field) for field in self.model_fields_set}


class GrantConstraintsPatchIn(BaseModel):
    """Revision-guarded, constraints-only change to one owner grant."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(gt=0)
    constraints: ConstraintsPatchIn | None = None
    approve_updated_access: bool = False

    @model_validator(mode="after")
    def require_one_change(self) -> GrantConstraintsPatchIn:
        """Keep legacy approval separate from ordinary resource edits."""
        if self.approve_updated_access and self.constraints is not None:
            raise ValueError("approve_updated_access cannot be combined with constraints")
        if not self.approve_updated_access and self.constraints is None:
            raise ValueError("constraints are required unless approving updated access")
        return self


class ConsentDecisionIn(BaseModel):
    """One exact browser consent decision protected by an independent CSRF token."""

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    csrf_token: str
    approved: bool
    grant_id: str | None = None


class OAuthAuthorizeIn(BaseModel):
    """Bounded authorization request copied from the browser URL by the SPA."""

    model_config = ConfigDict(extra="forbid")

    response_type: str = "code"
    client_id: str = Field(min_length=1, max_length=255)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    resource: str = Field(min_length=1, max_length=2048)
    scope: str = Field(default=Capability.INSTANCE_READ.value, max_length=2048)
    state: str = Field(min_length=1, max_length=2048)
    code_challenge: str = Field(min_length=43, max_length=128)
    code_challenge_method: str = "S256"


class LocalCredentialIn(BaseModel):
    """Lifetime for a one-time local credential handoff."""

    model_config = ConfigDict(extra="forbid")

    expires_in_days: int = Field(default=90, ge=1, le=365)


class DynamicClientRegistrationIn(BaseModel):
    """RFC 7591 subset for public authorization-code clients."""

    # RFC 7591 §2 requires servers to ignore unrecognized client metadata.
    model_config = ConfigDict(extra="ignore")

    client_name: str = Field(min_length=1, max_length=255)
    redirect_uris: list[Annotated[str, Field(min_length=1, max_length=2048)]] = Field(min_length=1, max_length=10)
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] = Field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: list[str] = Field(default_factory=lambda: ["code"])


def _runtime(request: Request):
    runtime = getattr(request.app.state, "assistant_auth", None)
    if runtime is None:
        raise HTTPException(status_code=404, detail="assistant access is disabled")
    return runtime


def create_owner_grant_router(repository: AssistantAuthRepository | None = None) -> APIRouter:
    """Create explicit browser-only grant list/create/revoke routes."""
    router = APIRouter(prefix="/assistant/grants", tags=["assistant-access"])

    @router.get("")
    def list_grants(request: Request, owner: BrowserOwnerDep) -> list[dict[str, Any]]:
        current_repository = repository or _runtime(request).repository
        return [_serialize_grant_summary(grant) for grant in current_repository.list_grant_summaries(owner.account_id)]

    @router.post("", status_code=201)
    def create_grant(request: Request, body: GrantCreateIn, owner: BrowserOwnerDep) -> dict[str, Any]:
        current_repository = repository or _runtime(request).repository
        expires_at = (
            datetime.now(UTC) + timedelta(days=body.expires_in_days) if body.expires_in_days is not None else None
        )
        try:
            grant = current_repository.create_grant(
                owner_account_id=owner.account_id,
                client_id=body.client_id,
                name=body.name,
                preset=body.preset,
                capabilities=body.capabilities,
                constraints=body.constraints.to_domain(),
                expires_at=expires_at,
            )
        except AuthorizationDenied as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return _serialize_grant(grant)

    @router.patch("/{grant_id}")
    def patch_grant_constraints(
        request: Request,
        grant_id: str,
        body: GrantConstraintsPatchIn,
        owner: BrowserOwnerDep,
    ) -> dict[str, Any]:
        """Apply explicit constraint changes without expanding grant administration."""
        current_repository = repository or _runtime(request).repository
        try:
            if body.approve_updated_access:
                grant = current_repository.approve_updated_access(
                    grant_id,
                    owner_account_id=owner.account_id,
                    expected_revision=body.expected_revision,
                )
            else:
                grant = current_repository.patch_grant_constraints(
                    grant_id,
                    owner_account_id=owner.account_id,
                    expected_revision=body.expected_revision,
                    constraints_patch=body.constraints.explicit_values() if body.constraints else {},
                )
        except GrantUpdateNotFound as error:
            raise HTTPException(status_code=404, detail="assistant grant not found") from error
        except GrantUpdateConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _serialize_grant(grant)

    @router.post("/{grant_id}/credentials", status_code=201)
    def issue_credential(
        request: Request,
        grant_id: str,
        body: LocalCredentialIn,
        owner: BrowserOwnerDep,
    ) -> dict[str, Any]:
        current_repository = repository or _runtime(request).repository
        grant = current_repository.get_grant_context(grant_id)
        if grant is None or grant.owner_account_id != owner.account_id:
            raise HTTPException(status_code=404, detail="assistant grant not found")
        expires_at = datetime.now(UTC) + timedelta(days=body.expires_in_days)
        credential = current_repository.issue_local_credential(grant_id, expires_at=expires_at).take()
        return {"credential": credential, "expires_at": expires_at.isoformat()}

    @router.post("/{grant_id}/revoke")
    def revoke_grant(request: Request, grant_id: str, owner: BrowserOwnerDep) -> dict[str, bool]:
        current_repository = repository or _runtime(request).repository
        grant = current_repository.get_grant_context(grant_id)
        if grant is None or grant.owner_account_id != owner.account_id:
            raise HTTPException(status_code=404, detail="assistant grant not found")
        return {"revoked": current_repository.revoke_grant(grant_id)}

    @router.delete("/{grant_id}", status_code=204)
    def remove_revoked_grant(request: Request, grant_id: str, owner: BrowserOwnerDep) -> None:
        """Forget an inactive connection record without deleting its audit history."""
        current_repository = repository or _runtime(request).repository
        try:
            current_repository.remove_revoked_grant(grant_id, owner_account_id=owner.account_id)
        except GrantRemovalConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return router


def create_oauth_router(
    repository: AssistantAuthRepository | None = None,
    oauth: OAuthService | None = None,
) -> APIRouter:
    """Create discovery, consent, token, and revocation endpoints."""
    router = APIRouter(prefix="/assistant/oauth", tags=["assistant-oauth"])

    @router.get("/authorize")
    def open_consent(request: Request) -> RedirectResponse:
        """Hand the browser to the SPA; it initializes state after owner login."""
        if len(str(request.url.query)) > 8_192:
            raise HTTPException(status_code=414, detail="OAuth authorization request is too large")
        # Resolve from .../assistant/oauth/authorize to .../assistant/consent. Keep this
        # relative so an externally mounted base path remains part of the browser URL.
        target = "../consent"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})

    @router.post("/authorize")
    def begin_consent(request: Request, body: OAuthAuthorizeIn, owner: BrowserOwnerDep) -> dict[str, Any]:
        current_runtime = None if repository is not None and oauth is not None else _runtime(request)
        current_repository = repository or current_runtime.repository
        current_oauth = oauth or current_runtime.oauth
        authorization_server = ASGIAuthorizationServer(current_oauth)
        if body.response_type != "code":
            raise HTTPException(status_code=400, detail="invalid OAuth authorization request")
        scopes = set(body.scope.split()) or {Capability.INSTANCE_READ.value}
        validation = current_oauth.validate_authorization_request(
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            resource=body.resource,
            scopes=scopes,
            code_challenge=body.code_challenge,
            code_challenge_method=body.code_challenge_method,
        )
        if not validation.is_valid:
            raise HTTPException(status_code=400, detail=validation.error)
        prepared = PreparedOAuthRequest.authorization(
            canonical_url=f"{current_oauth.issuer}/authorize",
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            resource=body.resource,
            scope=" ".join(sorted(scopes)),
            state=body.state,
            code_challenge=body.code_challenge,
            code_challenge_method=body.code_challenge_method,
        )
        try:
            authorization_server.get_consent_grant(prepared, end_user=owner)
        except Exception as error:
            if getattr(error, "error", None):
                raise HTTPException(status_code=400, detail=error.error) from error
            raise
        client = current_repository.get_oauth_client(body.client_id)
        now = datetime.now(UTC)
        flow_id, csrf_secret = current_repository.create_consent_flow(
            owner_account_id=owner.account_id,
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            resource=body.resource,
            scope=" ".join(sorted(scopes)),
            client_state=body.state,
            code_challenge=body.code_challenge,
            code_challenge_method=body.code_challenge_method,
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
        return {
            "flow_id": flow_id,
            "csrf_token": csrf_secret.take(),
            "client": {"id": client.client_id, "name": client.client_name},
            "requested_scopes": sorted(scopes),
            "resource": body.resource,
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
        }

    @router.post("/consent")
    def decide_consent(request: Request, body: ConsentDecisionIn, owner: BrowserOwnerDep) -> dict[str, str]:
        current_runtime = None if repository is not None and oauth is not None else _runtime(request)
        current_repository = repository or current_runtime.repository
        current_oauth = oauth or current_runtime.oauth
        authorization_server = ASGIAuthorizationServer(current_oauth)
        now = datetime.now(UTC)
        flow = current_repository.consume_consent_flow(
            body.flow_id,
            owner_account_id=owner.account_id,
            csrf_token=body.csrf_token,
            approved=body.approved,
            grant_id=body.grant_id,
            now=now,
        )
        if flow is None:
            raise HTTPException(status_code=400, detail="consent flow is invalid, expired, or already used")
        if body.approved and not body.grant_id:
            raise HTTPException(status_code=400, detail="approved consent requires a grant")
        grant_context = current_repository.get_grant_context(body.grant_id, now=now) if body.grant_id else None
        if grant_context is not None and (
            grant_context.owner_account_id != owner.account_id or grant_context.client_id != flow.client_id
        ):
            grant_context = None
        requested_scopes = frozenset(flow.scope.split())
        approved_scopes = requested_scopes
        if body.approved:
            if grant_context is None:
                return JSONResponse({"error": "invalid_scope"}, status_code=400)
            approved_scopes = requested_scopes & frozenset(
                capability.value for capability in grant_context.capabilities
            )
            if not approved_scopes:
                return JSONResponse({"error": "invalid_scope"}, status_code=400)
        prepared = PreparedOAuthRequest.authorization(
            canonical_url=f"{current_oauth.issuer}/authorize",
            client_id=flow.client_id,
            redirect_uri=flow.redirect_uri,
            resource=flow.resource,
            scope=" ".join(sorted(approved_scopes)),
            state=flow.client_state,
            code_challenge=flow.code_challenge,
            code_challenge_method=flow.code_challenge_method,
        )
        consent_grant = authorization_server.get_consent_grant(prepared, end_user=owner)
        response = authorization_server.create_authorization_response(
            prepared,
            grant_user=grant_context if body.approved else None,
            grant=consent_grant,
        )
        location = response.headers.get("location")
        if location is None:
            return _safe_oauth_error(response)
        if body.approved and approved_scopes != requested_scopes:
            parsed = urlsplit(location)
            query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "scope"]
            query.append(("scope", " ".join(sorted(approved_scopes))))
            location = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        return {"redirect_to": location}

    @router.post("/token")
    async def token(request: Request) -> JSONResponse:
        current_oauth = oauth or _runtime(request).oauth
        authorization_server = ASGIAuthorizationServer(current_oauth)
        prepared = await PreparedOAuthRequest.from_starlette(request, canonical_url=f"{current_oauth.issuer}/token")
        return authorization_server.create_token_response(prepared)

    @router.post("/revoke")
    async def revoke(request: Request) -> JSONResponse:
        current_oauth = oauth or _runtime(request).oauth
        authorization_server = ASGIAuthorizationServer(current_oauth)
        prepared = await PreparedOAuthRequest.from_starlette(request, canonical_url=f"{current_oauth.issuer}/revoke")
        return authorization_server.create_endpoint_response("revocation", prepared)

    @router.post("/register", status_code=201)
    def register(request: Request, body: DynamicClientRegistrationIn) -> dict[str, Any]:
        """Register a bounded public PKCE client; this grants no Shortlist access."""
        _rate_limit_registration()
        if body.token_endpoint_auth_method != "none":
            raise HTTPException(status_code=400, detail="only public OAuth clients are supported")
        if set(body.grant_types) != {"authorization_code", "refresh_token"} or body.response_types != ["code"]:
            raise HTTPException(status_code=400, detail="unsupported OAuth client metadata")
        if any(not _safe_redirect_uri(uri) for uri in body.redirect_uris):
            raise HTTPException(status_code=400, detail="redirect URIs must be exact HTTPS or loopback HTTP URLs")
        current_repository = repository or _runtime(request).repository
        if current_repository.oauth_client_count() >= 1_000:
            raise HTTPException(status_code=429, detail="OAuth client registration capacity reached")
        client_id = f"client_{secrets.token_urlsafe(18)}"
        current_repository.register_oauth_client(
            client_id=client_id,
            client_name=body.client_name,
            redirect_uris=body.redirect_uris,
        )
        return {
            "client_id": client_id,
            "client_name": body.client_name,
            "redirect_uris": body.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_id_issued_at": int(time.time()),
        }

    return router


def create_assistant_auth_router() -> APIRouter:
    """Create static routes that resolve the lifespan-built runtime per request."""
    router = APIRouter()
    router.include_router(create_owner_grant_router())
    router.include_router(create_oauth_router())

    @router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def authorization_metadata(request: Request) -> dict[str, Any]:
        return authorization_server_metadata(_runtime(request).oauth)

    @router.get("/.well-known/oauth-authorization-server/{issuer_path:path}", include_in_schema=False)
    def authorization_metadata_for_issuer(request: Request, issuer_path: str) -> dict[str, Any]:
        oauth = _runtime(request).oauth
        if f"/{issuer_path}" != urlsplit(oauth.issuer).path:
            raise HTTPException(status_code=404, detail="authorization server metadata not found")
        return authorization_server_metadata(oauth)

    @router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    def resource_metadata(request: Request) -> dict[str, Any]:
        return protected_resource_metadata(_runtime(request).oauth)

    @router.get("/.well-known/oauth-protected-resource/{resource_path:path}", include_in_schema=False)
    def resource_metadata_for_path(request: Request, resource_path: str) -> dict[str, Any]:
        oauth = _runtime(request).oauth
        if f"/{resource_path}" != urlsplit(oauth.resource).path:
            raise HTTPException(status_code=404, detail="protected resource metadata not found")
        return protected_resource_metadata(oauth)

    return router


def authorization_server_metadata(oauth: OAuthService) -> dict[str, Any]:
    """Public RFC 8414-compatible metadata without instance-private state."""
    return {
        "issuer": oauth.issuer,
        "authorization_endpoint": f"{oauth.issuer}/authorize",
        "token_endpoint": f"{oauth.issuer}/token",
        "revocation_endpoint": f"{oauth.issuer}/revoke",
        "registration_endpoint": f"{oauth.issuer}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": sorted(capability.value for capability in ASSISTANT_CAPABILITIES),
    }


def protected_resource_metadata(oauth: OAuthService) -> dict[str, Any]:
    """Public RFC 9728-style metadata for the configured MCP audience."""
    return {
        "resource": oauth.resource,
        "authorization_servers": [oauth.issuer],
        "scopes_supported": sorted(capability.value for capability in ASSISTANT_CAPABILITIES),
        "bearer_methods_supported": ["header"],
    }


def _serialize_grant(grant) -> dict[str, Any]:
    return {
        "id": grant.grant_id,
        "owner_account_id": grant.owner_account_id,
        "client_id": grant.client_id,
        "name": grant.name,
        "preset": grant.preset.value,
        "capabilities": sorted(capability.value for capability in grant.capabilities),
        "constraints": grant.constraints.public_dict(),
        "requires_access_approval": grant.constraints.requires_access_approval,
        "revision": grant.revision,
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
    }


def _serialize_grant_summary(summary: GrantSummary) -> dict[str, Any]:
    serialized = _serialize_grant(summary.context)
    serialized.update(
        created_at=summary.created_at.isoformat(),
        updated_at=summary.updated_at.isoformat(),
        revoked_at=summary.revoked_at.isoformat() if summary.revoked_at else None,
        last_used_at=summary.last_used_at.isoformat() if summary.last_used_at else None,
        local_credential_count=summary.local_credential_count,
    )
    return serialized


_REGISTRATION_HITS: deque[float] = deque()


def _rate_limit_registration() -> None:
    now = time.monotonic()
    while _REGISTRATION_HITS and now - _REGISTRATION_HITS[0] > 60:
        _REGISTRATION_HITS.popleft()
    if len(_REGISTRATION_HITS) >= 30:
        raise HTTPException(status_code=429, detail="too many OAuth client registrations")
    _REGISTRATION_HITS.append(now)


def _safe_redirect_uri(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
            return False
        if parsed.scheme == "https":
            return True
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    except ValueError:
        return False
