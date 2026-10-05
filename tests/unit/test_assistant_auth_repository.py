from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset, StoredGrantIdentity
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.assistant_auth.policy import AuthorizationDenied
from shortlist.server.assistant_auth.repository import AssistantAuthRepository, require_current_grant_in_session
from shortlist.server.db.models import Base

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def _repository() -> tuple[AssistantAuthRepository, sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    return AssistantAuthRepository(sessions, CredentialHasher(b"r" * 32)), sessions


def test_local_credential_authenticates_its_named_grant_and_revokes_independently() -> None:
    repository, _ = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="codex-local",
        name="Living room Codex",
        preset=GrantPreset.INSPECT,
        capabilities={Capability.INSTANCE_READ},
        constraints=GrantConstraints(),
        expires_at=NOW + timedelta(days=90),
        now=NOW,
    )
    issued = repository.issue_local_credential(grant.grant_id, expires_at=NOW + timedelta(days=90), now=NOW)
    raw = issued.take()

    authenticated = repository.authenticate_local_credential(raw, now=NOW)

    assert authenticated is not None
    assert authenticated.grant_id == grant.grant_id
    assert repository.authenticate_local_credential("shla_wrong", now=NOW) is None
    repository.revoke_grant(grant.grant_id, now=NOW)
    assert repository.authenticate_local_credential(raw, now=NOW) is None


def test_repository_persists_only_a_digest_of_the_local_credential() -> None:
    repository, sessions = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="claude-local",
        name="Claude",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(),
        now=NOW,
    )
    issued = repository.issue_local_credential(grant.grant_id, now=NOW)
    raw = issued.take()

    with sessions() as session:
        stored = session.query(AssistantGrant).filter_by(id=grant.grant_id).one()
        serialized = repr(stored.__dict__)
        credential_rows = repository.list_local_credentials(grant.grant_id, session=session)

    assert raw not in serialized
    assert raw not in repr(credential_rows[0].__dict__)
    assert credential_rows[0].token_digest == repository.hasher.digest(raw)


def test_reducing_a_grant_increments_revision_and_invalidates_old_context() -> None:
    repository, _ = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.OWNER_AUTOMATION,
        constraints=GrantConstraints(),
        now=NOW,
    )

    updated = repository.replace_grant_authority(
        grant.grant_id,
        capabilities={Capability.INSTANCE_READ},
        constraints=GrantConstraints(max_batch_size=1),
        expected_revision=grant.revision,
        now=NOW,
    )

    assert updated.revision == grant.revision + 1
    assert updated.capabilities == frozenset({Capability.INSTANCE_READ})


def test_reserved_owner_permissions_cannot_be_persisted_in_an_assistant_grant() -> None:
    repository, _ = _repository()

    for reserved in (Capability.SECRETS_READ, Capability.GRANTS_MANAGE):
        try:
            repository.create_grant(
                owner_account_id=42,
                client_id="client",
                name="Assistant",
                preset=GrantPreset.INSPECT,
                capabilities={Capability.INSTANCE_READ, reserved},
                constraints=GrantConstraints(),
                now=NOW,
            )
        except AuthorizationDenied:
            pass
        else:
            raise AssertionError(f"reserved capability {reserved} was accepted")


def test_transactional_recheck_preserves_the_token_capability_subset() -> None:
    repository, sessions = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.OWNER_AUTOMATION,
        constraints=GrantConstraints(),
        now=NOW,
    )
    token_principal = type(grant)(
        grant_id=grant.grant_id,
        owner_account_id=grant.owner_account_id,
        client_id=grant.client_id,
        name=grant.name,
        preset=grant.preset,
        capabilities=frozenset({Capability.INSTANCE_READ}),
        constraints=grant.constraints,
        revision=grant.revision,
        expires_at=grant.expires_at,
    )

    with sessions() as session:
        current = require_current_grant_in_session(session, token_principal, now=NOW)

    assert current.capabilities == frozenset({Capability.INSTANCE_READ})

    stored_identity = StoredGrantIdentity(
        grant_id=grant.grant_id,
        owner_account_id=grant.owner_account_id,
        client_id=grant.client_id,
        revision=grant.revision,
    )
    with sessions() as session:
        approved = require_current_grant_in_session(session, stored_identity, now=NOW)
    assert approved.capabilities == grant.capabilities
