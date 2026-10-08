"""Actual outbound run checkpoints retain money/privacy limits across failures and races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from shortlist.engine.provider_calls import ProviderCall
from shortlist.server.assistant.changes import ChangeError, ChangeService
from shortlist.server.assistant.run_adapter import RunAdapter
from shortlist.server.assistant.run_spend import RunSpendGuard
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset, StoredGrantIdentity
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.assistant_auth.repository import require_current_grant_in_session
from shortlist.server.db.models import Collection, Run, Server
from shortlist.server.settings_store import SettingsStore
from tests.unit.test_assistant_runs import run_env as run_env


def _paid_run(
    state, *, calls=2, images=0, approved=False, dry_run=True, acquisitions=0, provider="openai", managed=False
):
    with state.sessions() as session:
        session.add(Server(id=1, machine_id="paid", url="http://fake.invalid", token_enc="fake", owner_account_id=42))
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
                "requests.target": "arr",
                "requests.radarr.url": "http://radarr.test",
                "requests.radarr.apikey": "fake-request-key",
                "requests.radarr.quality_profile_id": 7,
                "requests.radarr.root_folder": "/movies",
                "tmdb.apikey": "fake-tmdb-key",
            }.items():
                store.set_in_transaction(key, value)
        capabilities = [cap.value for cap in Capability]
        if approved:
            capabilities.remove(Capability.AI_GENERATE.value)
        session.add(
            AssistantGrant(
                id="paid-grant",
                owner_account_id=42,
                client_id="paid-client",
                name="Paid test",
                preset=GrantPreset.OWNER_AUTOMATION.value,
                capabilities=capabilities,
                constraints=GrantConstraints(
                    row_ids=frozenset({1}),
                    person_ids=frozenset({1}),
                    library_keys=frozenset({"1"}),
                    include_future_libraries=True,
                    destination_ids=frozenset(
                        {"https://api.openai.com/v1", "https://generativelanguage.googleapis.com", "http://radarr.test"}
                    ),
                    max_provider_calls=calls,
                ).as_dict(),
            )
        )
        session.commit()
        principal = require_current_grant_in_session(session, StoredGrantIdentity("paid-grant", 42, "paid-client", 1))
    service = ChangeService(state.sessions, {"run": RunAdapter(state)})
    intent = {
        "row_ids": [1],
        "person_ids": [1],
        "dry_run": dry_run,
        "max_provider_calls": calls,
        "max_acquisitions": acquisitions,
        "allow_provider_managed_search": managed,
        "max_images": images,
        "max_output_tokens": 512,
        "max_native_tool_uses": 1,
    }
    change = service.prepare(principal, "run", intent)
    if approved:
        service.approve(change["change_id"], owner_account_id=42)
    receipt = service.apply(principal, change["change_id"], "paid-once")
    run_id = receipt["result"]["run_id"]
    with state.sessions() as session:
        run = session.get(Run, run_id)
        run.status = "running"
        run.began_at = datetime.now(UTC)
        session.commit()
    return principal, service, receipt, RunSpendGuard(state, run_id)


def _call(**kwargs):
    values = dict(
        kind="native_search",
        provider="openai",
        destination="https://api.openai.com/v1",
        model="gpt-4o-mini",
        output_tokens=512,
        native_tool_uses=1,
    )
    return ProviderCall(**(values | kwargs))


def test_provider_checkpoint_commits_before_io_and_enforces_aggregate_limit(run_env):
    from shortlist.server.assistant.run_spend import AssistantRunCall

    _principal, _service, receipt, guard = _paid_run(run_env, calls=1)
    # A different session sees the checkpoint while network work would be running.
    with guard(_call()), run_env.sessions() as session:
        claim = session.scalars(select(AssistantRunCall)).one()
        assert claim.status == "external_started"
        assert claim.operation_id == receipt["operation_id"]
    with pytest.raises(ChangeError, match="budget"), guard(_call()):
        pytest.fail("second paid call entered")
    with run_env.sessions() as session:
        assert session.scalars(select(AssistantRunCall)).one().status == "returned"


def test_unknown_call_blocks_further_spend_and_cannot_be_hidden_by_engine_success(run_env):
    principal, service, receipt, guard = _paid_run(run_env)
    with pytest.raises(TimeoutError), guard(_call()):
        raise TimeoutError("provider could have charged")
    with pytest.raises(ChangeError), guard(_call()):
        pytest.fail("new paid call entered after unknown result")
    with run_env.sessions() as session:
        run = session.get(Run, receipt["result"]["run_id"])
        run.status = "ok"  # provider facade swallowed the error and engine used a fallback
        session.commit()
    assert service.get_operation(principal, receipt["operation_id"])["status"] == "outcome_unknown"
    from shortlist.server.assistant.monitoring import MonitoringService

    assert MonitoringService(run_env).runs(principal).data["items"][0]["status"] == "outcome_unknown"


@pytest.mark.parametrize("mutation", ["revoke", "owner", "scope", "quota", "settings", "disabled"])
def test_every_outbound_call_rechecks_live_authority_before_io(run_env, mutation):
    _principal, _service, _receipt, guard = _paid_run(run_env)
    with run_env.sessions() as session:
        grant = session.get(AssistantGrant, "paid-grant")
        if mutation == "revoke":
            grant.revoked_at = datetime.now(UTC)
        elif mutation == "owner":
            session.get(Server, 1).owner_account_id = 99
        elif mutation == "scope":
            grant.capabilities = [cap for cap in grant.capabilities if cap != Capability.AI_GENERATE.value]
        elif mutation == "quota":
            grant.constraints = {**grant.constraints, "max_provider_calls": 0}
        elif mutation == "settings":
            SettingsStore(session, run_env.secrets).set_in_transaction("curator.model", "different-model")
        elif mutation == "disabled":
            run_env.assistant_auth = None
        session.commit()
    with pytest.raises((ChangeError, PermissionError)), guard(_call()):
        pytest.fail("unauthorized provider call entered")


@pytest.mark.parametrize(
    "change",
    [
        {"model": "expensive-other-model"},
        {"destination": "https://evil.invalid"},
        {"output_tokens": 513},
        {"native_tool_uses": 2},
        {"kind": "image"},
    ],
)
def test_runtime_descriptor_cannot_exceed_the_exact_contract(run_env, change):
    _principal, _service, _receipt, guard = _paid_run(run_env)
    with pytest.raises(ChangeError), guard(_call(**change)):
        pytest.fail("unplanned provider effect entered")


def test_parallel_calls_cannot_overrun_one_remaining_reservation(run_env):
    _principal, _service, _receipt, guard = _paid_run(run_env, calls=1)

    def call():
        try:
            with guard(_call()):
                return "entered"
        except ChangeError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: call(), range(2))) == ["denied", "entered"]


def test_exact_owner_approval_permits_the_approved_call_but_not_later_revision(run_env):
    _principal, _service, _receipt, guard = _paid_run(run_env, approved=True)
    with guard(_call()):
        pass
    with run_env.sessions() as session:
        session.get(AssistantGrant, "paid-grant").revision += 1
        session.commit()
    with pytest.raises((ChangeError, PermissionError)), guard(_call()):
        pytest.fail("stale exact approval entered")


def _request(run_env):
    from shortlist.engine.models import MediaType, MissingTitle
    from shortlist.engine.request_config import resolve_request_config

    with run_env.sessions() as session:
        config = run_env.run_service._ctx._engine_config(
            session, SettingsStore(session, run_env.secrets), dry_run=False, collection_ids=[1]
        )
        cfg = resolve_request_config(config.requests, config.rows[0].request_overrides)
    return cfg, MissingTitle(777, "A film", MediaType.MOVIE, 2000, 8.5, 999)


def test_acquisition_count_is_atomic_with_global_claim_and_blocks_other_origins(run_env):
    from shortlist.engine.models import RequestOutcome
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch
    from shortlist.server.services.request_actions import AutomaticRequestGuard

    _principal, _service, receipt, guard = _paid_run(run_env, dry_run=False, acquisitions=1)
    cfg, title = _request(run_env)
    with guard.acquisition("row-one", title, cfg) as record:
        assert record is not None
        with run_env.sessions() as session:
            claim = session.scalars(select(AssistantRequestDispatch)).one()
            assert claim.origin == "assistant_run" and claim.operation_id == receipt["operation_id"]
            assert "fake-request-key" not in str(claim.request_body)
        with AutomaticRequestGuard(run_env.sessions)("row-one", title, cfg) as other:
            assert other is None
        record(RequestOutcome(title.tmdb_id, title.title, title.media_type, "requested"))
    from dataclasses import replace

    with pytest.raises(ChangeError, match="budget"), guard.acquisition("row-one", replace(title, tmdb_id=778), cfg):
        pytest.fail("acquisition budget overrun")


def test_existing_global_claim_does_not_consume_run_acquisition_allowance(run_env):
    from dataclasses import replace

    from shortlist.engine.models import RequestOutcome
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch
    from shortlist.server.services.request_actions import AutomaticRequestGuard

    _principal, _service, _receipt, guard = _paid_run(run_env, dry_run=False, acquisitions=1)
    cfg, title = _request(run_env)
    with AutomaticRequestGuard(run_env.sessions)("row-one", title, cfg) as record:
        record(RequestOutcome(title.tmdb_id, title.title, title.media_type, "requested"))
    with guard.acquisition("row-one", title, cfg) as blocked:
        assert blocked is None
    other = replace(title, tmdb_id=778)
    with guard.acquisition("row-one", other, cfg) as record:
        assert record is not None
        record(RequestOutcome(other.tmdb_id, other.title, other.media_type, "requested"))
    with run_env.sessions() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(AssistantRequestDispatch).where(AssistantRequestDispatch.origin == "assistant_run")
                    )
                )
            )
            == 1
        )


def test_acquisition_unknown_stops_subsequent_provider_calls(run_env):
    principal, service, receipt, guard = _paid_run(run_env, dry_run=False, acquisitions=1)
    cfg, title = _request(run_env)
    with pytest.raises(TimeoutError), guard.acquisition("row-one", title, cfg):
        raise TimeoutError("could have accepted the request")
    with pytest.raises(ChangeError), guard(_call()):
        pytest.fail("provider spend after uncertain acquisition")
    assert service.get_operation(principal, receipt["operation_id"])["status"] == "outcome_unknown"


def test_image_allowance_is_separate_from_total_requests(run_env):
    _principal, _service, _receipt, guard = _paid_run(run_env, calls=3, images=1, dry_run=False)
    call = _call(kind="image", model="gpt-image-1", output_tokens=None, native_tool_uses=None)
    with guard(call):
        pass
    with pytest.raises(ChangeError, match="image budget"), guard(call):
        pytest.fail("second image entered")


def test_google_native_requires_exact_approval_even_for_broad_grant(run_env):
    with pytest.raises(ChangeError, match="approval"):
        _paid_run(run_env, provider="google", managed=True)


def test_google_native_exact_approval_preserves_uncapped_internal_search_disclosure(run_env):
    _principal, _service, receipt, guard = _paid_run(run_env, provider="google", managed=True, approved=True)
    from shortlist.server.assistant.operation_models import AssistantChange

    with run_env.sessions() as session:
        change = session.get(AssistantChange, receipt["change_id"])
        assert "cannot cap" in change.summary["provider_managed_search_notice"]
    with guard(
        _call(
            provider="google",
            destination="https://generativelanguage.googleapis.com",
            model="gemini-flash-latest",
            native_tool_uses=None,
        )
    ):
        pass


def test_startup_marks_interrupted_provider_call_unknown_without_replay(run_env):
    from shortlist.server.assistant.run_spend import AssistantRunCall, recover_assistant_run_calls

    principal, service, receipt, _guard = _paid_run(run_env)
    with run_env.sessions() as session:
        from dataclasses import asdict

        session.add(
            AssistantRunCall(
                run_id=receipt["result"]["run_id"], operation_id=receipt["operation_id"], **asdict(_call())
            )
        )
        session.commit()
    assert recover_assistant_run_calls(run_env.sessions) == 1
    assert recover_assistant_run_calls(run_env.sessions) == 0
    assert service.get_operation(principal, receipt["operation_id"])["status"] == "outcome_unknown"


def test_public_run_and_preview_accept_only_saved_selectors_while_legacy_intent_keeps_its_limits():
    from pydantic import ValidationError

    from shortlist.server.assistant.run_adapter import ConfiguredRunIntent, RunIntent, RunLimits
    from shortlist.server.assistant.tools import PreviewRowInput

    assert set(ConfiguredRunIntent.model_json_schema()["properties"]) == {
        "row_ids",
        "person_ids",
        "dry_run",
        "include_shared",
    }
    assert set(PreviewRowInput.model_json_schema()["properties"]) == {"row_id", "person_ids", "include_shared"}
    preview = {"row_id": 1, "person_ids": [1]}
    for field in RunLimits.model_fields:
        with pytest.raises(ValidationError):
            PreviewRowInput.model_validate({**preview, field: getattr(RunLimits(), field)})
    assert set(RunLimits.model_fields) <= set(RunIntent.model_fields)
    assert (
        RunIntent.model_validate(
            {"row_ids": [1], "person_ids": [1], "dry_run": True, "max_provider_calls": 1}
        ).max_provider_calls
        == 1
    )


def test_frozen_context_receives_live_guards_before_any_network_client_is_used(run_env, monkeypatch):
    from types import SimpleNamespace

    _principal, _service, receipt, _guard = _paid_run(run_env)

    def builder(**kwargs):
        session = kwargs["session"]
        config = run_env.run_service._ctx._engine_config(
            session, SettingsStore(session, run_env.secrets), dry_run=True, collection_ids=[1]
        )
        return SimpleNamespace(
            config=config, provider_controls=kwargs["provider_controls"], acquisition_guard=kwargs["acquisition_guard"]
        )

    monkeypatch.setattr(run_env.run_service, "build_context", builder)
    ctx, _profiles = run_env.run_service._build_assistant_context(
        receipt["result"]["run_id"], dry_run=True, loop=None, log_sink=None
    )
    assert isinstance(ctx.provider_controls.guard, RunSpendGuard)
    assert ctx.provider_controls.max_output_tokens == 512
    assert ctx.acquisition_guard.__self__ is ctx.provider_controls.guard
    with ctx.provider_controls.guard(_call()):
        pass


def test_apply_replay_never_reserves_again_and_a_new_plan_cannot_bypass_lifetime_quota(run_env):
    from shortlist.server.assistant.budgets import AssistantBudget

    principal, service, receipt, _guard = _paid_run(run_env, calls=1)
    assert service.apply(principal, receipt["change_id"], "second-key") == receipt
    other = service.prepare(
        principal,
        "run",
        {
            "row_ids": [1],
            "person_ids": [1],
            "dry_run": True,
            "max_provider_calls": 1,
        },
    )
    with pytest.raises(ChangeError, match="quota"):
        service.apply(principal, other["change_id"], "new-plan")
    with run_env.sessions() as session:
        assert session.get(AssistantBudget, principal.grant_id).provider_calls_reserved == 1
        assert len(list(session.scalars(select(Run)))) == 1


@pytest.mark.parametrize("derived", ["cold_start", "history_depth"])
def test_runtime_person_progress_does_not_invalidate_approved_remaining_calls(run_env, derived):
    from shortlist.server.db.models import User

    _principal, _service, _receipt, guard = _paid_run(run_env)
    with guard(_call()):
        pass
    # These fields are written by persist_user_live/reconcile_watched during the same run.
    with run_env.sessions() as session:
        person = session.get(User, 1)
        if derived == "cold_start":
            person.cold_start = True
        else:
            person.prefs = {**person.prefs, "history_depth": 42}
        session.commit()
    with guard(_call()):
        pass


def test_real_person_preference_changes_still_invalidate_remaining_calls(run_env):
    from shortlist.server.db.models import User

    _principal, _service, _receipt, guard = _paid_run(run_env)
    with run_env.sessions() as session:
        person = session.get(User, 1)
        person.prefs = {**person.prefs, "excluded_genres": ["Horror"]}
        session.commit()
    with pytest.raises(ChangeError, match="changed"), guard(_call()):
        pytest.fail("a changed preference cannot reuse the frozen run context")


def test_failed_result_checkpoint_stops_more_calls_and_terminal_receipt_is_unknown(run_env, monkeypatch):
    principal, service, receipt, guard = _paid_run(run_env)
    original = guard._finish_provider

    def failed_checkpoint(*args, **kwargs):
        raise OSError("the provider returned but the database checkpoint failed")

    monkeypatch.setattr(guard, "_finish_provider", failed_checkpoint)
    with pytest.raises(OSError), guard(_call()):
        pass
    monkeypatch.setattr(guard, "_finish_provider", original)
    # Even if progress persistence reports a successful engine fallback, the unfinished ledger wins.
    with run_env.sessions() as session:
        session.get(Run, receipt["result"]["run_id"]).status = "ok"
        session.commit()
    assert service.get_operation(principal, receipt["operation_id"])["status"] == "outcome_unknown"
    with run_env.sessions() as session:
        session.get(Run, receipt["result"]["run_id"]).status = "running"
        session.commit()
    with pytest.raises(ChangeError), guard(_call()):
        pytest.fail("paid work continued after a failed result checkpoint")
