"""Owner choices are exact configured endpoints, never live URL aliases."""

from __future__ import annotations

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
from shortlist.server.assistant_auth.repository import AssistantAuthRepository, GrantUpdateConflict
from shortlist.server.assistant_auth.types import Capability, GrantConstraints, GrantPreset
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
