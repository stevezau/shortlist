from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.oauth import OAuthService
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.db.models import Base

NOW = datetime(2026, 10, 5, tzinfo=UTC)
ISSUER = "https://shortlist.example/assistant/oauth"
RESOURCE = "https://shortlist.example/mcp"
REDIRECT = "http://127.0.0.1:49152/callback"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _service(tmp_path) -> OAuthService:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = AssistantAuthRepository(sessions, CredentialHasher(b"o" * 32))
    repository.register_oauth_client(
        client_id="public-client",
        client_name="Test client",
        redirect_uris=[REDIRECT],
        token_endpoint_auth_method="none",
        now=NOW,
    )
    repository.create_grant(
        owner_account_id=42,
        client_id="public-client",
        name="Test client",
        preset=GrantPreset.INSPECT,
        capabilities={Capability.INSTANCE_READ, Capability.CATALOG_READ},
        constraints=GrantConstraints(),
        now=NOW,
    )
    return OAuthService(repository, issuer=ISSUER, resource=RESOURCE)


def test_authorization_code_requires_s256_and_exact_redirect_resource_and_scope(tmp_path) -> None:
    service = _service(tmp_path)
    verifier = "v" * 64

    assert service.validate_authorization_request(
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        scopes={Capability.INSTANCE_READ.value},
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
    ).is_valid
    assert not service.validate_authorization_request(
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        scopes={Capability.INSTANCE_READ.value},
        code_challenge=verifier,
        code_challenge_method="plain",
    ).is_valid
    assert not service.validate_authorization_request(
        client_id="public-client",
        redirect_uri=f"{REDIRECT}/extra",
        resource=RESOURCE,
        scopes={Capability.INSTANCE_READ.value},
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
    ).is_valid
    assert not service.validate_authorization_request(
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        scopes={Capability.SECRETS_READ.value},
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
    ).is_valid
    assert not service.validate_authorization_request(
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=f"{RESOURCE}/other",
        scopes={Capability.INSTANCE_READ.value},
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
    ).is_valid


def test_code_is_one_use_and_tokens_are_bound_to_grant_client_resource_and_issuer(tmp_path) -> None:
    service = _service(tmp_path)
    verifier = "x" * 64
    code = service.issue_authorization_code(
        owner_account_id=42,
        grant_id=service.repository.find_grant_for_client("public-client").grant_id,
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        scopes={Capability.INSTANCE_READ.value},
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        now=NOW,
    ).take()

    tokens = service.exchange_code(
        code=code,
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        code_verifier=verifier,
        now=NOW,
    )

    access = tokens.access_token.take()
    refresh = tokens.refresh_token.take()
    verified = service.verify_access_token(access, now=NOW)
    assert verified is not None
    assert verified.issuer == ISSUER
    assert verified.resource == RESOURCE
    assert verified.client_id == "public-client"
    assert (
        service.exchange_code(
            code=code,
            client_id="public-client",
            redirect_uri=REDIRECT,
            resource=RESOURCE,
            code_verifier=verifier,
            now=NOW,
        )
        is None
    )
    assert refresh not in repr(tokens)


def test_refresh_rotates_and_replay_revokes_the_family(tmp_path) -> None:
    service = _service(tmp_path)
    verifier = "z" * 64
    code = service.issue_authorization_code(
        owner_account_id=42,
        grant_id=service.repository.find_grant_for_client("public-client").grant_id,
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        scopes={Capability.INSTANCE_READ.value},
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        now=NOW,
    ).take()
    first = service.exchange_code(
        code=code,
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        code_verifier=verifier,
        now=NOW,
    )
    old_refresh = first.refresh_token.take()
    old_access = first.access_token.take()

    second = service.refresh(
        refresh_token=old_refresh,
        client_id="public-client",
        resource=RESOURCE,
        now=NOW + timedelta(minutes=1),
    )
    assert second is not None
    new_access = second.access_token.take()
    assert service.verify_access_token(new_access, now=NOW + timedelta(minutes=1)) is not None

    assert (
        service.refresh(
            refresh_token=old_refresh,
            client_id="public-client",
            resource=RESOURCE,
            now=NOW + timedelta(minutes=2),
        )
        is None
    )
    assert service.verify_access_token(old_access, now=NOW + timedelta(minutes=2)) is None
    assert service.verify_access_token(new_access, now=NOW + timedelta(minutes=2)) is None


@pytest.mark.parametrize("revoke_by_token", [False, True])
def test_family_revocation_between_rotation_and_issue_prevents_successor(
    tmp_path, monkeypatch, revoke_by_token
) -> None:
    service = _service(tmp_path)
    grant = service.repository.find_grant_for_client("public-client")
    first = service._issue_pair(
        grant_id=grant.grant_id,
        client_id="public-client",
        resource=RESOURCE,
        scope=Capability.INSTANCE_READ.value,
        family_id="race-family",
        previous_token_id=None,
        now=NOW,
    )
    old_refresh, old_access = first.refresh_token.take(), first.access_token.take()
    original = service.repository.save_oauth_token_pair
    attempted = []

    def interleaved(**kwargs):
        assert kwargs["previous_token_id"] is not None
        if revoke_by_token:
            assert service.revoke(old_refresh, now=NOW + timedelta(seconds=1))
        else:
            # A simultaneous replay loses the rotation claim and revokes the whole family.
            assert (
                service.refresh(
                    refresh_token=old_refresh,
                    client_id="public-client",
                    resource=RESOURCE,
                    now=NOW + timedelta(seconds=1),
                )
                is None
            )
        attempted.append(kwargs["raw_access"])
        return original(**kwargs)

    monkeypatch.setattr(service.repository, "save_oauth_token_pair", interleaved)
    assert (
        service.refresh(
            refresh_token=old_refresh,
            client_id="public-client",
            resource=RESOURCE,
            now=NOW + timedelta(seconds=1),
        )
        is None
    )
    assert len(attempted) == 1
    for token in [old_access, *attempted]:
        assert service.verify_access_token(token, now=NOW + timedelta(seconds=2)) is None


def test_authlib_successor_save_rejects_a_revoked_family(tmp_path) -> None:
    from types import SimpleNamespace

    from authlib.oauth2.rfc6749 import InvalidGrantError

    from shortlist.server.assistant_auth.authlib_adapter import ASGIAuthorizationServer

    service = _service(tmp_path)
    first = service._issue_pair(
        grant_id=service.repository.find_grant_for_client("public-client").grant_id,
        client_id="public-client",
        resource=RESOURCE,
        scope=Capability.INSTANCE_READ.value,
        family_id="authlib-race-family",
        previous_token_id=None,
        now=NOW,
    )
    source = service.repository.get_refresh_token(first.refresh_token.take())
    assert service.repository.rotate_refresh_token(source.id, now=NOW)
    service.repository.revoke_refresh_family(source.refresh_family_id, now=NOW)
    request = SimpleNamespace(
        authorization_code=None, refresh_token=source, client=SimpleNamespace(client_id="public-client")
    )
    with pytest.raises(InvalidGrantError):
        ASGIAuthorizationServer(service).save_token(
            {"access_token": "refused-new-access", "refresh_token": "refused-new-refresh", "scope": source.scope},
            request,
        )
    assert service.verify_access_token("refused-new-access", now=NOW) is None


def test_grant_revocation_immediately_invalidates_access_and_refresh(tmp_path) -> None:
    service = _service(tmp_path)
    verifier = "q" * 64
    grant = service.repository.find_grant_for_client("public-client")
    code = service.issue_authorization_code(
        owner_account_id=42,
        grant_id=grant.grant_id,
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        scopes={Capability.INSTANCE_READ.value},
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        now=NOW,
    ).take()
    tokens = service.exchange_code(
        code=code,
        client_id="public-client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        code_verifier=verifier,
        now=NOW,
    )
    access = tokens.access_token.take()
    refresh = tokens.refresh_token.take()

    service.repository.revoke_grant(grant.grant_id, now=NOW + timedelta(seconds=1))

    assert service.verify_access_token(access, now=NOW + timedelta(seconds=1)) is None
    assert (
        service.refresh(
            refresh_token=refresh,
            client_id="public-client",
            resource=RESOURCE,
            now=NOW + timedelta(seconds=1),
        )
        is None
    )
