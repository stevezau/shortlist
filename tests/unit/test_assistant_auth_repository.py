from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset, StoredGrantIdentity
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.models import (
    AssistantBootstrapFlow,
    AssistantConsentFlow,
    AssistantGrant,
    AssistantLocalCredential,
    AssistantOAuthCode,
    AssistantOAuthToken,
)
from shortlist.server.assistant_auth.policy import AuthorizationDenied
from shortlist.server.assistant_auth.repository import (
    AssistantAuthRepository,
    GrantUpdateConflict,
    GrantUpdateNotFound,
    require_current_grant_in_session,
)
from shortlist.server.db.models import Base, Event

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


def test_constraints_patch_is_owner_scoped_cas_and_preserves_other_grant_authority() -> None:
    repository, sessions = _repository()
    expires_at = NOW + timedelta(days=90)
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.OWNER_AUTOMATION,
        capabilities={Capability.INSTANCE_READ},
        constraints=GrantConstraints(
            row_ids=frozenset({1}),
            max_batch_size=4,
            max_work_per_operation=8,
            max_provider_calls=3,
        ),
        expires_at=expires_at,
        now=NOW,
    )

    updated = repository.patch_grant_constraints(
        grant.grant_id,
        owner_account_id=42,
        expected_revision=grant.revision,
        constraints_patch={"row_ids": set(), "max_batch_size": None, "max_provider_calls": 0},
        now=NOW,
    )

    assert updated.constraints == GrantConstraints(max_work_per_operation=8)
    assert updated.revision == 2
    assert (updated.owner_account_id, updated.client_id, updated.name, updated.preset, updated.capabilities) == (
        grant.owner_account_id,
        grant.client_id,
        grant.name,
        grant.preset,
        grant.capabilities,
    )
    assert updated.expires_at == grant.expires_at
    with sessions() as session:
        audit = session.query(Event).one()
        assert audit.scope == "assistant.grant.update"
        assert audit.message == {
            "grant_id": grant.grant_id,
            "actor": {"via": "browser", "account_id": 42},
            "changed_fields": ["max_batch_size", "max_provider_calls", "row_ids"],
            "revision": 2,
        }

    with pytest.raises(GrantUpdateConflict, match="assistant grant changed"):
        repository.patch_grant_constraints(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=updated.revision,
            constraints_patch={"row_ids": {2}},
            now=expires_at,
        )
    with sessions() as session:
        stored = session.get(AssistantGrant, grant.grant_id)
        assert GrantConstraints.from_dict(stored.constraints) == updated.constraints
        assert stored.revision == updated.revision
        assert session.query(Event).count() == 1

    with pytest.raises(GrantUpdateConflict, match="assistant grant changed"):
        repository.patch_grant_constraints(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=grant.revision,
            constraints_patch={"row_ids": {2}},
            now=NOW,
        )
    with pytest.raises(GrantUpdateNotFound, match="assistant grant not found"):
        repository.patch_grant_constraints(
            grant.grant_id,
            owner_account_id=7,
            expected_revision=updated.revision,
            constraints_patch={"row_ids": {2}},
            now=NOW,
        )


def test_legacy_grant_needs_explicit_approval_and_preserves_paid_limits() -> None:
    repository, sessions = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="legacy-client",
        name="Legacy assistant",
        preset=GrantPreset.OWNER_AUTOMATION,
        constraints=GrantConstraints(
            person_ids=frozenset({10}),
            include_future_people=False,
            max_batch_size=25,
            max_work_per_operation=100,
            max_provider_calls=3,
        ),
        now=NOW,
    )

    with pytest.raises(GrantUpdateConflict, match="owner approval"):
        repository.patch_grant_constraints(
            grant.grant_id, owner_account_id=42, expected_revision=1, constraints_patch={"row_ids": {2}}, now=NOW
        )
    approved = repository.approve_updated_access(grant.grant_id, owner_account_id=42, expected_revision=1, now=NOW)

    assert not approved.constraints.requires_access_approval
    assert approved.constraints.person_ids == frozenset()
    assert approved.constraints.include_future_people is True
    assert approved.constraints.as_dict()["person_ids"] == []
    assert approved.constraints.as_dict()["include_future_people"] is True
    assert approved.constraints.max_batch_size == 25
    assert approved.constraints.max_work_per_operation is None
    assert approved.constraints.max_provider_calls == 3
    with sessions() as session:
        audit = session.query(Event).order_by(Event.id.desc()).one()
        assert audit.scope == "assistant.grant.approved_updated_access"


@pytest.mark.parametrize("stored_value", [None, False, 1, "true"])
def test_legacy_people_marker_requires_exact_true(stored_value: object | None) -> None:
    persisted: dict[str, object] = {"person_ids": [10]}
    if stored_value is not None:
        persisted["include_future_people"] = stored_value

    constraints = GrantConstraints.from_dict(persisted)

    assert constraints.requires_access_approval
    assert constraints.as_dict()["person_ids"] == [10]
    assert constraints.as_dict()["include_future_people"] is False


def test_exact_true_people_marker_is_rollback_compatible_all_people_access() -> None:
    constraints = GrantConstraints.from_dict({"person_ids": [10], "include_future_people": True})

    assert not constraints.requires_access_approval
    assert constraints.as_dict()["person_ids"] == [10]
    assert constraints.as_dict()["include_future_people"] is True


@pytest.mark.parametrize("utc_offset_hours", [10, -7])
def test_grant_contexts_normalize_expiry_to_aware_utc(utc_offset_hours: int) -> None:
    repository, _ = _repository()
    original_expiry = datetime(2027, 1, 3, 15, 0, tzinfo=timezone(timedelta(hours=utc_offset_hours)))
    expires_at = original_expiry.astimezone(UTC)
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(row_ids=frozenset({1})),
        expires_at=original_expiry,
        now=NOW,
    )

    fresh = repository.get_grant_context(grant.grant_id, now=NOW)
    patched = repository.patch_grant_constraints(
        grant.grant_id,
        owner_account_id=42,
        expected_revision=grant.revision,
        constraints_patch={"row_ids": {2}},
        now=NOW,
    )

    assert fresh is not None
    assert (grant.expires_at, fresh.expires_at, patched.expires_at) == (expires_at, expires_at, expires_at)


def test_grant_context_preserves_no_expiry() -> None:
    repository, _ = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(),
        now=NOW,
    )

    fresh = repository.get_grant_context(grant.grant_id, now=NOW)

    assert grant.expires_at is None
    assert fresh is not None
    assert fresh.expires_at is None


def test_constraints_patch_rolls_back_when_its_audit_cannot_be_written() -> None:
    repository, sessions = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(row_ids=frozenset({1})),
        now=NOW,
    )

    def reject_audit(session, _flush_context, _instances) -> None:
        if any(isinstance(row, Event) for row in session.new):
            raise RuntimeError("audit storage failed")

    event.listen(sessions.class_, "before_flush", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="audit storage failed"):
            repository.patch_grant_constraints(
                grant.grant_id,
                owner_account_id=42,
                expected_revision=grant.revision,
                constraints_patch={"row_ids": {2}},
                now=NOW,
            )
    finally:
        event.remove(sessions.class_, "before_flush", reject_audit)

    with sessions() as session:
        stored = session.get(AssistantGrant, grant.grant_id)
        assert GrantConstraints.from_dict(stored.constraints) == grant.constraints
        assert stored.revision == grant.revision
        assert session.query(Event).count() == 0


def test_removing_a_revoked_grant_cascades_credentials_and_retains_audit_history() -> None:
    repository, sessions = _repository()
    grant = repository.create_grant(
        owner_account_id=42,
        client_id="removed-client",
        name="Old connection",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(),
        now=NOW,
    )
    repository.issue_local_credential(grant.grant_id, now=NOW)
    repository.register_oauth_client(
        client_id=grant.client_id,
        client_name="Removed client",
        redirect_uris=["http://127.0.0.1:49152/callback"],
        now=NOW,
    )
    repository.revoke_grant(grant.grant_id, now=NOW)
    with sessions() as session:
        session.execute(text("PRAGMA foreign_keys=ON"))
        session.add_all(
            [
                AssistantOAuthCode(
                    code_digest="c" * 64,
                    grant_id=grant.grant_id,
                    owner_account_id=42,
                    client_id=grant.client_id,
                    redirect_uri="http://127.0.0.1:49152/callback",
                    resource="https://shortlist.example/mcp",
                    scope="instance.read",
                    code_challenge="c" * 64,
                    code_challenge_method="S256",
                    created_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                ),
                AssistantOAuthToken(
                    grant_id=grant.grant_id,
                    client_id=grant.client_id,
                    issuer="https://shortlist.example/assistant/oauth",
                    resource="https://shortlist.example/mcp",
                    scope="instance.read",
                    access_digest="a" * 64,
                    access_prefix="mcp_access",
                    access_expires_at=NOW + timedelta(minutes=10),
                    refresh_digest="r" * 64,
                    refresh_prefix="mcp_refresh",
                    refresh_expires_at=NOW + timedelta(days=1),
                    refresh_family_id="family-removed",
                    issued_at=NOW,
                ),
                AssistantConsentFlow(
                    id="flow-removed",
                    owner_account_id=42,
                    client_id=grant.client_id,
                    grant_id=grant.grant_id,
                    redirect_uri="http://127.0.0.1:49152/callback",
                    resource="https://shortlist.example/mcp",
                    scope="instance.read",
                    client_state="state",
                    code_challenge="p" * 64,
                    code_challenge_method="S256",
                    csrf_digest="s" * 64,
                    created_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                ),
                AssistantBootstrapFlow(
                    id="bootstrap-removed",
                    client_id=grant.client_id,
                    client_name="Removed client",
                    deployment_proof_digest="b" * 64,
                    grant_id=grant.grant_id,
                    created_at=NOW,
                    expires_at=NOW + timedelta(days=1),
                ),
                Event(scope="assistant.history", message={"grant_id": grant.grant_id}),
            ]
        )
        session.commit()

    assert repository.remove_revoked_grant(grant.grant_id, owner_account_id=42, now=NOW) is True

    with sessions() as session:
        assert session.get(AssistantGrant, grant.grant_id) is None
        assert session.query(AssistantLocalCredential).filter_by(grant_id=grant.grant_id).count() == 0
        assert session.query(AssistantOAuthCode).filter_by(grant_id=grant.grant_id).count() == 0
        assert session.query(AssistantOAuthToken).filter_by(grant_id=grant.grant_id).count() == 0
        assert session.query(AssistantConsentFlow).filter_by(grant_id=grant.grant_id).count() == 0
        assert session.get(AssistantBootstrapFlow, "bootstrap-removed").grant_id is None
        assert session.query(Event).filter_by(scope="assistant.history").count() == 1
        removed = session.query(Event).filter_by(scope="assistant.grant.removed").one()
        assert removed.message == {
            "grant_id": grant.grant_id,
            "actor": {"via": "browser", "account_id": 42},
        }


def test_removing_a_grant_is_owner_scoped_idempotent_and_requires_prior_revocation() -> None:
    repository, sessions = _repository()
    active = repository.create_grant(
        owner_account_id=42,
        client_id="active-client",
        name="Active",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(),
        now=NOW,
    )
    foreign = repository.create_grant(
        owner_account_id=7,
        client_id="foreign-client",
        name="Foreign",
        preset=GrantPreset.INSPECT,
        constraints=GrantConstraints(),
        now=NOW,
    )
    repository.revoke_grant(foreign.grant_id, now=NOW)

    with pytest.raises(ValueError, match="revoke"):
        repository.remove_revoked_grant(active.grant_id, owner_account_id=42, now=NOW)
    assert repository.remove_revoked_grant(foreign.grant_id, owner_account_id=42, now=NOW) is False
    with sessions() as session:
        assert session.get(AssistantGrant, foreign.grant_id) is not None
    assert repository.remove_revoked_grant("grt_missing", owner_account_id=42, now=NOW) is False
    with sessions() as session:
        assert session.query(Event).filter_by(scope="assistant.grant.removed").count() == 0


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
