from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.oauth import OAuthService
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.db.models import Base
from tests.assistant_oauth import authorize, exchange_code, issue_pair, pkce_challenge, refresh
from tests.db_helpers import disposing_engine

# Authlib stamps expiry from the wall clock, so the fixture time must be the real now.
NOW = datetime.now(UTC)
ISSUER = "https://shortlist.example/assistant/oauth"
RESOURCE = "https://shortlist.example/mcp"
REDIRECT = "http://127.0.0.1:49152/callback"


_challenge = pkce_challenge


@contextmanager
def _service(tmp_path: Path) -> Iterator[OAuthService]:
    with disposing_engine(
        create_engine(f"sqlite:///{tmp_path / 'oauth.db'}", connect_args={"check_same_thread": False})
    ) as engine:
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
        yield OAuthService(repository, issuer=ISSUER, resource=RESOURCE)


def test_authorization_code_requires_s256_and_exact_redirect_resource_and_scope(tmp_path) -> None:
    with _service(tmp_path) as service:
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


def _pair(service: OAuthService, verifier: str = "x" * 64, scopes: set[str] | None = None):
    grant = service.repository.find_grant_for_client("public-client")
    return issue_pair(
        service,
        owner_account_id=42,
        grant_id=grant.grant_id,
        client_id="public-client",
        redirect_uri=REDIRECT,
        scopes=scopes or {Capability.INSTANCE_READ.value},
        verifier=verifier,
    )


def test_code_is_one_use_and_tokens_are_bound_to_grant_client_resource_and_issuer(tmp_path) -> None:
    with _service(tmp_path) as service:
        verifier = "x" * 64
        grant = service.repository.find_grant_for_client("public-client")
        code = authorize(
            service,
            owner_account_id=42,
            grant_id=grant.grant_id,
            client_id="public-client",
            redirect_uri=REDIRECT,
            scopes={Capability.INSTANCE_READ.value},
            verifier=verifier,
        )
        exchange = {"code": code, "client_id": "public-client", "redirect_uri": REDIRECT, "verifier": verifier}

        tokens = exchange_code(service, **exchange)

        assert tokens.ok, tokens.body
        verified = service.verify_access_token(tokens.access_token, now=NOW)
        assert verified is not None
        assert verified.issuer == ISSUER
        assert verified.resource == RESOURCE
        assert verified.client_id == "public-client"
        assert tokens.body["token_type"].lower() == "bearer"
        assert tokens.body["resource"] == RESOURCE
        replay = exchange_code(service, **exchange)
        assert replay.status == 400
        assert replay.body["error"] == "invalid_grant"
        assert "access_token" not in replay.body
        assert service.verify_access_token(tokens.access_token, now=NOW) is not None


def test_code_is_refused_for_the_wrong_verifier_redirect_or_resource(tmp_path) -> None:
    with _service(tmp_path) as service:
        grant = service.repository.find_grant_for_client("public-client")
        for override in (
            {"verifier": "w" * 64},
            {"redirect_uri": f"{REDIRECT}/other"},
            {"resource": f"{RESOURCE}/other"},
        ):
            code = authorize(
                service,
                owner_account_id=42,
                grant_id=grant.grant_id,
                client_id="public-client",
                redirect_uri=REDIRECT,
                scopes={Capability.INSTANCE_READ.value},
                verifier="x" * 64,
            )
            arguments = {"code": code, "client_id": "public-client", "redirect_uri": REDIRECT, "verifier": "x" * 64}
            response = exchange_code(service, **{**arguments, **override})
            assert response.status == 400, override
            assert "access_token" not in response.body


def test_refresh_rotates_and_replay_revokes_the_family(tmp_path) -> None:
    with _service(tmp_path) as service:
        first = _pair(service, "z" * 64)

        second = refresh(service, refresh_token=first.refresh_token, client_id="public-client")
        assert second.ok, second.body
        later = NOW + timedelta(minutes=1)
        assert service.verify_access_token(second.access_token, now=later) is not None

        replay = refresh(service, refresh_token=first.refresh_token, client_id="public-client")
        assert replay.status == 400
        assert replay.body["error"] == "invalid_grant"
        assert service.verify_access_token(first.access_token, now=later) is None
        assert service.verify_access_token(second.access_token, now=later) is None
        # The family is dead, so the successor's refresh token no longer rotates either.
        assert not refresh(service, refresh_token=second.refresh_token, client_id="public-client").ok


@pytest.mark.parametrize("revoke_by_token", [False, True])
def test_family_revocation_between_rotation_and_issue_prevents_successor(
    tmp_path, monkeypatch, revoke_by_token
) -> None:
    with _service(tmp_path) as service:
        first = _pair(service, "r" * 64)
        old_refresh, old_access = first.refresh_token, first.access_token
        original = service.repository.save_oauth_token_pair
        attempted = []

        def interleaved(**kwargs):
            assert kwargs["previous_token_id"] is not None
            if revoke_by_token:
                assert service.revoke(old_refresh)
            else:
                # A simultaneous replay loses the rotation claim and revokes the whole family.
                assert not refresh(service, refresh_token=old_refresh, client_id="public-client").ok
            attempted.append(kwargs["raw_access"])
            return original(**kwargs)

        monkeypatch.setattr(service.repository, "save_oauth_token_pair", interleaved)
        raced = refresh(service, refresh_token=old_refresh, client_id="public-client")
        assert raced.status == 400
        assert "access_token" not in raced.body
        assert len(attempted) == 1
        for token in [old_access, *attempted]:
            assert service.verify_access_token(token, now=NOW + timedelta(seconds=2)) is None


def test_authlib_successor_save_rejects_a_revoked_family(tmp_path) -> None:
    from types import SimpleNamespace

    from authlib.oauth2.rfc6749 import InvalidGrantError

    from shortlist.server.assistant_auth.authlib_adapter import ASGIAuthorizationServer

    with _service(tmp_path) as service:
        first = _pair(service, "s" * 64)
        source = service.repository.get_refresh_token(first.refresh_token)
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
    with _service(tmp_path) as service:
        grant = service.repository.find_grant_for_client("public-client")
        tokens = _pair(service, "q" * 64)

        service.repository.revoke_grant(grant.grant_id, now=NOW + timedelta(seconds=1))

        assert service.verify_access_token(tokens.access_token, now=NOW + timedelta(seconds=1)) is None
        assert not refresh(service, refresh_token=tokens.refresh_token, client_id="public-client").ok


def test_an_approval_without_a_grant_does_not_burn_the_pending_consent_flow(tmp_path) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from shortlist.server.assistant_auth.routes import BrowserOwner, ConsentDecisionIn, create_oauth_router

    with _service(tmp_path) as service:
        flow_id, csrf = service.repository.create_consent_flow(
            owner_account_id=42,
            client_id="public-client",
            redirect_uri=REDIRECT,
            resource=RESOURCE,
            scope=Capability.INSTANCE_READ.value,
            client_state="state",
            code_challenge=_challenge("c" * 64),
            code_challenge_method="S256",
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
        token = csrf.take()
        decide = next(
            route.endpoint
            for route in create_oauth_router(service.repository, service).routes
            if route.path.endswith("/consent")
        )

        with pytest.raises(HTTPException) as refused:
            decide(
                SimpleNamespace(),
                ConsentDecisionIn(flow_id=flow_id, csrf_token=token, approved=True),
                BrowserOwner(account_id=42),
            )

        assert refused.value.status_code == 400
        flow = service.repository.consume_consent_flow(
            flow_id, owner_account_id=42, csrf_token=token, approved=False, grant_id=None, now=NOW
        )
        assert flow is not None, "the malformed approval consumed the flow"
