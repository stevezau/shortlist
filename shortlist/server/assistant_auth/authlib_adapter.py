"""Authlib authorization-server core adapted to Starlette/FastAPI requests."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlencode

from authlib.oauth2.rfc6749 import (
    AuthorizationServer,
    InvalidGrantError,
    InvalidRequestError,
    InvalidScopeError,
    JsonPayload,
    JsonRequest,
    OAuth2Error,
    OAuth2Payload,
    OAuth2Request,
)
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc6750 import BearerTokenGenerator
from authlib.oauth2.rfc7009 import RevocationEndpoint
from authlib.oauth2.rfc7636 import CodeChallenge
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .credentials import as_utc
from .models import AssistantOAuthClient, AssistantOAuthCode, AssistantOAuthToken
from .oauth import OAuthService, _opaque_family_id
from .repository import OAuthTokenIssuanceDenied
from .types import Capability, GrantContext


class InvalidTargetError(OAuth2Error):
    """The RFC 8707 resource indicator is absent or does not match."""

    error = "invalid_target"


@dataclass(frozen=True, slots=True)
class PreparedOAuthRequest:
    """Bounded request data collected before entering Authlib's synchronous core."""

    method: str
    uri: str
    headers: Mapping[str, str]
    query: dict[str, list[str]]
    form: dict[str, list[str]]

    @classmethod
    async def from_starlette(cls, request: Request, *, canonical_url: str) -> PreparedOAuthRequest:
        body = await request.body()
        if len(body) > 16_384:
            raise InvalidRequestError("OAuth form is too large")
        form: dict[str, list[str]] = {}
        if body:
            from urllib.parse import parse_qs

            try:
                form = parse_qs(body.decode("ascii"), keep_blank_values=True, max_num_fields=20)
            except (UnicodeDecodeError, ValueError) as error:
                raise InvalidRequestError("Invalid OAuth form") from error
        query: defaultdict[str, list[str]] = defaultdict(list)
        for key, value in request.query_params.multi_items():
            query[key].append(value)
        uri = canonical_url
        if query:
            uri = f"{uri}?{urlencode([(key, value) for key, values in query.items() for value in values])}"
        return cls(request.method, uri, dict(request.headers), dict(query), form)

    @classmethod
    def authorization(
        cls,
        *,
        canonical_url: str,
        client_id: str,
        redirect_uri: str,
        resource: str,
        scope: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> PreparedOAuthRequest:
        query = {
            "response_type": ["code"],
            "client_id": [client_id],
            "redirect_uri": [redirect_uri],
            "resource": [resource],
            "scope": [scope],
            "state": [state],
            "code_challenge": [code_challenge],
            "code_challenge_method": [code_challenge_method],
        }
        encoded = urlencode([(key, value) for key, values in query.items() for value in values])
        return cls("GET", f"{canonical_url}?{encoded}", {}, query, {})


class _Payload(OAuth2Payload):
    def __init__(self, values: dict[str, list[str]]) -> None:
        self._values = values

    @property
    def data(self) -> dict[str, str]:
        return {key: values[0] for key, values in self._values.items() if values}

    @property
    def datalist(self) -> defaultdict[str, list[str]]:
        return defaultdict(list, {key: list(values) for key, values in self._values.items()})


class _OAuth2Request(OAuth2Request):
    def __init__(self, request: PreparedOAuthRequest) -> None:
        super().__init__(method=request.method, uri=request.uri, headers=request.headers)
        self._request = request
        merged = defaultdict(list, request.query)
        for key, values in request.form.items():
            merged[key].extend(values)
        self.payload = _Payload(dict(merged))

    @property
    def args(self) -> dict[str, str]:
        return {key: values[0] for key, values in self._request.query.items() if values}

    @property
    def form(self) -> dict[str, str]:
        return {key: values[0] for key, values in self._request.form.items() if values}


class _JsonPayload(JsonPayload):
    @property
    def data(self) -> dict:
        return {}


class _JsonRequest(JsonRequest):
    def __init__(self, request: PreparedOAuthRequest) -> None:
        super().__init__(request.method, request.uri, request.headers)
        self.payload = _JsonPayload()


class S256OnlyCodeChallenge(CodeChallenge):
    """Require an explicit S256 method; Authlib also supports ``plain``."""

    DEFAULT_CODE_CHALLENGE_METHOD = "S256"
    SUPPORTED_CODE_CHALLENGE_METHOD: ClassVar = ["S256"]

    def validate_code_challenge(self, grant, redirect_uri) -> None:
        request = grant.request
        if request.payload.data.get("code_challenge_method") != "S256":
            raise InvalidRequestError("'code_challenge_method' must be S256")
        if not request.payload.data.get("code_challenge"):
            raise InvalidRequestError("Missing 'code_challenge'")
        super().validate_code_challenge(grant, redirect_uri)


class ShortlistAuthorizationCodeGrant(AuthorizationCodeGrant):
    """Authlib code grant backed by Shortlist's persistent hashed store."""

    TOKEN_ENDPOINT_AUTH_METHODS: ClassVar = ["none"]

    @property
    def oauth(self) -> OAuthService:
        return self.server.oauth

    def validate_authorization_request(self) -> str:
        redirect_uri = super().validate_authorization_request()
        data = self.request.payload.data
        if data.get("resource") != self.oauth.resource:
            raise InvalidTargetError()
        if not data.get("state"):
            raise InvalidRequestError("Missing 'state'")
        try:
            scopes = {Capability(scope) for scope in (self.request.scope or "").split()}
        except ValueError as error:
            raise InvalidScopeError() from error
        if not scopes:
            raise InvalidScopeError()
        return redirect_uri

    def save_authorization_code(self, code: str, request: OAuth2Request) -> None:
        grant: GrantContext = request.user
        requested = {Capability(scope) for scope in request.scope.split()}
        if not grant.capabilities.issuperset(requested) or grant.client_id != request.client.client_id:
            raise InvalidScopeError()
        now = datetime.now(UTC)
        self.oauth.repository.save_authorization_code(
            raw_code=code,
            grant_id=grant.grant_id,
            owner_account_id=grant.owner_account_id,
            client_id=request.client.client_id,
            redirect_uri=request.payload.redirect_uri,
            resource=request.payload.data["resource"],
            scope=request.scope,
            code_challenge=request.payload.data["code_challenge"],
            code_challenge_method=request.payload.data["code_challenge_method"],
            expires_at=now + self.oauth.authorization_code_ttl,
            now=now,
        )

    def query_authorization_code(self, code: str, client: AssistantOAuthClient) -> AssistantOAuthCode | None:
        stored = self.oauth.repository.get_authorization_code(code)
        if stored is None or stored.client_id != client.client_id or as_utc(stored.expires_at) <= datetime.now(UTC):
            return None
        return stored

    def validate_token_request(self) -> None:
        super().validate_token_request()
        stored = self.request.authorization_code
        if self.request.form.get("resource") != stored.resource:
            raise InvalidTargetError()
        if not self.oauth.repository.consume_authorization_code(stored.id, now=datetime.now(UTC)):
            raise InvalidGrantError("Authorization code was already used")

    def delete_authorization_code(self, authorization_code: AssistantOAuthCode) -> None:
        return None

    def authenticate_user(self, authorization_code: AssistantOAuthCode) -> GrantContext | None:
        return self.oauth.repository.get_grant_context(authorization_code.grant_id)


class ShortlistRefreshTokenGrant(RefreshTokenGrant):
    """Authlib refresh grant with rotation and replay-family revocation."""

    TOKEN_ENDPOINT_AUTH_METHODS: ClassVar = ["none"]
    INCLUDE_NEW_REFRESH_TOKEN = True

    @property
    def oauth(self) -> OAuthService:
        return self.server.oauth

    def authenticate_refresh_token(self, refresh_token: str) -> AssistantOAuthToken | None:
        stored = self.oauth.repository.get_refresh_token(refresh_token)
        now = datetime.now(UTC)
        if stored is None:
            return None
        if stored.refresh_revoked_at is not None:
            self.oauth.repository.revoke_refresh_family(stored.refresh_family_id, now=now)
            return None
        if as_utc(stored.refresh_expires_at) <= now or stored.issuer != self.oauth.issuer:
            return None
        return stored

    def validate_token_request(self) -> None:
        super().validate_token_request()
        stored = self.request.refresh_token
        if self.request.form.get("resource") != stored.resource:
            raise InvalidTargetError()
        grant = self.oauth.repository.get_grant_context(stored.grant_id)
        requested = set((self.request.scope or stored.scope).split())
        if grant is None or not grant.capabilities.issuperset(Capability(scope) for scope in requested):
            raise InvalidScopeError()
        if not self.oauth.repository.rotate_refresh_token(stored.id, now=datetime.now(UTC)):
            self.oauth.repository.revoke_refresh_family(stored.refresh_family_id, now=datetime.now(UTC))
            raise InvalidGrantError("Refresh token was already used")

    def authenticate_user(self, refresh_token: AssistantOAuthToken) -> GrantContext | None:
        return self.oauth.repository.get_grant_context(refresh_token.grant_id)

    def revoke_old_credential(self, refresh_token: AssistantOAuthToken) -> None:
        return None


class ShortlistRevocationEndpoint(RevocationEndpoint):
    """Authlib RFC 7009 endpoint for public clients."""

    CLIENT_AUTH_METHODS: ClassVar = ["none"]

    @property
    def oauth(self) -> OAuthService:
        return self.server.oauth

    def query_token(self, token_string: str, token_type_hint: str | None) -> AssistantOAuthToken | None:
        return self.oauth.repository.find_oauth_token(token_string)

    def revoke_token(self, token: AssistantOAuthToken, request: OAuth2Request) -> None:
        raw = request.form.get("token")
        self.oauth.revoke(raw, token_type_hint=request.form.get("token_type_hint"))


class ASGIAuthorizationServer(AuthorizationServer):
    """Project-owned thin ASGI adapter around Authlib's framework-neutral core."""

    def __init__(self, oauth: OAuthService) -> None:
        super().__init__(scopes_supported=[capability.value for capability in Capability])
        self.oauth = oauth
        generator = BearerTokenGenerator(
            access_token_generator=lambda *args, **kwargs: oauth.repository.hasher.issue("shlo").take(),
            refresh_token_generator=lambda *args, **kwargs: oauth.repository.hasher.issue("shlr").take(),
            expires_generator=lambda client, grant_type: int(oauth.access_token_ttl.total_seconds()),
        )
        self.register_token_generator("default", generator)
        self.register_grant(ShortlistAuthorizationCodeGrant, [S256OnlyCodeChallenge(required=True)])
        self.register_grant(ShortlistRefreshTokenGrant)
        self.register_endpoint(ShortlistRevocationEndpoint)

    def query_client(self, client_id: str) -> AssistantOAuthClient | None:
        return self.oauth.repository.get_oauth_client(client_id)

    def save_token(self, token: dict, request: OAuth2Request) -> None:
        now = datetime.now(UTC)
        if request.authorization_code is not None:
            source = request.authorization_code
            family_id = _opaque_family_id()
            previous_token_id = None
        else:
            source = request.refresh_token
            family_id = source.refresh_family_id
            previous_token_id = source.id
        resource = source.resource
        token["resource"] = resource
        try:
            self.oauth.repository.save_oauth_token_pair(
                grant_id=source.grant_id,
                client_id=request.client.client_id,
                issuer=self.oauth.issuer,
                resource=resource,
                scope=token.get("scope") or source.scope,
                raw_access=token["access_token"],
                access_expires_at=now + self.oauth.access_token_ttl,
                raw_refresh=token["refresh_token"],
                refresh_expires_at=now + self.oauth.refresh_token_ttl,
                refresh_family_id=family_id,
                previous_token_id=previous_token_id,
                now=now,
            )
        except OAuthTokenIssuanceDenied:
            raise InvalidGrantError("The grant or refresh family no longer permits token issuance.") from None

    def create_oauth2_request(self, request: PreparedOAuthRequest) -> OAuth2Request:
        return _OAuth2Request(request)

    def create_json_request(self, request: PreparedOAuthRequest) -> JsonRequest:
        return _JsonRequest(request)

    def handle_response(self, status: int, body, headers) -> Response:
        header_map = dict(headers)
        if isinstance(body, dict):
            return JSONResponse(body, status_code=status, headers=header_map)
        if isinstance(body, str) and header_map.get("Content-Type") == "application/json":
            return JSONResponse(json.loads(body), status_code=status, headers=header_map)
        return Response(body or b"", status_code=status, headers=header_map)

    def send_signal(self, name: str, *args, **kwargs) -> None:
        return None
