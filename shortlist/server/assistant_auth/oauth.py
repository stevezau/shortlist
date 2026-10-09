"""OAuth request validation, access-token verification and revocation.

The code, refresh and token endpoints themselves are Authlib grants in `authlib_adapter.py`; this
service holds the shared issuer/resource bindings they and the MCP verifier read.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .repository import AssistantAuthRepository
from .types import ASSISTANT_CAPABILITIES, Capability, VerifiedOAuthToken


@dataclass(frozen=True, slots=True)
class OAuthValidation:
    """Safe validation result for a browser consent screen."""

    is_valid: bool
    error: str | None = None


class OAuthService:
    """Persistent OAuth authority shared by HTTP routes and MCP verification."""

    authorization_code_ttl = timedelta(minutes=5)
    access_token_ttl = timedelta(minutes=15)
    refresh_token_ttl = timedelta(days=30)

    def __init__(self, repository: AssistantAuthRepository, *, issuer: str, resource: str) -> None:
        self.repository = repository
        self.issuer = issuer.rstrip("/")
        self.resource = resource

    def validate_authorization_request(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        resource: str | None,
        scopes: set[str],
        code_challenge: str | None,
        code_challenge_method: str | None,
    ) -> OAuthValidation:
        """Validate all client-controlled authorization request bindings."""
        client = self.repository.get_oauth_client(client_id)
        if client is None:
            return OAuthValidation(False, "invalid_client")
        if redirect_uri not in client.redirect_uris:
            return OAuthValidation(False, "invalid_redirect_uri")
        if resource != self.resource:
            return OAuthValidation(False, "invalid_target")
        if not code_challenge or code_challenge_method != "S256" or not _valid_pkce_value(code_challenge):
            return OAuthValidation(False, "invalid_code_challenge")
        try:
            requested = {Capability(scope) for scope in scopes}
        except ValueError:
            return OAuthValidation(False, "invalid_scope")
        if not requested or not ASSISTANT_CAPABILITIES.issuperset(requested):
            return OAuthValidation(False, "invalid_scope")
        return OAuthValidation(True)

    def verify_access_token(self, raw_token: str, *, now: datetime | None = None) -> VerifiedOAuthToken | None:
        """Verify issuer, audience, lifetime, scopes, and current grant state."""
        token = self.repository.verify_access_token(raw_token, now=now or datetime.now(UTC))
        if token is None or token.issuer != self.issuer or token.resource != self.resource:
            return None
        if not token.grant.capabilities.issuperset(Capability(scope) for scope in token.scopes):
            return None
        return token

    def revoke(self, raw_token: str, *, token_type_hint: str | None = None, now: datetime | None = None) -> bool:
        """Revoke an access credential or a refresh token family."""
        return self.repository.revoke_oauth_token(
            raw_token, token_type_hint=token_type_hint, now=now or datetime.now(UTC)
        )


def _valid_pkce_value(value: str) -> bool:
    return 43 <= len(value) <= 128 and all(character.isalnum() or character in "-._~" for character in value)


def _opaque_family_id() -> str:
    return f"fam_{secrets.token_urlsafe(18)}"
