"""OAuth authorization-code, PKCE, refresh rotation, and token verification."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .credentials import IssuedSecret, as_utc
from .repository import AssistantAuthRepository, OAuthTokenIssuanceDenied
from .types import ASSISTANT_CAPABILITIES, Capability, VerifiedOAuthToken


@dataclass(frozen=True, slots=True)
class OAuthValidation:
    """Safe validation result for a browser consent screen."""

    is_valid: bool
    error: str | None = None


@dataclass(slots=True)
class OAuthTokenPair:
    """Fresh token response; credential bodies are one-time values."""

    access_token: IssuedSecret
    refresh_token: IssuedSecret
    token_type: str
    expires_in: int
    scope: str
    resource: str

    def __repr__(self) -> str:
        return (
            "OAuthTokenPair(access_token=<redacted>, refresh_token=<redacted>, "
            f"token_type={self.token_type!r}, expires_in={self.expires_in!r}, "
            f"scope={self.scope!r}, resource={self.resource!r})"
        )


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

    def issue_authorization_code(
        self,
        *,
        owner_account_id: int,
        grant_id: str,
        client_id: str,
        redirect_uri: str,
        resource: str,
        scopes: set[str],
        code_challenge: str,
        code_challenge_method: str,
        now: datetime | None = None,
    ) -> IssuedSecret:
        """Issue a short-lived code after browser owner consent."""
        timestamp = now or datetime.now(UTC)
        validation = self.validate_authorization_request(
            client_id=client_id,
            redirect_uri=redirect_uri,
            resource=resource,
            scopes=scopes,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        if not validation.is_valid:
            raise ValueError(validation.error)
        grant = self.repository.get_grant_context(grant_id, now=timestamp)
        if grant is None or grant.owner_account_id != owner_account_id or grant.client_id != client_id:
            raise ValueError("grant does not match owner and OAuth client")
        requested = {Capability(scope) for scope in scopes}
        if not grant.capabilities.issuperset(requested):
            raise ValueError("invalid_scope")

        raw = self.repository.hasher.issue("shlc").take()
        self.repository.save_authorization_code(
            raw_code=raw,
            grant_id=grant_id,
            owner_account_id=owner_account_id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            resource=resource,
            scope=" ".join(sorted(scopes)),
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=timestamp + self.authorization_code_ttl,
            now=timestamp,
        )
        return IssuedSecret(raw)

    def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        resource: str | None,
        code_verifier: str,
        now: datetime | None = None,
    ) -> OAuthTokenPair | None:
        """Redeem an exact-bound code once and issue a rotating token pair."""
        timestamp = now or datetime.now(UTC)
        stored = self.repository.get_authorization_code(code)
        if stored is None or as_utc(stored.expires_at) <= timestamp:
            return None
        if stored.client_id != client_id or stored.redirect_uri != redirect_uri or stored.resource != resource:
            return None
        if stored.code_challenge_method != "S256" or not _verify_s256(code_verifier, stored.code_challenge):
            return None
        grant = self.repository.get_grant_context(stored.grant_id, now=timestamp)
        if grant is None or grant.client_id != client_id:
            return None
        if not grant.capabilities.issuperset(Capability(scope) for scope in stored.scope.split()):
            return None
        if not self.repository.consume_authorization_code(stored.id, now=timestamp):
            return None
        return self._issue_pair(
            grant_id=stored.grant_id,
            client_id=client_id,
            resource=stored.resource,
            scope=stored.scope,
            family_id=_opaque_family_id(),
            previous_token_id=None,
            now=timestamp,
        )

    def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        resource: str | None,
        scopes: set[str] | None = None,
        now: datetime | None = None,
    ) -> OAuthTokenPair | None:
        """Rotate a refresh token; replay revokes its entire token family."""
        timestamp = now or datetime.now(UTC)
        stored = self.repository.get_refresh_token(refresh_token)
        if stored is None:
            return None
        if stored.refresh_revoked_at is not None:
            self.repository.revoke_refresh_family(stored.refresh_family_id, now=timestamp)
            return None
        if as_utc(stored.refresh_expires_at) <= timestamp:
            return None
        if stored.client_id != client_id or stored.resource != resource or stored.issuer != self.issuer:
            return None
        original_scopes = set(stored.scope.split())
        requested_scopes = original_scopes if scopes is None else set(scopes)
        if not requested_scopes or not original_scopes.issuperset(requested_scopes):
            return None
        grant = self.repository.get_grant_context(stored.grant_id, now=timestamp)
        if grant is None or grant.client_id != client_id:
            return None
        if not grant.capabilities.issuperset(Capability(scope) for scope in requested_scopes):
            return None
        if not self.repository.rotate_refresh_token(stored.id, now=timestamp):
            self.repository.revoke_refresh_family(stored.refresh_family_id, now=timestamp)
            return None
        return self._issue_pair(
            grant_id=stored.grant_id,
            client_id=stored.client_id,
            resource=stored.resource,
            scope=" ".join(sorted(requested_scopes)),
            family_id=stored.refresh_family_id,
            previous_token_id=stored.id,
            now=timestamp,
        )

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

    def _issue_pair(
        self,
        *,
        grant_id: str,
        client_id: str,
        resource: str,
        scope: str,
        family_id: str,
        previous_token_id: int | None,
        now: datetime,
    ) -> OAuthTokenPair | None:
        raw_access = self.repository.hasher.issue("shlo").take()
        raw_refresh = self.repository.hasher.issue("shlr").take()
        try:
            self.repository.save_oauth_token_pair(
                grant_id=grant_id,
                client_id=client_id,
                issuer=self.issuer,
                resource=resource,
                scope=scope,
                raw_access=raw_access,
                access_expires_at=now + self.access_token_ttl,
                raw_refresh=raw_refresh,
                refresh_expires_at=now + self.refresh_token_ttl,
                refresh_family_id=family_id,
                previous_token_id=previous_token_id,
                now=now,
            )
        except OAuthTokenIssuanceDenied:
            return None
        return OAuthTokenPair(
            access_token=IssuedSecret(raw_access),
            refresh_token=IssuedSecret(raw_refresh),
            token_type="Bearer",
            expires_in=int(self.access_token_ttl.total_seconds()),
            scope=scope,
            resource=resource,
        )


def _valid_pkce_value(value: str) -> bool:
    return 43 <= len(value) <= 128 and all(character.isalnum() or character in "-._~" for character in value)


def _verify_s256(verifier: str, expected: str) -> bool:
    if not _valid_pkce_value(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(actual, expected)


def _opaque_family_id() -> str:
    return f"fam_{secrets.token_urlsafe(18)}"
