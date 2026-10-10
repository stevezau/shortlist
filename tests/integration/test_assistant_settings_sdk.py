"""Every advertised writable setting survives real SDK plan/apply/read/reset.

Values are explicit, useful alternatives rather than synthesized copies of the
validator. Only upstream Plex/TMDB/provider boundaries use the shared fakes.
"""

from __future__ import annotations

import pytest

from shortlist.server.assistant.operation_models import AssistantChange
from shortlist.server.catalogs.models import ResetBehavior
from shortlist.server.catalogs.settings import get_settings_catalog
from shortlist.server.db.models import Setting
from shortlist.server.settings_store import SettingsStore
from tests.integration.test_assistant_mcp_tool_matrix import (
    _apply,
    _approve_owner_change,
    _wait_for_operation,
    _wire_app,
)

pytestmark = pytest.mark.integration

SETTING_VALUES = {
    "plex.orphan_confirm_delay_s": 0.1,
    "plex.timeout_s": 20,
    "plextv.throttle_s": 0.01,
    "curator.provider": "google",
    "curator.model": "synthetic-model",
    "row.name_template": "SDK {library_name} recommendations",
    "row.size": 12,
    "rows.manage_shelf_order": False,
    "candidates.sources": ["tmdb_discover"],
    "llm_web.search_provider": "exa",
    "llm_web.instructions": "Prefer thoughtful mysteries.",
    "exa.search_type": "instant",
    "recommendations.watched_pct": 0.5,
    "recommendations.refresh_days": 14,
    "recommendations.idle_hold_days": 21,
    "recommendations.recency": 0.7,
    "recommendations.genre_avoidance": 0.3,
    "recommendations.franchise": 0.4,
    "recommendations.cast": 0.2,
    "recommendations.recent_count": 8,
    "recommendations.max_seeds": 16,
    "recommendations.rating_source": "imdb",
    "recommendations.min_history": 5,
    "recommendations.cold_start": "skip",
    "recommendations.blocked_shared_seeds": [9001, 9002],
    "recommendations.use_plex_ratings": False,
    "recommendations.dislike_threshold": 3.0,
    "privacy.hide_shared_from_disabled": False,
    "requests.enabled": True,
    "requests.target": "overseerr",
    "requests.rating_source": "imdb",
    "requests.min_rating": 6.5,
    "requests.language_mode": "prefer",
    "requests.preferred_languages": ["en", "fr"],
    "requests.min_rating_other": 8.5,
    "requests.min_votes": 200,
    "requests.min_demand": 2,
    "requests.min_year": 1990,
    "requests.max_year": 2030,
    "requests.max_per_run": 3,
    "requests.auto_send": False,
    "requests.hold_genres": [99, 10402],
    "requests.hold_tags": {"156205": "concert film"},
    "requests.auto_min_demand": 4,
    "requests.auto_min_rating": 8.5,
    "requests.tag": "sdk-reviewed",
    "requests.auto_user_tag": True,
    "requests.overseerr.request_as_user_id": 12,
    "requests.radarr.quality_profile_id": 2,
    "requests.radarr.root_folder": "/synthetic/movies",
    "requests.sonarr.quality_profile_id": 3,
    "requests.sonarr.root_folder": "/synthetic/shows",
    "requests.sonarr.monitor": "firstSeason",
    "notify.webhook.enabled": True,
    "notify.webhook.events": ["run.finished", "privacy.exposure"],
    "notify.webhook.auth_header_name": "X-Synthetic-Authorization",
    "runs.retention": 6,
    "events.retention": 2,
    "sync.watch_full_days": 14,
    "backup.max_keep": 5,
    "jobs.max_parallel_readonly": 2,
    "run.concurrency": 2,
    "log.level": "INFO",
    "paused_all": True,
    "sync.watch_cron": "0 0 1 1 *",
    "sync.users_cron": "5 0 1 1 *",
    "backup.cron": "10 0 1 1 *",
    "privacy.sync_cron": "15 0 1 1 *",
    "rows.visibility_cron": "20 0 1 1 *",
    "sync.check_cron": "25 0 1 1 *",
    "maintenance.prune_cron": "30 0 1 1 *",
    "themes.rotate_cron": "35 0 1 1 *",
}


def test_the_roundtrip_cases_cover_exactly_every_advertised_writable_setting():
    writable = {item.key for item in get_settings_catalog() if item.assistant_writable}
    assert set(SETTING_VALUES) == writable
    assert len(writable) == 72


@pytest.mark.parametrize(
    "group", ["system", "recommendations", "row_defaults", "requests", "notifications", "schedules"]
)
def test_mcp_sdk_every_writable_setting_roundtrips_and_resets(tmp_path, monkeypatch, group):
    definitions = {item.key: item for item in get_settings_catalog() if item.group.value == group}
    values = {key: value for key, value in SETTING_VALUES.items() if key in definitions}
    with _wire_app(tmp_path, monkeypatch) as (wire, app, _state):
        app.state.scheduler.pause()
        for key, value in values.items():
            plan = wire.exercise([("shortlist_plan_configuration", {"values": {key: value}})])[
                "shortlist_plan_configuration"
            ]["data"]
            if not plan["authorization"]["can_apply"]:
                _approve_owner_change(wire, app, plan["change_id"])
            applied = _apply(wire, plan["change_id"], f"roundtrip-{key}")
            assert key in applied["result"]["changed_keys"], key
            read = wire.exercise([("shortlist_get_configuration", {"group": group})])["shortlist_get_configuration"][
                "data"
            ]["values"]
            assert read[key]["value"] == value, key
            with app.state.sessions() as session:
                assert SettingsStore(session, app.state.secrets).get(key) == value, key
                change = session.get(AssistantChange, plan["change_id"])
                assert change.intent["values"] == {key: value}
            if applied["job_ids"]:
                assert _wait_for_operation(wire, applied["operation_id"])["status"] == "completed", key

        resets = [key for key in values if definitions[key].reset_behavior == ResetBehavior.RESTORE_DEFAULT]
        nulls = {key: None for key in values if definitions[key].reset_behavior == ResetBehavior.LITERAL_NULL}
        assert set(resets) | set(nulls) == set(values)
        reset = wire.exercise([("shortlist_plan_configuration", {"resets": resets, "values": nulls})])[
            "shortlist_plan_configuration"
        ]["data"]
        if not reset["authorization"]["can_apply"]:
            _approve_owner_change(wire, app, reset["change_id"])
        reset_result = _apply(wire, reset["change_id"], f"reset-{group}")
        assert set(reset_result["result"]["changed_keys"]) == set(values)
        read = wire.exercise([("shortlist_get_configuration", {"group": group})])["shortlist_get_configuration"][
            "data"
        ]["values"]
        with app.state.sessions() as session:
            for key in values:
                assert read[key]["value"] == definitions[key].default, key
                if key in resets:
                    assert session.get(Setting, key) is None, key
                else:
                    assert SettingsStore(session, app.state.secrets).get(key) is None, key
        assert app.state.matrix_provider_calls == []


@pytest.mark.parametrize("key", ["candidates.sources", "notify.webhook.events"])
def test_mcp_sdk_catalogued_list_options_are_accepted_and_unknown_members_are_atomic(tmp_path, monkeypatch, key):
    with _wire_app(tmp_path, monkeypatch) as (wire, app, _state):
        plan = wire.exercise([("shortlist_plan_configuration", {"values": {key: SETTING_VALUES[key]}})])[
            "shortlist_plan_configuration"
        ]["data"]
        if not plan["authorization"]["can_apply"]:
            _approve_owner_change(wire, app, plan["change_id"])
        _apply(wire, plan["change_id"], f"list-option-{key}")
        for invalid in ([*SETTING_VALUES[key], "not-a-supported-choice"], "tmdb_discover", [1], None):
            wire.expect_error("shortlist_plan_configuration", {"values": {key: invalid, "row.size": 20}})
        with app.state.sessions() as session:
            store = SettingsStore(session, app.state.secrets)
            assert store.get(key) == SETTING_VALUES[key]
            assert store.get("row.size") == 15
        empty = wire.exercise([("shortlist_plan_configuration", {"values": {key: []}})])[
            "shortlist_plan_configuration"
        ]["data"]
        if not empty["authorization"]["can_apply"]:
            _approve_owner_change(wire, app, empty["change_id"])
        _apply(wire, empty["change_id"], f"empty-list-{key}")
        with app.state.sessions() as session:
            assert SettingsStore(session, app.state.secrets).get(key) == []


def test_sdk_browser_only_settings_and_mixed_invalid_changes_cannot_persist(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch) as (wire, app, _state):
        with app.state.sessions() as session:
            before = {row.key: row.value for row in session.query(Setting)}
            change_count = session.query(AssistantChange).count()
        browser_only = [item for item in get_settings_catalog() if not item.assistant_writable]
        assert len(browser_only) == 26
        for item in browser_only:
            wire.expect_error("shortlist_plan_configuration", {"values": {item.key: item.default, "row.size": 20}})
            wire.expect_error("shortlist_plan_configuration", {"resets": [item.key]})
        for values in (
            {"row.size": True},
            {"row.size": 41},
            {"requests.target": "unknown"},
            {"recommendations.max_seeds": 16, "unknown.setting": 1},
        ):
            wire.expect_error("shortlist_plan_configuration", {"values": values})
        with app.state.sessions() as session:
            assert {row.key: row.value for row in session.query(Setting)} == before
            assert session.query(AssistantChange).count() == change_count


def test_sdk_configuration_staleness_and_resource_denial_preserve_the_saved_values(tmp_path, monkeypatch):
    from tests.integration.test_assistant_sdk_boundaries import _constrained_wire

    with _wire_app(tmp_path, monkeypatch) as (wire, app, _state):
        stale = wire.exercise([("shortlist_plan_configuration", {"values": {"row.size": 20}})])[
            "shortlist_plan_configuration"
        ]["data"]
        current = wire.exercise([("shortlist_plan_configuration", {"values": {"row.size": 12}})])[
            "shortlist_plan_configuration"
        ]["data"]
        applied = _apply(wire, current["change_id"], "configuration-concurrent-edit")
        assert applied["result"]["changed_keys"] == ["row.size"]
        wire.expect_error(
            "shortlist_apply_change",
            {"change_id": stale["change_id"], "idempotency_key": "configuration-stale-negative"},
            code="stale_plan",
        )
        limited = _constrained_wire(wire, app)
        limited.expect_error("shortlist_get_configuration", {"group": "row_defaults"}, code="missing_permission")
        proposed = limited.exercise([("shortlist_plan_configuration", {"values": {"row.size": 25}})])[
            "shortlist_plan_configuration"
        ]["data"]
        assert not proposed["authorization"]["can_apply"]
        limited.expect_error(
            "shortlist_apply_change",
            {"change_id": proposed["change_id"], "idempotency_key": "configuration-scope-negative"},
            code="missing_permission",
        )
        with app.state.sessions() as session:
            assert SettingsStore(session, app.state.secrets).get("row.size") == 12
