"""Fixtures and builders the assistant tests share; moved verbatim from the first module that needed each."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.changes import ChangeService
from shortlist.server.assistant.request_adapter import RequestAdapter
from shortlist.server.assistant.setup_adapter import SetupAdapter
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.db.models import Collection, RequestCandidate, Server, User
from shortlist.server.services.run_service import RunService
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import SettingsStore
from tests.db_helpers import create_schema, disposing_engine


def candidate(candidate_id: int = 1) -> RequestCandidate:
    return RequestCandidate(
        id=candidate_id,
        tmdb_id=100 + candidate_id,
        media_type="movie",
        title=f"Title {candidate_id}",
        rating=8.0,
        vote_count=1000,
        demand=2,
        status="pending",
        tags=["shortlist"],
        wanters=["sarah"],
        why=[],
    )


@pytest.fixture
def request_env(tmp_path):
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'requests.db'}")) as engine:
        create_schema(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path))
        state.run_service = SimpleNamespace(
            build_requests_context=lambda: pytest.fail("separate context during planning")
        )
        with sessions() as session:
            session.add(
                Server(machine_id="requests", url="http://unused.invalid", token_enc="unused", owner_account_id=42)
            )
            session.add(User(id=1, slug="sarah", username="sarah", plex_account_id=10))
            session.add(Collection(id=1, slug="movies", name="Movies", library_keys=["1"]))
            item = candidate()
            item.row_slug = "movies"
            session.add(item)
            store = SettingsStore(session, state.secrets)
            for key, value in {
                "requests.enabled": True,
                "requests.target": "arr",
                "requests.radarr.url": "http://radarr.test",
                "requests.radarr.apikey": "private-request-key",
                "requests.radarr.quality_profile_id": 7,
                "requests.radarr.root_folder": "/movies",
                "requests.tag": "shortlist",
                "tmdb.apikey": "private-metadata-key",
            }.items():
                store.set_in_transaction(key, value)
            session.commit()
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"request-test-key-0000000000000000"))
        principal = repository.create_grant(
            owner_account_id=42,
            client_id="requests-test",
            name="Requests test",
            preset=GrantPreset.OWNER_AUTOMATION,
            capabilities={
                Capability.CHANGES_PREPARE,
                Capability.REQUESTS_READ,
                Capability.REQUESTS_MANAGE,
                Capability.REQUESTS_SEND,
            },
            constraints=GrantConstraints(
                row_ids=frozenset({1}),
                person_ids=frozenset({1}),
                library_keys=frozenset({"1"}),
                destination_ids=frozenset({"radarr", "http://radarr.test"}),
                max_batch_size=25,
            ),
        )
        state.assistant_auth = SimpleNamespace(repository=repository)
        adapter = RequestAdapter(state)
        service = ChangeService(sessions, {"requests": adapter})
        yield SimpleNamespace(state=state, adapter=adapter, service=service, principal=principal, repository=repository)


@pytest.fixture
def run_env(tmp_path):
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'runs.db'}")) as engine:
        create_schema(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        with sessions() as session:
            session.add(User(id=1, plex_account_id=11, slug="alice", username="alice", enabled=True))
            session.add(User(id=2, plex_account_id=22, slug="bob", username="bob", enabled=True))
            session.add(Collection(id=1, slug="row-one", name="Picked", library_keys=["1"]))
            session.commit()
        from shortlist.server.services.secrets import SecretBox

        state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path), assistant_auth=object())
        state.run_service = RunService(sessions, SimpleNamespace(publish=lambda *a: None), tmp_path, state.secrets)
        state.run_service.state = state
        yield state


@pytest.fixture
def setup_env(tmp_path):
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'setup.db'}")) as engine:
        create_schema(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        with sessions() as session:
            session.add(
                Server(machine_id="setup", url="http://unused.invalid", token_enc="unused", owner_account_id=42)
            )
            session.add(User(id=1, username="recipient", slug="recipient", plex_account_id=1))
            session.commit()
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"setup-transaction-key-00000000000"))
        principal = repository.create_grant(
            owner_account_id=42,
            client_id="setup-test",
            name="Setup test",
            preset=GrantPreset.OWNER_AUTOMATION,
            constraints=GrantConstraints(person_ids=frozenset({1}), library_keys=frozenset({"1"})),
        )
        state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path))
        adapter = SetupAdapter(state)
        service = ChangeService(sessions, {"setup": adapter})
        intent = {
            "theme": {"name": "Quiet comedy", "media": ["movie"], "genres": ["Comedy"]},
            "row": {
                "action": "create",
                "template_id": "picked-for-you",
                "values": {
                    "name": "Quiet night",
                    "audience": "subset",
                    "audience_user_ids": [1],
                    "library_keys": ["1"],
                },
            },
        }
        yield SimpleNamespace(
            sessions=sessions,
            repository=repository,
            principal=principal,
            adapter=adapter,
            service=service,
            intent=intent,
        )
