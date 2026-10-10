"""Drive the real Authlib authorization server, as the HTTP routes do, from a test.

Tests that need an authorization code or a token pair go through `ASGIAuthorizationServer` — the code
that runs in production — rather than a parallel implementation. Authlib stamps expiry from the wall
clock, so tests that use this should too.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from shortlist.server.assistant_auth.authlib_adapter import ASGIAuthorizationServer, PreparedOAuthRequest
from shortlist.server.assistant_auth.oauth import OAuthService
from shortlist.server.assistant_auth.routes import BrowserOwner


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """A `/token` answer: HTTP status plus the parsed JSON body."""

    status: int
    body: dict

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def access_token(self) -> str:
        return self.body["access_token"]

    @property
    def refresh_token(self) -> str:
        return self.body["refresh_token"]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize(
    oauth: OAuthService,
    *,
    owner_account_id: int,
    grant_id: str,
    client_id: str,
    redirect_uri: str,
    scopes: set[str],
    verifier: str,
    resource: str | None = None,
) -> str:
    """Approve a consent request for a grant and return the authorization code."""
    server = ASGIAuthorizationServer(oauth)
    prepared = PreparedOAuthRequest.authorization(
        canonical_url=f"{oauth.issuer}/authorize",
        client_id=client_id,
        redirect_uri=redirect_uri,
        resource=resource or oauth.resource,
        scope=" ".join(sorted(scopes)),
        state="test-state",
        code_challenge=pkce_challenge(verifier),
        code_challenge_method="S256",
    )
    consent = server.get_consent_grant(prepared, end_user=BrowserOwner(account_id=owner_account_id))
    grant_context = oauth.repository.get_grant_context(grant_id)
    assert grant_context is not None
    response = server.create_authorization_response(prepared, grant_user=grant_context, grant=consent)
    location = response.headers["location"]
    return parse_qs(urlsplit(location).query)["code"][0]


def token_request(oauth: OAuthService, **form: str) -> TokenResponse:
    """POST a form to the real token endpoint."""
    prepared = PreparedOAuthRequest(
        "POST",
        f"{oauth.issuer}/token",
        {"content-type": "application/x-www-form-urlencoded"},
        {},
        {key: [value] for key, value in form.items()},
    )
    response = ASGIAuthorizationServer(oauth).create_token_response(prepared)
    return TokenResponse(response.status_code, json.loads(response.body))


def exchange_code(
    oauth: OAuthService,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    resource: str | None = None,
) -> TokenResponse:
    return token_request(
        oauth,
        grant_type="authorization_code",
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        resource=resource or oauth.resource,
    )


def refresh(oauth: OAuthService, *, refresh_token: str, client_id: str) -> TokenResponse:
    return token_request(
        oauth,
        grant_type="refresh_token",
        refresh_token=refresh_token,
        client_id=client_id,
        resource=oauth.resource,
    )


def issue_pair(
    oauth: OAuthService,
    *,
    owner_account_id: int,
    grant_id: str,
    client_id: str,
    redirect_uri: str,
    scopes: set[str],
    verifier: str = "v" * 64,
) -> TokenResponse:
    """Run the whole code flow and return the resulting token pair."""
    code = authorize(
        oauth,
        owner_account_id=owner_account_id,
        grant_id=grant_id,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        verifier=verifier,
    )
    return exchange_code(oauth, code=code, client_id=client_id, redirect_uri=redirect_uri, verifier=verifier)
