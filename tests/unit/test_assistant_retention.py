"""Assistant working state is pruned once it cannot matter, and unused OAuth clients expire."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.assistant.retention import UNAPPLIED_CHANGE_RETENTION, prune_assistant_state
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.models import (
    AssistantConsentFlow,
    AssistantOAuthClient,
    AssistantOAuthCode,
    AssistantOAuthRevokedFamily,
    AssistantOAuthToken,
)
from shortlist.server.assistant_auth.oauth import OAuthService
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.assistant_auth.retention import REVOKED_FAMILY_TTL, UNUSED_CLIENT_TTL
from shortlist.server.services import jobs
from tests.db_helpers import create_schema, disposing_engine

NOW = datetime.now(UTC)
REDIRECT = "http://127.0.0.1:49152/callback"
RESOURCE = "https://shortlist.example/mcp"


@contextmanager
def _env(tmp_path) -> Iterator[SimpleNamespace]:
    with disposing_engine(
        create_engine(f"sqlite:///{tmp_path / 'retention.db'}", connect_args={"check_same_thread": False})
    ) as engine:
        create_schema(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"r" * 32))
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="used-client",
            name="Used",
            preset=GrantPreset.INSPECT,
            capabilities={Capability.INSTANCE_READ},
            constraints=GrantConstraints(),
            now=NOW,
        )
        yield SimpleNamespace(sessions=sessions, repository=repository, grant=grant)


def _client(repository: AssistantAuthRepository, client_id: str, *, created: datetime) -> None:
    repository.register_oauth_client(client_id=client_id, client_name=client_id, redirect_uris=[REDIRECT], now=created)


def _token(env, *, suffix: str, client_id: str, issued: datetime, refresh_expires: datetime, **extra) -> None:
    with env.sessions() as session:
        session.add(
            AssistantOAuthToken(
                grant_id=env.grant.grant_id,
                client_id=client_id,
                issuer="https://shortlist.example/oauth",
                resource=RESOURCE,
                scope="instance.read",
                access_digest=f"a{suffix}".ljust(64, "0"),
                access_prefix="shlo",
                access_expires_at=issued + timedelta(minutes=15),
                refresh_digest=f"r{suffix}".ljust(64, "0"),
                refresh_prefix="shlr",
                refresh_expires_at=refresh_expires,
                refresh_family_id=f"fam_{suffix}",
                issued_at=issued,
                **extra,
            )
        )
        session.commit()


def _count(env, model) -> int:
    with env.sessions() as session:
        return session.query(model).count()


def test_expired_oauth_state_is_pruned_and_live_state_is_kept(tmp_path) -> None:
    with _env(tmp_path) as env:
        _client(env.repository, "used-client", created=NOW - timedelta(days=90))
        with env.sessions() as session:
            for suffix, expires in (("dead", NOW - timedelta(minutes=1)), ("live", NOW + timedelta(minutes=4))):
                session.add(
                    AssistantOAuthCode(
                        code_digest=f"c{suffix}".ljust(64, "0"),
                        grant_id=env.grant.grant_id,
                        owner_account_id=42,
                        client_id="used-client",
                        redirect_uri=REDIRECT,
                        resource=RESOURCE,
                        scope="instance.read",
                        code_challenge="c" * 43,
                        code_challenge_method="S256",
                        created_at=NOW - timedelta(minutes=10),
                        expires_at=expires,
                    )
                )
                session.add(
                    AssistantConsentFlow(
                        id=f"consent_{suffix}",
                        owner_account_id=42,
                        client_id="used-client",
                        redirect_uri=REDIRECT,
                        resource=RESOURCE,
                        scope="instance.read",
                        client_state="s",
                        code_challenge="c" * 43,
                        code_challenge_method="S256",
                        csrf_digest="d" * 64,
                        created_at=NOW - timedelta(minutes=20),
                        expires_at=expires,
                    )
                )
            session.commit()

        with env.sessions() as session:
            counts = prune_assistant_state(session, now=NOW)
            session.commit()

        assert counts["oauth_codes"] == 1
        assert counts["consent_flows"] == 1
        with env.sessions() as session:
            assert session.query(AssistantOAuthCode).one().code_digest.startswith("clive")
            assert session.query(AssistantConsentFlow).one().id == "consent_live"


def test_a_rotated_token_survives_until_its_refresh_expiry_so_replay_still_revokes_the_family(tmp_path) -> None:
    with _env(tmp_path) as env:
        _client(env.repository, "used-client", created=NOW - timedelta(days=90))
        long_ago = NOW - timedelta(days=2)
        # Rotated two days ago: access is long expired, but a replay of its refresh token must still be seen.
        _token(
            env,
            suffix="rotated",
            client_id="used-client",
            issued=long_ago,
            refresh_expires=NOW + timedelta(days=28),
            refresh_revoked_at=long_ago,
        )
        # Fully expired and revoked: nothing can use or replay it.
        _token(
            env,
            suffix="spent",
            client_id="used-client",
            issued=NOW - timedelta(days=40),
            refresh_expires=NOW - timedelta(days=10),
            refresh_revoked_at=NOW - timedelta(days=39),
        )
        # Fully expired and never revoked: the last token of a family nobody used for 30 days.
        _token(
            env,
            suffix="idle",
            client_id="used-client",
            issued=NOW - timedelta(days=40),
            refresh_expires=NOW - timedelta(days=10),
        )
        _token(env, suffix="live", client_id="used-client", issued=NOW, refresh_expires=NOW + timedelta(days=30))

        with env.sessions() as session:
            assert prune_assistant_state(session, now=NOW)["oauth_tokens"] == 2
            session.commit()

        with env.sessions() as session:
            assert {row.refresh_family_id for row in session.query(AssistantOAuthToken)} == {
                "fam_rotated",
                "fam_live",
            }


def test_revocation_tombstones_outlive_the_longest_refresh_token(tmp_path) -> None:
    assert OAuthService.refresh_token_ttl < REVOKED_FAMILY_TTL
    with _env(tmp_path) as env:
        with env.sessions() as session:
            session.add_all(
                [
                    AssistantOAuthRevokedFamily(
                        family_id="fam_old", revoked_at=NOW - REVOKED_FAMILY_TTL - timedelta(hours=1)
                    ),
                    AssistantOAuthRevokedFamily(family_id="fam_recent", revoked_at=NOW - timedelta(days=5)),
                ]
            )
            session.commit()

        with env.sessions() as session:
            assert prune_assistant_state(session, now=NOW)["revoked_families"] == 1
            session.commit()

        with env.sessions() as session:
            assert session.query(AssistantOAuthRevokedFamily).one().family_id == "fam_recent"


def _change(change_id: str, *, expires: datetime, operation_id: str | None = None) -> AssistantChange:
    return AssistantChange(
        id=change_id,
        grant_id="g",
        owner_account_id=42,
        client_id="c",
        grant_revision=1,
        kind="theme",
        intent={},
        dependencies={},
        requirements={},
        effects=[],
        summary={},
        content_hash="h" * 64,
        created_at=expires - timedelta(minutes=15),
        expires_at=expires,
        operation_id=operation_id,
    )


def test_old_unapplied_changes_are_pruned_but_applied_and_recent_ones_are_kept(tmp_path) -> None:
    with _env(tmp_path) as env:
        stale = NOW - UNAPPLIED_CHANGE_RETENTION - timedelta(days=1)
        with env.sessions() as session:
            session.add_all(
                [
                    _change("unapplied-old", expires=stale),
                    _change("recent", expires=NOW - timedelta(days=1)),
                    _change("applied-old", expires=stale),
                    _change("linked-old", expires=stale, operation_id="op-linked"),
                ]
            )
            session.add(
                AssistantOperation(
                    id="op-1",
                    grant_id="g",
                    owner_account_id=42,
                    client_id="c",
                    change_id="applied-old",
                    idempotency_key="k",
                    request_hash="h" * 64,
                    authorization_basis="owner",
                )
            )
            session.commit()

        with env.sessions() as session:
            assert prune_assistant_state(session, now=NOW)["assistant_changes"] == 1
            session.commit()

        with env.sessions() as session:
            assert {row.id for row in session.query(AssistantChange)} == {"recent", "applied-old", "linked-old"}


def test_unused_clients_expire_but_clients_with_a_grant_code_or_token_stay(tmp_path) -> None:
    with _env(tmp_path) as env:
        old = NOW - UNUSED_CLIENT_TTL - timedelta(hours=1)
        for client_id in ("spam", "used-client", "has-token"):
            _client(env.repository, client_id, created=old)
        _client(env.repository, "fresh", created=NOW - timedelta(hours=1))
        _token(env, suffix="t", client_id="has-token", issued=NOW, refresh_expires=NOW + timedelta(days=30))

        assert env.repository.expire_unused_oauth_clients(now=NOW) == 1

        with env.sessions() as session:
            assert {row.client_id for row in session.query(AssistantOAuthClient)} == {
                "used-client",
                "has-token",
                "fresh",
            }


def test_an_old_client_with_only_an_unexpired_consent_flow_survives(tmp_path) -> None:
    with _env(tmp_path) as env:
        _client(env.repository, "mid-authorize", created=NOW - timedelta(hours=25))
        _client(env.repository, "abandoned", created=NOW - timedelta(hours=25))
        with env.sessions() as session:
            session.add(
                AssistantConsentFlow(
                    id="consent_pending",
                    owner_account_id=42,
                    client_id="mid-authorize",
                    redirect_uri=REDIRECT,
                    resource=RESOURCE,
                    scope="instance.read",
                    client_state="s",
                    code_challenge="c" * 43,
                    code_challenge_method="S256",
                    csrf_digest="d" * 64,
                    created_at=NOW - timedelta(minutes=2),
                    expires_at=NOW + timedelta(minutes=8),
                )
            )
            session.commit()

        assert env.repository.expire_unused_oauth_clients(now=NOW) == 1

        with env.sessions() as session:
            assert {row.client_id for row in session.query(AssistantOAuthClient)} == {"mid-authorize"}
            assert session.query(AssistantConsentFlow).count() == 1


def test_register_reclaims_unused_clients_when_the_cap_is_reached(tmp_path, monkeypatch) -> None:
    from fastapi import HTTPException

    from shortlist.server.assistant_auth import routes

    with _env(tmp_path) as env:
        monkeypatch.setattr(routes, "_MAX_OAUTH_CLIENTS", 3)
        monkeypatch.setattr(routes, "_REGISTRATION_HITS", routes.deque())
        old = NOW - UNUSED_CLIENT_TTL - timedelta(hours=1)
        for index in range(3):
            _client(env.repository, f"spam-{index}", created=old)
        oauth = OAuthService(env.repository, issuer="https://shortlist.example/oauth", resource=RESOURCE)
        register = next(
            route.endpoint
            for route in routes.create_oauth_router(env.repository, oauth).routes
            if route.path.endswith("/register")
        )
        body = routes.DynamicClientRegistrationIn(client_name="Real client", redirect_uris=[REDIRECT])

        created = register(SimpleNamespace(), body)

        assert created["client_id"].startswith("client_")
        assert env.repository.oauth_client_count() == 1

        # A cap full of RECENT clients still refuses: only genuinely unused, aged ones are reclaimed.
        for index in range(2):
            _client(env.repository, f"recent-{index}", created=NOW)
        try:
            register(SimpleNamespace(), body)
        except HTTPException as error:
            assert error.status_code == 429
        else:
            raise AssertionError("registration beyond the cap was accepted")


def test_the_maintenance_prune_job_sweeps_assistant_state(tmp_path) -> None:
    with _env(tmp_path) as env:
        _client(env.repository, "used-client", created=NOW - timedelta(days=90))
        _client(env.repository, "spam", created=NOW - UNUSED_CLIENT_TTL - timedelta(hours=1))
        _token(
            env,
            suffix="spent",
            client_id="used-client",
            issued=NOW - timedelta(days=40),
            refresh_expires=NOW - timedelta(days=10),
        )

        result = jobs._HANDLERS["maintenance.prune"](SimpleNamespace(sessions=env.sessions), {})

        assert result["assistant"]["oauth_tokens"] == 1
        assert result["assistant"]["oauth_clients"] == 1
        assert _count(env, AssistantOAuthToken) == 0
        with env.sessions() as session:
            assert [row.client_id for row in session.query(AssistantOAuthClient)] == ["used-client"]
