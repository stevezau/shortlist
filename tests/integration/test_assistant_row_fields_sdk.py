"""Explicit, compatible SDK writes for every advertised row field."""

from __future__ import annotations

from copy import deepcopy

import pytest

from shortlist.server.catalogs.templates import ROW_FIELD_DEFINITIONS
from shortlist.server.db.models import Collection, CollectionAudience
from tests.integration.test_assistant_mcp_tool_matrix import (
    _apply,
    _approve_owner_change,
    _wait_for_operation,
    _wire_app,
)

pytestmark = pytest.mark.integration

BASE = {
    "build": "per_person",
    "audience": "subset",
    "audience_user_ids": [],
    "enabled": False,
    "schedule": "",
    "media": "movie",
    "library_keys": ["1"],
    "size": 11,
}

ROW_CASES = {
    "appearance": {
        "sort_order": 7,
        "name_template": "SDK {library_name} picks",
        "fallback_name": "SDK discoveries",
        "description": "Selected for {user} in {library_name}.",
        "sort_title_prefix": "!007_",
        "request_tag": "sdk-row",
        "candidate_sources": ["tmdb_discover"],
        "pick_order": "newest",
        "placement": "library",
        "placement_friends": "home",
        "show_days": [1, 3, 5],
        "pin_top": True,
        "hub_anchor": {"1": {"anchor": "", "row": "matrix-row", "before": True, "top": False, "enabled": True}},
        "poster": {"mode": "text", "title": "SDK picks", "subtitle": "For {user}", "style": "Warm orange"},
        "ai_instructions": {"mode": "own", "text": "Recommend thoughtful mysteries."},
    },
    "ranking": {
        "watched_pct": 0.2,
        "refresh_days": 14,
        "idle_hold_days": 21,
        "recency": 0.8,
        "recent_count": 6,
        "max_seeds": 17,
        "max_runtime": 130,
        "min_year": 1990,
        "max_year": 2025,
        "min_rating": 7.1,
        "cold_start": "skip",
        "seed_window": 3,
    },
    "request_limits": {
        "req_min_rating": 7.5,
        "req_min_votes": 150,
        "req_min_demand": 2,
        "req_min_year": 1980,
        "req_max_year": 2025,
        "req_auto_send": False,
        "req_auto_min_demand": 3,
        "req_auto_min_rating": 8.0,
        "req_max_per_row": 4,
        "req_radarr_quality_profile_id": 2,
        "req_radarr_root_folder": "/synthetic/movies",
        "req_sonarr_quality_profile_id": 3,
        "req_sonarr_root_folder": "/synthetic/shows",
        "req_sonarr_monitor": "firstSeason",
        "req_language_mode": "prefer",
        "req_preferred_languages": ["en", "fr"],
        "req_min_rating_other": 9.0,
        "req_auto_user_tag": True,
    },
    "rewatch": {"rewatch": True, "rewatch_cooldown_days": 60},
    "unstarted": {"media": "show", "library_keys": ["2"], "unstarted_only": True},
    "requested_titles": {"requests_row": True, "requests_window_days": 30, "requests_tag_pattern": "req-{username}"},
    "seasonal": {"seasons": ["halloween"], "season_lead_days": 20, "season_after_days": 3},
    "shared": {"build": "shared", "min_watchers": 3},
    "theme": {
        "theme_id": 20,
        "theme_mode": "explore",
        "explore_brief": "Quiet journeys.",
        "theme_days": 10,
        "refresh_share": 0.5,
        "repeat_cooldown_days": 45,
        "avoid_rows": ["matrix-row"],
        "ai_paused": True,
    },
    # Activation is an isolated explicit branch, still unscheduled until separately requested.
    "activation": {"enabled": True, "audience_user_ids": [1], "schedule": "0 0 1 1 *"},
}

NULL_FIELDS = {
    "watched_pct",
    "refresh_days",
    "idle_hold_days",
    "recency",
    "recent_count",
    "max_seeds",
    "max_runtime",
    "min_year",
    "max_year",
    "min_rating",
    "cold_start",
    "req_min_rating",
    "req_min_votes",
    "req_min_demand",
    "req_min_year",
    "req_max_year",
    "req_auto_send",
    "req_auto_min_demand",
    "req_auto_min_rating",
    "req_max_per_row",
    "req_radarr_quality_profile_id",
    "req_radarr_root_folder",
    "req_sonarr_quality_profile_id",
    "req_sonarr_root_folder",
    "req_sonarr_monitor",
    "req_language_mode",
    "req_preferred_languages",
    "req_min_rating_other",
    "req_auto_user_tag",
    "theme_days",
    "refresh_share",
    "repeat_cooldown_days",
    "avoid_rows",
}


def _call(wire, name, request):
    return wire.exercise([(name, request)])[name]["data"]


def _save(wire, app, request, key):
    plan = _call(wire, "shortlist_plan_row", request)
    if not plan["authorization"]["can_apply"]:
        _approve_owner_change(wire, app, plan["change_id"])
    result = _apply(wire, plan["change_id"], key)
    if result["job_ids"]:
        assert _wait_for_operation(wire, result["operation_id"])["status"] == "completed"
    return result


def _assert_readback(wire, app, row_id, expected):
    read = _call(wire, "shortlist_get_row", {"id": row_id})
    with app.state.sessions() as session:
        row = session.get(Collection, row_id)
        for key, value in expected.items():
            actual = read["audience_user_ids"] if key == "audience_user_ids" else read["fields"][key]
            assert actual == value, (key, actual, value)
            if hasattr(row, key):
                assert getattr(row, key) == value, key
            elif key == "audience_user_ids":
                assert (
                    sorted(entry.user_id for entry in session.query(CollectionAudience).filter_by(collection_id=row_id))
                    == value
                )
        assert read["redacted_fields"] == {}
    return read


def test_row_field_cases_cover_the_entire_advertised_writable_contract():
    covered = set(BASE) | {"name"} | set().union(*(set(case) for case in ROW_CASES.values()))
    assert covered == set(ROW_FIELD_DEFINITIONS)
    assert len(covered) == 72
    assert not {"id", "slug", "dry_run", "defer_rename", "poster_upload"} & covered


@pytest.mark.parametrize("case", ROW_CASES)
def test_sdk_each_row_field_persists_with_compatible_dependencies_and_null_resets(tmp_path, monkeypatch, case):
    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        app.state.scheduler.pause()
        collections_before = deepcopy(state.collections)
        created = _save(
            wire,
            app,
            {
                "action": "create",
                "template_id": "picked-for-you",
                "values": {**BASE, "name": f"SDK {case} before"},
            },
            f"row-fields-create-{case}",
        )
        row_id = created["result"]["row_id"]
        _assert_readback(wire, app, row_id, {**BASE, "name": f"SDK {case} before"})
        values = {"name": f"SDK {case} after", **ROW_CASES[case]}
        _save(wire, app, {"action": "update", "row_id": row_id, "values": values}, f"row-fields-update-{case}")
        expected = {**BASE, **values}
        _assert_readback(wire, app, row_id, expected)
        nulls = {key: None for key in values if key in NULL_FIELDS}
        if nulls:
            _save(wire, app, {"action": "update", "row_id": row_id, "values": nulls}, f"row-fields-reset-{case}")
            _assert_readback(wire, app, row_id, {**expected, **nulls})
        if case == "request_limits":
            # Explicitly clearing languages must stay distinguishable from inheriting them.
            _save(
                wire,
                app,
                {"action": "update", "row_id": row_id, "values": {"req_preferred_languages": []}},
                "row-fields-empty-languages",
            )
            _assert_readback(wire, app, row_id, {**expected, **nulls, "req_preferred_languages": []})
        if case == "appearance":
            inherited = {
                "candidate_sources": [],
                "show_days": [],
                "hub_anchor": {},
                "poster": {"mode": "", "title": "", "subtitle": "", "style": ""},
                "ai_instructions": {"mode": "default", "text": ""},
            }
            _save(wire, app, {"action": "update", "row_id": row_id, "values": inherited}, "row-fields-inherit")
            _assert_readback(wire, app, row_id, {**expected, **inherited})
        # No run was requested: activation and configuration convergence cannot make recommendations.
        assert state.collections == collections_before
        assert app.state.matrix_provider_calls == []


def test_sdk_row_invalid_dependencies_and_owner_only_controls_are_atomic(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch) as (wire, app, _state):
        created = _save(
            wire,
            app,
            {"action": "create", "template_id": "picked-for-you", "values": {**BASE, "name": "SDK invalid controls"}},
            "row-controls-create",
        )
        row_id = created["result"]["row_id"]
        before = _call(wire, "shortlist_get_row", {"id": row_id})
        for values in (
            {"id": 999},
            {"slug": "reassign-identity"},
            {"defer_rename": True},
            {"dry_run": True},
            {"poster_upload": "arbitrary-bytes"},
            {"size": 41},
            {"min_year": 2025, "max_year": 1990},
            {"unstarted_only": True},
            {"rewatch": True, "requests_row": True},
            {"theme_mode": "explore"},
            {"req_preferred_languages": ["not-a-language"]},
            {"candidate_sources": ["unregistered-source"]},
            {"ai_paused": "true"},
        ):
            wire.expect_error(
                "shortlist_plan_row", {"action": "update", "row_id": row_id, "values": {"sort_order": 55, **values}}
            )
        after = _call(wire, "shortlist_get_row", {"id": row_id})
        assert after == before
