"""SQLite credential lifetimes keep their UTC meaning at the MCP SDK boundary."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import HTTPConnection

from shortlist.server.assistant_auth import GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.oauth import OAuthService
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.assistant_auth.verifier import AssistantTokenVerifier
from shortlist.server.db.models import Base


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="process timezone switching requires tzset")
@pytest.mark.parametrize("timezone", ["Australia/Sydney", "America/Los_Angeles"])
@pytest.mark.parametrize("credential_kind", ["local_grant", "local_credential", "oauth"])
@pytest.mark.parametrize("expired", [False, True], ids=["future", "expired"])
def test_sqlite_expiry_keeps_utc_meaning_through_sdk_guard(tmp_path, monkeypatch, timezone, credential_kind, expired):
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    repository = AssistantAuthRepository(sessionmaker(bind=engine), CredentialHasher(b"t" * 32))
    resource = "https://shortlist.example/mcp"
    issuer = "https://shortlist.example/assistant/oauth"
    verifier = AssistantTokenVerifier(OAuthService(repository, issuer=issuer, resource=resource))
    now = datetime.now(UTC)
    issued_at = now - timedelta(minutes=20)
    expires_at = now + timedelta(minutes=-1 if expired else 10)
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="expiry-test",
        name="Expiry test",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(),
        expires_at=expires_at if credential_kind == "local_grant" else None,
        now=issued_at,
    )
    if credential_kind == "oauth":
        raw = repository.hasher.issue("access").take()
        repository.save_oauth_token_pair(
            grant_id=grant.grant_id,
            client_id=grant.client_id,
            issuer=issuer,
            resource=resource,
            scope="instance.read",
            raw_access=raw,
            access_expires_at=expires_at,
            raw_refresh=repository.hasher.issue("refresh").take(),
            refresh_expires_at=now + timedelta(days=1),
            refresh_family_id="expiry-test-family",
            previous_token_id=None,
            now=issued_at,
        )
    else:
        raw = repository.issue_local_credential(
            grant.grant_id,
            expires_at=expires_at if credential_kind == "local_credential" else None,
            now=issued_at,
        ).take()
    connection = HTTPConnection({"type": "http", "headers": [(b"authorization", f"Bearer {raw}".encode())]})
    try:
        with monkeypatch.context() as process_timezone:
            process_timezone.setenv("TZ", timezone)
            time.tzset()
            try:
                token = asyncio.run(verifier.verify_token(raw))
                authenticated = asyncio.run(BearerAuthBackend(verifier).authenticate(connection))
                if expired:
                    assert token is None
                    assert authenticated is None
                else:
                    assert token is not None
                    assert authenticated is not None
                    if credential_kind != "local_credential":
                        actual_expiry = token.expires_at
                        assert actual_expiry == int(expires_at.timestamp())
            finally:
                process_timezone.undo()
                time.tzset()
    finally:
        engine.dispose()
