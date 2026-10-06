"""MCP SDK token verification and per-request grant resolution."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .credentials import as_utc
from .oauth import OAuthService
from .policy import AuthorizationDenied
from .repository import AssistantAuthRepository
from .types import GrantContext


class AssistantTokenVerifier(TokenVerifier):
    """Adapt persistent opaque OAuth tokens to MCP SDK ``TokenVerifier``."""

    def __init__(self, oauth: OAuthService) -> None:
        self.oauth = oauth

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return MCP token facts only after every server-side binding passes."""
        verified = self.oauth.verify_access_token(token)
        if verified is None:
            local_grant = self.oauth.repository.authenticate_local_credential(token)
            if local_grant is None:
                return None
            return AccessToken(
                token=token,
                client_id=local_grant.client_id,
                scopes=sorted(capability.value for capability in local_grant.capabilities),
                expires_at=int(as_utc(local_grant.expires_at).timestamp()) if local_grant.expires_at else None,
                resource=self.oauth.resource,
                subject=str(local_grant.owner_account_id),
                claims={
                    "iss": self.oauth.issuer,
                    "grant_id": local_grant.grant_id,
                    "grant_revision": local_grant.revision,
                    "credential_kind": "local",
                },
            )
        return AccessToken(
            token=token,
            client_id=verified.client_id,
            scopes=sorted(verified.scopes),
            expires_at=int(verified.expires_at.timestamp()),
            resource=verified.resource,
            subject=str(verified.grant.owner_account_id),
            claims={
                "iss": verified.issuer,
                "grant_id": verified.grant.grant_id,
                "grant_revision": verified.grant.revision,
            },
        )


def resolve_mcp_principal(
    repository: AssistantAuthRepository,
    *,
    now: datetime | None = None,
) -> GrantContext:
    """Resolve and revalidate the current request's assistant principal.

    The MCP auth context is a context variable set by the SDK middleware for
    one HTTP request. This function never reads mutable application-global
    identity state.

    Raises:
        AuthorizationDenied: No valid current assistant grant is present.
    """
    access_token = get_access_token()
    if access_token is None or not isinstance(access_token.claims, dict):
        raise AuthorizationDenied("authenticated assistant context is missing")
    grant_id = access_token.claims.get("grant_id")
    grant_revision = access_token.claims.get("grant_revision")
    if not isinstance(grant_id, str) or not isinstance(grant_revision, int):
        raise AuthorizationDenied("authenticated assistant context is invalid")
    principal = repository.get_grant_context(grant_id, now=now or datetime.now(UTC))
    if principal is None:
        raise AuthorizationDenied("assistant grant is revoked or expired")
    if (
        principal.revision != grant_revision
        or principal.client_id != access_token.client_id
        or str(principal.owner_account_id) != access_token.subject
    ):
        raise AuthorizationDenied("assistant grant changed; reconnect before continuing")
    token_scopes = frozenset(access_token.scopes)
    scoped_capabilities = frozenset(
        capability for capability in principal.capabilities if capability.value in token_scopes
    )
    return replace(principal, capabilities=scoped_capabilities)
