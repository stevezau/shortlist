"""A saved-configuration run has role authority without a legacy lifetime allowance."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from shortlist.engine.provider_calls import ProviderCall
from shortlist.server.assistant.changes import ChangeError, ChangeService
from shortlist.server.assistant.run_adapter import ConfiguredRunAdapter, ConfiguredRunIntent
from shortlist.server.assistant.run_spend import RunSpendGuard
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset, StoredGrantIdentity
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.assistant_auth.repository import require_current_grant_in_session
from shortlist.server.db.models import Collection, Run, Server
from shortlist.server.settings_store import SettingsStore
from tests.conftest import make_profile
from tests.unit.test_assistant_runs import run_env as run_env


def _configured_run(
    state, *, role="manage", provider="openai", dry_run=True, images=False, acquisitions=False, historical_reserved=0
):
    from shortlist.server.assistant.budgets import AssistantBudget

    with state.sessions() as session:
        session.add(
            Server(id=1, machine_id="configured", url="http://fake.invalid", token_enc="fake", owner_account_id=42)
        )
        store = SettingsStore(session, state.secrets)
        store.set_in_transaction("curator.provider", provider)
        store.set_in_transaction("curator.api_key", "fake-key-never-sent")
        session.get(Collection, 1).candidate_sources = ["llm_web"]
        if images:
            session.get(Collection, 1).poster = {"mode": "ai"}
        if acquisitions:
            for key, value in {
                "requests.enabled": True,
                "requests.auto_send": True,
                "requests.max_per_run": 2,
                "requests.target": "arr",
                "requests.radarr.url": "http://radarr.test",
                "requests.radarr.apikey": "fake-request-key",
                "requests.radarr.quality_profile_id": 7,
                "requests.radarr.root_folder": "/movies",
                "tmdb.apikey": "fake-tmdb-key",
            }.items():
                store.set_in_transaction(key, value)
        session.add(
            AssistantGrant(
                id="configured-grant",
                owner_account_id=42,
                client_id="configured-client",
                name="Configured test",
                preset=GrantPreset.OWNER_AUTOMATION.value,
                capabilities=[cap.value for cap in Capability if cap != Capability.AI_GENERATE],
                constraints=GrantConstraints(
                    basic_access_v1=role,
                    owner_managed=True,
                    include_future_rows=True,
                    include_future_libraries=True,
                    max_provider_calls=0,
                ).as_dict(),
            )
        )
        if historical_reserved:
            session.add(AssistantBudget(grant_id="configured-grant", provider_calls_reserved=historical_reserved))
        session.commit()
        principal = require_current_grant_in_session(
            session, StoredGrantIdentity("configured-grant", 42, "configured-client", 1)
        )
    service = ChangeService(state.sessions, {"configured_run": ConfiguredRunAdapter(state)})
    change = service.prepare(principal, "configured_run", {"row_ids": [1], "person_ids": [1], "dry_run": dry_run})
    receipt = service.apply(principal, change["change_id"], "configured-once")
    run_id = receipt["result"]["run_id"]
    with state.sessions() as session:
        run = session.get(Run, run_id)
        run.status = "running"
        run.began_at = datetime.now(UTC)
        session.commit()
    return receipt, RunSpendGuard(state, run_id)


def test_configured_intent_rejects_every_paid_or_provider_override():
    saved = {"row_ids": [1], "person_ids": [1], "dry_run": True}
    assert ConfiguredRunIntent.model_validate(saved).model_dump()["include_shared"] is False
    for override in ("max_provider_calls", "max_output_tokens", "provider", "prompt", "url", "normal_run"):
        with pytest.raises(ValidationError):
            ConfiguredRunIntent.model_validate(saved | {override: 1})


def test_manage_configured_run_uses_saved_effects_without_spending_legacy_quota(run_env):
    from shortlist.server.assistant.budgets import AssistantBudget

    receipt, guard = _configured_run(run_env)
    with run_env.sessions() as session:
        run = session.get(Run, receipt["result"]["run_id"])
        assert run.stats["assistant_contract"]["policy"] == "configured_run_v1"
        assert run.stats["assistant_contract"]["intent"] == {
            "row_ids": [1],
            "person_ids": [1],
            "dry_run": True,
            "include_shared": False,
        }
        assert session.get(AssistantBudget, "configured-grant") is None
    call = ProviderCall(
        kind="native_search", provider="openai", destination="https://api.openai.com/v1", model="gpt-4o-mini"
    )
    with guard(call):
        pass
    with guard(call):
        pass
    with run_env.sessions() as session:
        assert session.get(AssistantBudget, "configured-grant") is None


def test_configured_context_sends_normal_provider_request_through_durable_guard(run_env, monkeypatch):
    from shortlist.engine.curator.openai import OpenAICurator
    from shortlist.server.assistant.budgets import AssistantBudget
    from shortlist.server.assistant.operation_models import AssistantRunCall

    receipt, _guard = _configured_run(run_env, historical_reserved=1)
    sdk = MagicMock()
    sdk.base_url = "https://api.openai.com/v1"
    sdk.responses.create.return_value = SimpleNamespace(output_text="[]", usage=None)

    def build_fake_context(**kwargs):
        config = run_env.run_service._ctx._engine_config(
            kwargs["session"],
            SettingsStore(kwargs["session"], run_env.secrets),
            dry_run=kwargs["dry_run"],
            collection_ids=kwargs["collection_ids"],
        )
        curator = OpenAICurator(api_key="fake-key-never-sent", provider_controls=kwargs["provider_controls"])
        curator._client = sdk
        return SimpleNamespace(config=config, curator=curator)

    monkeypatch.setattr(run_env.run_service, "build_context", build_fake_context)
    ctx, _profiles = run_env.run_service._build_assistant_context(
        receipt["result"]["run_id"], dry_run=True, loop=None, log_sink=None
    )
    assert ctx.curator.recommend_web(make_profile(), [], k=1) == []

    kwargs = sdk.responses.create.call_args.kwargs
    assert kwargs["tools"] == [{"type": "web_search", "search_context_size": "high"}]
    assert "max_output_tokens" not in kwargs
    assert "max_tool_calls" not in kwargs
    with run_env.sessions() as session:
        call = session.scalars(select(AssistantRunCall)).one()
        assert (call.run_id, call.kind, call.destination, call.status) == (
            receipt["result"]["run_id"],
            "native_search",
            "https://api.openai.com/v1",
            "returned",
        )
        assert session.get(AssistantBudget, "configured-grant").provider_calls_reserved == 1
        stored = session.get(AssistantGrant, "configured-grant")
        assert GrantConstraints.from_dict(stored.constraints).max_provider_calls == 0


def test_configured_image_and_acquisition_follow_saved_descriptors_and_per_run_cap(run_env):
    from dataclasses import replace

    from shortlist.engine.models import MediaType, MissingTitle, RequestOutcome
    from shortlist.engine.request_config import resolve_request_config
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch, AssistantRunCall

    receipt, guard = _configured_run(run_env, dry_run=False, images=True, acquisitions=True)
    with run_env.sessions() as session:
        run = session.get(Run, receipt["result"]["run_id"])
        contract = run.stats["assistant_contract"]
        assert contract["acquisition_limit"] == 2
        assert {effect["kind"] for effect in contract["spend"]["providers"]} == {"native_search", "image"}
        config = run_env.run_service._ctx._engine_config(
            session, SettingsStore(session, run_env.secrets), dry_run=False, collection_ids=[1]
        )
        cfg = resolve_request_config(config.requests, config.rows[0].request_overrides)
    image = ProviderCall(kind="image", provider="openai", destination="https://api.openai.com/v1", model="gpt-image-1")
    with guard(image):
        pass
    title = MissingTitle(777, "A film", MediaType.MOVIE, 2000, 8.5, 999)
    for candidate in (title, replace(title, tmdb_id=778)):
        with guard.acquisition("row-one", candidate, cfg) as record:
            assert record is not None
            record(RequestOutcome(candidate.tmdb_id, candidate.title, candidate.media_type, "requested"))
    with pytest.raises(ChangeError, match="budget"), guard.acquisition("row-one", replace(title, tmdb_id=779), cfg):
        pytest.fail("the saved acquisition cap was exceeded")
    with run_env.sessions() as session:
        assert len(list(session.scalars(select(AssistantRunCall)))) == 1
        assert len(list(session.scalars(select(AssistantRequestDispatch)))) == 2


@pytest.mark.parametrize("mutation", ["role", "revoke", "model"])
def test_configured_guard_rechecks_current_role_and_settings_before_each_effect(run_env, mutation):
    _receipt, guard = _configured_run(run_env)
    with run_env.sessions() as session:
        grant = session.get(AssistantGrant, "configured-grant")
        if mutation == "role":
            grant.constraints = {**grant.constraints, "basic_access_v1": "view"}
        elif mutation == "revoke":
            grant.revoked_at = datetime.now(UTC)
        else:
            SettingsStore(session, run_env.secrets).set_in_transaction("curator.model", "different-model")
        session.commit()
    call = ProviderCall(
        kind="native_search", provider="openai", destination="https://api.openai.com/v1", model="gpt-4o-mini"
    )
    with pytest.raises((ChangeError, PermissionError)), guard(call):
        pytest.fail("a changed grant or provider setting entered external I/O")


@pytest.mark.parametrize("mutation", ["token", "destination", "model"])
def test_configured_guard_keeps_token_ceiling_and_exact_provider_identity(run_env, mutation):
    receipt, guard = _configured_run(run_env)
    if mutation == "token":
        with run_env.sessions() as session:
            run = session.get(Run, receipt["result"]["run_id"])
            actor = dict(run.stats["assistant_actor"])
            actor["effective_capabilities"] = [
                capability
                for capability in actor["effective_capabilities"]
                if capability != Capability.HISTORY_PROVIDERS
            ]
            run.stats = {**run.stats, "assistant_actor": actor}
            session.commit()
    call = ProviderCall(
        kind="native_search",
        provider="openai",
        destination="https://elsewhere.invalid" if mutation == "destination" else "https://api.openai.com/v1",
        model="different-model" if mutation == "model" else "gpt-4o-mini",
    )
    with pytest.raises((ChangeError, PermissionError)), guard(call):
        pytest.fail("token scope or exact provider identity was bypassed")
