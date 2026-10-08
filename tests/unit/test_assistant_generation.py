"""A generation reservation permits one dispatch, even if authoring asks twice."""

from types import SimpleNamespace

import pytest

from shortlist.server.assistant.generation import BudgetedCurator, GenerationIntent, provider_destination
from tests.db_helpers import disposing_engine


def test_one_dispatch_and_output_ceiling_are_enforced():
    calls = []
    completed = []

    class Provider:
        name = "fake"
        last_tokens = 11

        def complete(self, system, user, *, max_tokens):
            calls.append(max_tokens)
            return "reply"

    wrapper = BudgetedCurator(
        Provider(), maximum_tokens=1200, record=lambda text, tokens: completed.append((text, tokens))
    )
    assert wrapper.complete("system", "brief", max_tokens=8000) == "reply"
    assert calls == [1200]
    assert completed == [("reply", 11)]
    with pytest.raises(RuntimeError, match="one"):
        wrapper.complete("system", "again", max_tokens=8000)
    assert calls == [1200]


def test_generation_requires_an_explicit_bounded_brief():
    with pytest.raises(ValueError):
        GenerationIntent(brief="", media="movie")
    with pytest.raises(ValueError):
        GenerationIntent(brief="Films", media="movie", max_output_tokens=100000)


def test_local_provider_destination_is_explicit_and_strips_no_credentials():
    store = SimpleNamespace(
        get=lambda key: {
            "curator.provider": "openai_compatible",
            "curator.openai_base_url": "http://localhost:1234/v1",
        }.get(key)
    )
    assert provider_destination(store) == "http://localhost:1234/v1"
    bad = SimpleNamespace(
        get=lambda key: {
            "curator.provider": "openai_compatible",
            "curator.openai_base_url": "https://user:secret@example.com/v1",
        }.get(key)
    )
    with pytest.raises(ValueError):
        provider_destination(bad)


@pytest.fixture
def generation_env(tmp_path, monkeypatch):
    import json

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from shortlist.server.assistant import generation
    from shortlist.server.assistant.changes import ChangeService
    from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
    from shortlist.server.assistant_auth.credentials import CredentialHasher
    from shortlist.server.assistant_auth.repository import AssistantAuthRepository
    from shortlist.server.db.models import Base, Server
    from shortlist.server.services.secrets import SecretBox
    from shortlist.server.settings_store import SettingsStore

    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'generation.db'}")) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path))
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"test-generation-hash-key-00000000"))
        state.assistant_auth = SimpleNamespace(repository=repository)
        with sessions() as session:
            session.add(
                Server(machine_id="generation", url="http://unused.invalid", token_enc="unused", owner_account_id=42)
            )
            store = SettingsStore(session, state.secrets)
            store.set_in_transaction("curator.provider", "openai")
            store.set_in_transaction("curator.api_key", "fake-provider-secret")
            store.set_in_transaction("tmdb.apikey", "fake-metadata-secret")
            session.commit()
        principal = repository.create_grant(
            owner_account_id=42,
            client_id="generation-test",
            name="Generation test",
            preset=GrantPreset.OWNER_AUTOMATION,
            capabilities={Capability.CHANGES_PREPARE, Capability.AI_GENERATE, Capability.CATALOG_READ},
            constraints=GrantConstraints(
                destination_ids=frozenset({"https://api.openai.com/v1"}), max_provider_calls=1
            ),
        )
        calls, factories, plex_reads = [], [], []
        answer = json.dumps(
            {
                "name": "Twists",
                "emoji": "🎬",
                "rules": {},
                "tags": [],
                "genres": ["Thriller"],
                "titles": [{"media": "movie", "title": "Memento", "year": 2000}],
                "collections": [{"section_key": "private", "title": "Do not read this"}],
            }
        )

        class Provider:
            name = "fake"
            can_complete = True
            last_tokens = 11

            def complete(self, system, user, *, max_tokens):
                calls.append({"max_tokens": max_tokens, "system": system, "user": user})
                return answer

        def factory(provider, **kwargs):
            factories.append({"provider": provider, **kwargs})
            return Provider()

        class Metadata:
            def search(self, title, media, *, year=None):
                return {"id": 1, "title": "Memento"}

            def search_all(self, title, media):
                return [self.search(title, media)]

            def search_keywords(self, query, limit=10):
                return []

            def discover_all(self, media, params):
                return []

            def list_item(self, tmdb_id, media):
                return {"id": 1, "title": "Memento", "release_date": "2000-01-01", "vote_average": 8, "vote_count": 999}

            def details(self, tmdb_id, media):
                return {"runtime": 113}

        class NoPlex:
            def __getattr__(self, name):
                plex_reads.append(name)
                raise AssertionError("Generation must not inspect Plex")

        monkeypatch.setattr(generation, "make_curator", factory)
        monkeypatch.setattr(generation, "TmdbClient", lambda *args, **kwargs: Metadata())
        monkeypatch.setattr(generation, "_NoLibraryReader", NoPlex)
        state.changes = ChangeService(sessions, {"generation": generation.GenerationAdapter(state)})
        yield SimpleNamespace(state=state, principal=principal, calls=calls, factories=factories, plex_reads=plex_reads)


def _generation_operation(env, *, principal=None, approve=False, key="generation-test-key"):
    from sqlalchemy import select

    from shortlist.server.db.models import Job

    who = principal or env.principal
    plan = env.state.changes.prepare(
        who, "generation", {"brief": "Films with twists", "media": "movie", "max_output_tokens": 1200}
    )
    if approve:
        env.state.changes.approve(plan["change_id"], owner_account_id=42)
    receipt = env.state.changes.apply(who, plan["change_id"], key)
    with env.state.sessions() as session:
        job = session.scalars(select(Job).where(Job.operation_id == receipt["operation_id"])).one()
        job_id = job.id
    return plan, receipt, job_id


def test_dispatch_uses_one_bounded_provider_call_and_never_reads_plex(generation_env):
    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant.operation_models import AssistantOperation

    env = generation_env
    _plan, receipt, job_id = _generation_operation(env)
    result = dispatch_generation(env.state, {}, job_id=job_id)
    assert result["status"] == "completed"
    assert len(env.calls) == 1
    assert env.calls[0]["max_tokens"] == 1200
    assert env.factories[0]["max_retries"] == 0
    assert env.factories[0]["base_url"] == "https://api.openai.com/v1"
    assert env.plex_reads == []
    with env.state.sessions() as session:
        operation = session.get(AssistantOperation, receipt["operation_id"])
        assert operation.result["draft"]["picks"][0]["tmdb_id"] == 1
        assert "fake-provider-secret" not in str(operation.result)
        assert operation.result["library_availability"] == "not_checked"
    dispatch_generation(env.state, {}, job_id=job_id)
    assert len(env.calls) == 1


def test_checkpoint_replay_never_dispatches_or_audits_a_second_provider_call(generation_env, monkeypatch):
    from sqlalchemy import select

    from shortlist.server.assistant import generation
    from shortlist.server.db.models import Event

    env = generation_env
    _plan, _receipt, job_id = _generation_operation(env)
    author = generation.author_theme

    def interrupted_metadata(**kwargs):
        kwargs["curator"].complete("system", "brief", max_tokens=4000)
        raise RuntimeError("metadata process interrupted after provider reply")

    monkeypatch.setattr(generation, "author_theme", interrupted_metadata)
    with pytest.raises(RuntimeError):
        generation.dispatch_generation(env.state, {}, job_id=job_id)
    assert len(env.calls) == 1
    monkeypatch.setattr(generation, "author_theme", author)
    assert generation.dispatch_generation(env.state, {}, job_id=job_id)["status"] == "completed"
    assert len(env.calls) == 1
    assert len(env.factories) == 1
    with env.state.sessions() as session:
        calls = session.scalars(select(Event).where(Event.scope == "assistant.provider_call")).all()
        assert len(calls) == 1


def test_unknown_dispatch_is_never_replayed_or_refunded(generation_env):
    from shortlist.server.assistant.budgets import AssistantBudget
    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant.operation_models import AssistantOperation

    env = generation_env
    _plan, receipt, job_id = _generation_operation(env)
    with env.state.sessions() as session:
        operation = session.get(AssistantOperation, receipt["operation_id"])
        operation.result = {**operation.result, "generation_stage": "external_started"}
        session.commit()
    assert dispatch_generation(env.state, {}, job_id=job_id)["status"] == "outcome_unknown"
    assert env.calls == [] and env.factories == []
    assert env.state.changes.get_operation(env.principal, receipt["operation_id"])["status"] == "outcome_unknown"
    with env.state.sessions() as session:
        assert session.get(AssistantBudget, env.principal.grant_id).provider_calls_reserved == 1


@pytest.mark.parametrize("changed", ["revocation", "settings"])
def test_changed_authority_or_settings_cancels_before_any_provider_io(generation_env, changed):
    from datetime import UTC, datetime

    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant_auth.models import AssistantGrant
    from shortlist.server.settings_store import SettingsStore

    env = generation_env
    _plan, _receipt, job_id = _generation_operation(env)
    with env.state.sessions() as session:
        if changed == "revocation":
            session.get(AssistantGrant, env.principal.grant_id).revoked_at = datetime.now(UTC)
        else:
            SettingsStore(session, env.state.secrets).set_in_transaction("curator.model", "changed-model")
        session.commit()
    assert dispatch_generation(env.state, {}, job_id=job_id)["status"] == "cancelled"
    assert env.calls == [] and env.factories == []


def test_owner_profile_provider_endpoint_change_cancels_queued_work_and_new_plan_uses_current_url(generation_env):
    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant.operation_models import AssistantChange
    from shortlist.server.assistant_auth import GrantConstraints, GrantPreset
    from shortlist.server.settings_store import SettingsStore

    env = generation_env
    repository = env.state.assistant_auth.repository
    profile = repository.create_grant(
        owner_account_id=42,
        client_id="profile-generation",
        name="Owner profile",
        preset=GrantPreset.OWNER_AUTOMATION,
        constraints=GrantConstraints(owner_managed=True, max_provider_calls=2),
    )
    assert profile.constraints.destination_ids == frozenset(
        {"https://api.openai.com/v1", "https://api.themoviedb.org/3"}
    )
    _positive, _receipt, positive_job = _generation_operation(env, principal=profile, key="profile-positive")
    assert dispatch_generation(env.state, {}, job_id=positive_job)["status"] == "completed"
    assert len(env.calls) == 1 and env.factories[0]["base_url"] == "https://api.openai.com/v1"
    old_plan, _receipt, job_id = _generation_operation(env, principal=profile, key="profile-stale")
    with env.state.sessions() as session:
        assert session.get(AssistantChange, old_plan["change_id"]).requirements["destination_ids"] == [
            "https://api.openai.com/v1"
        ]
    with env.state.sessions() as session:
        store = SettingsStore(session, env.state.secrets)
        store.set_in_transaction("curator.provider", "openai_compatible")
        store.set_in_transaction("curator.openai_base_url", "http://127.0.0.1:1234/v1")
        session.commit()
    current = repository.get_grant_context(profile.grant_id)
    assert "http://127.0.0.1:1234/v1" in current.constraints.destination_ids
    assert "https://api.openai.com/v1" not in current.constraints.destination_ids
    assert dispatch_generation(env.state, {}, job_id=job_id)["status"] == "cancelled"
    assert len(env.calls) == 1 and len(env.factories) == 1
    new_plan = env.state.changes.prepare(
        current, "generation", {"brief": "A different film set", "media": "movie", "max_output_tokens": 1200}
    )
    with env.state.sessions() as session:
        assert session.get(AssistantChange, new_plan["change_id"]).requirements["destination_ids"] == [
            "http://127.0.0.1:1234/v1"
        ]


def test_exact_approval_supplements_effect_permission_but_not_quota(generation_env):
    from dataclasses import replace

    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant_auth import Capability

    env = generation_env
    scoped = replace(env.principal, capabilities=env.principal.capabilities - {Capability.AI_GENERATE})
    _plan, receipt, job_id = _generation_operation(env, principal=scoped, approve=True)
    assert receipt["authorization_basis"] == "operation_approval"
    assert dispatch_generation(env.state, {}, job_id=job_id)["status"] == "completed"
    assert len(env.calls) == 1


def test_worker_preserves_original_token_scope(generation_env):
    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant.operation_models import AssistantOperation

    env = generation_env
    _plan, receipt, job_id = _generation_operation(env)
    with env.state.sessions() as session:
        operation = session.get(AssistantOperation, receipt["operation_id"])
        operation.result = {**operation.result, "_effective_capabilities": ["changes.prepare", "catalog.read"]}
        session.commit()
    assert dispatch_generation(env.state, {}, job_id=job_id)["status"] == "cancelled"
    assert env.calls == [] and env.factories == []


def test_new_plan_cannot_exceed_lifetime_quota_even_with_owner_approval(generation_env):
    from shortlist.server.assistant.budgets import AssistantBudget
    from shortlist.server.assistant.changes import ChangeError

    env = generation_env
    plan, first, _job_id = _generation_operation(env)
    assert env.state.changes.apply(env.principal, plan["change_id"], "retry-key") == first
    second = env.state.changes.prepare(env.principal, "generation", {"brief": "A different theme", "media": "movie"})
    env.state.changes.approve(second["change_id"], owner_account_id=42)
    with pytest.raises(ChangeError, match="quota"):
        env.state.changes.apply(env.principal, second["change_id"], "second-generation")
    with env.state.sessions() as session:
        assert session.get(AssistantBudget, env.principal.grant_id).provider_calls_reserved == 1


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openai_compatible", "google"])
def test_paid_dispatch_constructs_real_sdks_without_redirects_or_retries(provider):
    """Check SDK transport state, not just the arguments passed to a mocked factory."""
    from shortlist.engine.curator import make_curator

    curator = make_curator(
        provider,
        api_key="test-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
        follow_redirects=False,
    )
    client = curator._client
    try:
        if provider == "google":
            transport = client._api_client
            assert transport._http_options.retry_options.attempts == 1
            assert transport._httpx_client.follow_redirects is False
            assert transport._async_httpx_client.follow_redirects is False
        else:
            assert client.max_retries == 0
            assert client._client.follow_redirects is False
    finally:
        client.close()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openai_compatible", "google"])
def test_regular_provider_construction_keeps_existing_retry_policy(provider):
    from shortlist.engine.curator import make_curator

    curator = make_curator(provider, api_key="test-key", base_url="https://example.invalid/v1")
    client = curator._client
    try:
        if provider == "google":
            assert client._api_client._http_options.retry_options.attempts == 2
        else:
            assert client.max_retries == 2
    finally:
        client.close()


@pytest.mark.parametrize("checkpoint", ["missing", "expired"])
def test_a_lost_provider_reply_never_authorizes_another_paid_dispatch(generation_env, checkpoint):
    import time

    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant.operation_models import AssistantOperation
    from shortlist.server.db.models import CacheRow

    env = generation_env
    _plan, receipt, job_id = _generation_operation(env)
    with env.state.sessions() as session:
        operation = session.get(AssistantOperation, receipt["operation_id"])
        operation.result = {**operation.result, "generation_stage": "provider_returned"}
        if checkpoint == "expired":
            session.add(
                CacheRow(
                    kind="assistant_generation",
                    key=operation.id,
                    value={"reply": "discarded provider output", "tokens": 11},
                    expires_at=time.time() - 1,
                )
            )
        session.commit()
    assert dispatch_generation(env.state, {}, job_id=job_id)["status"] == "failed"
    assert env.calls == [] and env.factories == []


@pytest.mark.parametrize("change", ["scope_growth", "scope_reduction", "quota_reduction", "exact_revision"])
def test_dispatch_reauthorizes_standing_scope_and_quota_but_binds_exact_revision(generation_env, change):
    from shortlist.server.assistant.generation import dispatch_generation
    from shortlist.server.assistant_auth.models import AssistantGrant

    env = generation_env
    _plan, _receipt, job_id = _generation_operation(env, approve=change == "exact_revision")
    with env.state.sessions() as session:
        grant = session.get(AssistantGrant, env.principal.grant_id)
        grant.revision += 1
        if change == "scope_reduction":
            grant.capabilities = [cap for cap in grant.capabilities if cap != "ai.generate"]
        elif change == "quota_reduction":
            grant.constraints = {**grant.constraints, "max_provider_calls": 0}
        else:
            grant.constraints = {**grant.constraints, "row_ids": [99]}
        session.commit()
    result = dispatch_generation(env.state, {}, job_id=job_id)
    assert result["status"] == ("completed" if change == "scope_growth" else "cancelled")
    assert len(env.calls) == (1 if change == "scope_growth" else 0)
