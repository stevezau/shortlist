"""Owner choices are exact configured endpoints, never live URL aliases."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shortlist.server.assistant.budgets import AssistantBudget
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.destinations import (
    DestinationSelection,
    DestinationSelectionConflict,
    configured_destinations,
)
from shortlist.server.assistant_auth.policy import AuthorizationDenied, owner_managed_capabilities, require_authorized
from shortlist.server.assistant_auth.repository import (
    AssistantAuthRepository,
    GrantUpdateConflict,
    require_current_grant_in_session,
)
from shortlist.server.assistant_auth.types import Capability, GrantConstraints, GrantPreset, ResourceSelection
from shortlist.server.db.models import Base, Event, Setting
from tests.db_helpers import disposing_engine


def test_configured_catalog_uses_exact_runtime_destinations_without_secrets() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions() as session:
            session.add_all(
                [
                    Setting(key="curator.provider", value={"v": "openai"}),
                    Setting(key="curator.api_key", value={"v": "ciphertext"}),
                    Setting(key="exa.apikey", value={"v": "ciphertext"}),
                    Setting(key="searxng.url", value={"v": "http://localhost:8080/search/"}),
                    Setting(key="requests.radarr.url", value={"v": "http://localhost:7878/"}),
                    Setting(key="requests.radarr.apikey", value={"v": "ciphertext"}),
                    Setting(key="requests.sonarr.url", value={"v": "https://user:pass@example.com"}),
                    Setting(key="requests.sonarr.apikey", value={"v": "ciphertext"}),
                    Setting(key="plex.url", value={"v": "http://localhost:32400/"}),
                    Setting(key="plex.token", value={"v": "ciphertext"}),
                    Setting(key="tautulli.url", value={"v": "http://localhost:8181/"}),
                    Setting(key="tautulli.apikey", value={"v": "ciphertext"}),
                    Setting(key="tmdb.apikey", value={"v": "ciphertext"}),
                    Setting(key="trakt.client_id", value={"v": "client-id"}),
                    Setting(key="requests.mdblist.apikey", value={"v": "ciphertext"}),
                ]
            )
            session.commit()
            choices = {item.service_id: item for item in configured_destinations(session)}

        assert {key: item.destination_id for key, item in choices.items()} == {
            "curator": "https://api.openai.com/v1",
            "exa": "https://api.exa.ai",
            "searxng": "http://localhost:8080/search",
            "radarr": "http://localhost:7878",
            "plex": "http://localhost:32400",
            "tautulli": "http://localhost:8181",
            "tmdb": "https://api.themoviedb.org/3",
            "trakt": "https://api.trakt.tv",
            "mdblist": "https://api.mdblist.com",
        }
        assert "ciphertext" not in repr(choices)


def test_selected_endpoint_change_is_rejected_without_writing_a_grant() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"r" * 32))
        with sessions() as session:
            session.add(Setting(key="searxng.url", value={"v": "http://localhost:8080"}))
            session.commit()
            seen = configured_destinations(session)[0]
            session.get(Setting, "searxng.url").value = {"v": "http://localhost:8081"}
            session.commit()

        try:
            repository.create_grant(
                owner_account_id=42,
                client_id="client",
                name="Assistant",
                preset=GrantPreset.INSPECT,
                constraints=GrantConstraints(destination_ids=frozenset({seen.destination_id})),
                selected_destinations=[
                    DestinationSelection(service_id=seen.service_id, destination_id=seen.destination_id)
                ],
            )
        except DestinationSelectionConflict:
            pass
        else:
            raise AssertionError("a changed service endpoint was approved")
        assert repository.list_grants(42) == []

        grant = repository.create_grant(
            owner_account_id=42,
            client_id="client",
            name="Assistant",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(destination_ids=frozenset({seen.destination_id})),
        )
        updated = repository.patch_grant_constraints(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=grant.revision,
            constraints_patch={"max_batch_size": 10},
        )
        assert updated.constraints.destination_ids == frozenset({seen.destination_id})
        assert updated.revision == 2


def test_capabilities_and_quota_change_in_one_revision() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"r" * 32))
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="client",
            name="Assistant",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        updated = repository.patch_grant_constraints(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=grant.revision,
            constraints_patch={"max_provider_calls": 2},
            capabilities={*grant.capabilities, Capability.AI_GENERATE},
        )
        assert updated.revision == 2
        assert updated.constraints.max_provider_calls == 2
        assert Capability.AI_GENERATE in updated.capabilities
        try:
            repository.patch_grant_constraints(
                grant.grant_id,
                owner_account_id=42,
                expected_revision=grant.revision,
                constraints_patch={"max_provider_calls": 0},
                capabilities=set(grant.capabilities),
            )
        except GrantUpdateConflict:
            pass
        else:
            raise AssertionError("a stale capability and quota update was accepted")
        assert repository.get_grant_context(grant.grant_id).constraints.max_provider_calls == 2


def test_stale_selected_service_rolls_back_combined_permissions_quota_and_audit() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"r" * 32))
        with sessions() as session:
            session.add(Setting(key="searxng.url", value={"v": "http://localhost:8080"}))
            session.commit()
            seen = configured_destinations(session)[0]
            session.get(Setting, "searxng.url").value = {"v": "http://localhost:8081"}
            session.commit()
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="client",
            name="Assistant",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )

        try:
            repository.patch_grant_constraints(
                grant.grant_id,
                owner_account_id=42,
                expected_revision=grant.revision,
                constraints_patch={
                    "destination_ids": frozenset({seen.destination_id}),
                    "max_provider_calls": 2,
                },
                capabilities={*grant.capabilities, Capability.AI_GENERATE},
                selected_destinations=[
                    DestinationSelection(service_id=seen.service_id, destination_id=seen.destination_id)
                ],
            )
        except DestinationSelectionConflict:
            pass
        else:
            raise AssertionError("stale selected service was approved")

        unchanged = repository.get_grant_context(grant.grant_id)
        assert unchanged.revision == grant.revision
        assert unchanged.capabilities == grant.capabilities
        assert unchanged.constraints.destination_ids == frozenset()
        assert unchanged.constraints.max_provider_calls == 0
        with sessions() as session:
            assert session.scalars(select(Event).where(Event.scope == "assistant.grant.update")).all() == []


def test_owner_quota_summary_preserves_uncertain_reservations_when_limit_is_zero() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"r" * 32))
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="client",
            name="Assistant",
            preset=GrantPreset.INSPECT,
            capabilities={Capability.INSTANCE_READ, Capability.AI_GENERATE},
            constraints=GrantConstraints(max_provider_calls=0),
        )
        with sessions() as session:
            session.add(AssistantBudget(grant_id=grant.grant_id, provider_calls_reserved=1))
            session.commit()
        before = repository.list_grant_summaries(42)[0]
        assert before.provider_calls_reserved == 1
        assert before.context.constraints.max_provider_calls == 0
        updated = repository.patch_grant_constraints(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=grant.revision,
            constraints_patch={"max_batch_size": 26},
        )
        after = repository.list_grant_summaries(42)[0]
        assert updated.capabilities == grant.capabilities
        assert after.provider_calls_reserved == 1
        assert after.context.constraints.max_provider_calls == 0


def test_owner_profile_resolves_current_services_and_settings_in_every_transaction() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"p" * 32))
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="local-client",
            name="Full owner consent",
            preset=GrantPreset.OWNER_AUTOMATION,
            constraints=GrantConstraints(owner_managed=True),
        )
        credential = repository.issue_local_credential(grant.grant_id).take()
        assert grant.constraints.owner_managed
        assert grant.constraints.include_future_rows and grant.constraints.include_future_libraries
        assert owner_managed_capabilities() == set(grant.capabilities)
        assert Capability.MAINTENANCE_EXECUTE in grant.capabilities
        assert Capability.HISTORY_EXPORT in grant.capabilities
        assert Capability.HISTORY_PROVIDERS in grant.capabilities
        assert Capability.REQUESTS_SEND in grant.capabilities
        assert Capability.AI_GENERATE not in grant.capabilities
        assert {Capability.SECRETS_READ, Capability.GRANTS_MANAGE}.isdisjoint(grant.capabilities)
        assert not grant.constraints.destination_ids
        require_authorized(
            grant,
            [Capability.CONFIG_WRITE],
            ResourceSelection(setting_groups=frozenset({"row_defaults"})),
        )

        with sessions() as session:
            session.add(Setting(key="searxng.url", value={"v": "http://localhost:8080"}))
            session.commit()
        fresh = repository.authenticate_local_credential(credential)
        assert fresh is not None
        require_authorized(
            fresh,
            [Capability.HISTORY_PROVIDERS],
            ResourceSelection(destination_ids=frozenset({"http://localhost:8080"})),
        )
        with pytest.raises(AuthorizationDenied):
            require_authorized(
                fresh,
                [Capability.HISTORY_PROVIDERS],
                ResourceSelection(destination_ids=frozenset({"https://unregistered.invalid"})),
            )
        with sessions() as session:
            current = require_current_grant_in_session(session, fresh)
            assert "http://localhost:8080" in current.constraints.destination_ids
            session.get(Setting, "searxng.url").value = {"v": "http://localhost:8081"}
            session.commit()
        with sessions() as session:
            current = require_current_grant_in_session(session, fresh)
            assert "http://localhost:8081" in current.constraints.destination_ids
            assert "http://localhost:8080" not in current.constraints.destination_ids
            session.delete(session.get(Setting, "searxng.url"))
            session.commit()
        assert not repository.get_grant_context(grant.grant_id).constraints.destination_ids


def test_owner_profile_upgrade_preserves_legacy_paid_state_and_is_revision_guarded() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"p" * 32))
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="legacy-client",
            name="Restricted",
            preset=GrantPreset.INSPECT,
            capabilities={Capability.INSTANCE_READ, Capability.AI_GENERATE},
            constraints=GrantConstraints(include_future_people=False, max_provider_calls=0),
        )
        with sessions() as session:
            session.add(AssistantBudget(grant_id=grant.grant_id, provider_calls_reserved=1))
            session.commit()
        assert not repository.get_grant_context(grant.grant_id).constraints.owner_managed
        updated = repository.update_owner_managed(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=grant.revision,
            upgrade=True,
        )
        assert updated.constraints.owner_managed
        assert updated.constraints.max_provider_calls == 0
        assert Capability.AI_GENERATE in updated.capabilities
        assert updated.constraints.include_future_people
        assert repository.list_grant_summaries(42)[0].provider_calls_reserved == 1
        with pytest.raises(GrantUpdateConflict):
            repository.update_owner_managed(
                grant.grant_id,
                owner_account_id=42,
                expected_revision=grant.revision,
                paid_enabled=True,
                max_provider_calls=2,
            )
        assert repository.get_grant_context(grant.grant_id).constraints.max_provider_calls == 0
        with pytest.raises(ValueError, match="exceed"):
            repository.update_owner_managed(
                grant.grant_id,
                owner_account_id=42,
                expected_revision=updated.revision,
                paid_enabled=True,
                max_provider_calls=1,
            )
        paid = repository.update_owner_managed(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=updated.revision,
            paid_enabled=True,
            max_provider_calls=2,
        )
        assert paid.constraints.max_provider_calls == 2


def test_profile_marker_is_legacy_false_and_cannot_be_set_by_granular_patch() -> None:
    from pydantic import ValidationError

    from shortlist.server.assistant_auth.routes import GrantConstraintsPatchIn

    assert GrantConstraints.from_dict({}).owner_managed is False
    assert GrantConstraints.from_dict({"owner_managed": True}).owner_managed is True
    with pytest.raises(ValidationError):
        GrantConstraintsPatchIn.model_validate(
            {
                "expected_revision": 1,
                "constraints": {"owner_managed": True},
            }
        )
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        repository = AssistantAuthRepository(
            sessionmaker(bind=engine, expire_on_commit=False), CredentialHasher(b"p" * 32)
        )
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="legacy",
            name="Legacy",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        with pytest.raises(ValueError, match="unknown assistant grant constraints"):
            repository.patch_grant_constraints(
                grant.grant_id,
                owner_account_id=42,
                expected_revision=grant.revision,
                constraints_patch={"owner_managed": True},
            )
        assert not repository.get_grant_context(grant.grant_id).constraints.owner_managed


def test_upgrade_keeps_positive_legacy_quota_unusable_without_paid_capability() -> None:
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"p" * 32))
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="legacy",
            name="No paid capability",
            preset=GrantPreset.INSPECT,
            capabilities={Capability.INSTANCE_READ},
            constraints=GrantConstraints(max_provider_calls=5),
        )
        with sessions() as session:
            session.add(AssistantBudget(grant_id=grant.grant_id, provider_calls_reserved=1))
            session.commit()
        upgraded = repository.update_owner_managed(
            grant.grant_id,
            owner_account_id=42,
            expected_revision=grant.revision,
            upgrade=True,
        )
        assert upgraded.constraints.max_provider_calls == 5
        assert Capability.AI_GENERATE not in upgraded.capabilities
        assert repository.list_grant_summaries(42)[0].provider_calls_reserved == 1


def test_profile_has_maintenance_capability_but_task_still_requires_exact_owner_approval() -> None:
    from shortlist.server.assistant.maintenance_adapter import MaintenanceAdapter

    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as session:
            plan = MaintenanceAdapter(None).prepare(session, {"task": "cache.refresh"})
    assert Capability.MAINTENANCE_EXECUTE in owner_managed_capabilities()
    assert plan.requirements.capabilities == (Capability.MAINTENANCE_EXECUTE.value,)
    assert plan.requirements.requires_approval is True
