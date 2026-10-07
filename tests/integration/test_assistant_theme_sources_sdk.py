"""Theme sources survive MCP authoring and reach the shared engine within library scope."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shortlist.engine.clients.plex_pms import LibraryTitle
from shortlist.engine.models import MediaType
from shortlist.engine.themes import load_theme
from shortlist.server.assistant.theme_adapter import AssistantTheme
from shortlist.server.db.models import Theme
from shortlist.server.services.theme_store import spec_from_row
from tests.integration.test_assistant_mcp_tool_matrix import _apply, _approve_owner_change, _wire_app

pytestmark = pytest.mark.integration

TAGS = [{"id": 111, "name": "Time travel"}]
COLLECTIONS = [{"section_key": "1", "section_title": "Movies", "title": "Source collection"}]


def _call(wire, name: str, body: dict) -> dict:
    return wire.exercise([(name, body)])[name]["data"]


def test_sdk_theme_sources_create_read_runtime_preserve_and_clear(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        draft = {"name": "Source theme", "media": ["movie"], "tags": TAGS, "collections": COLLECTIONS}
        plan = _call(wire, "shortlist_plan_theme", {"action": "create", "draft": draft})
        assert plan["authorization"]["can_apply"] is True
        assert plan["summary"]["draft"]["tags"] == TAGS
        assert plan["summary"]["draft"]["collections"] == COLLECTIONS
        saved = _apply(wire, plan["change_id"], "theme-sources-create")
        theme_id = saved["result"]["theme_id"]
        result = _call(wire, "shortlist_get_theme", {"id": theme_id})
        assert result["tags"] == TAGS and result["collections"] == COLLECTIONS
        with app.state.sessions() as session:
            spec = spec_from_row(session.get(Theme, theme_id))

        # Only vendor boundaries are substituted: the persisted spec uses the real membership engine.
        calls = []
        tagged = {"id": 101, "title": "Keyword match", "release_date": "2000-01-01"}
        collected = {"id": 202, "title": "Collection member", "release_date": "2001-01-01"}

        def discover(media, params):
            calls.append(("discover", media, params))
            return [tagged] if media == MediaType.MOVIE else []

        def members(section_key, title):
            calls.append(("collection", section_key, title))
            return [LibraryTitle(202, MediaType.MOVIE, "Collection member", 2001)]

        def item(tmdb_id, media):
            calls.append(("item", tmdb_id, media))
            return collected

        titles = load_theme(
            SimpleNamespace(discover_all=discover, list_item=item),
            SimpleNamespace(collection_members=members),
            spec,
            {MediaType.MOVIE: {101: 1001, 202: 1002}},
        )
        assert titles.titles.ids[MediaType.MOVIE] == frozenset({101, 202})
        assert ("discover", MediaType.MOVIE, {"with_keywords": "111"}) in calls
        assert ("discover", MediaType.SHOW, {"with_keywords": "111"}) in calls
        assert ("collection", "1", "Source collection") in calls
        assert ("item", 202, MediaType.MOVIE) in calls
        assert titles.own == frozenset({(MediaType.MOVIE, 202)})

        # An older client omits these fields when editing the name; omission must not erase sources.
        update = {"action": "update", "theme_id": theme_id, "draft": {"name": "Renamed", "media": ["movie"]}}
        plan = _call(wire, "shortlist_plan_theme", update)
        _apply(wire, plan["change_id"], "theme-sources-preserve")
        preserved = _call(wire, "shortlist_get_theme", {"id": theme_id})
        assert preserved["name"] == "Renamed"
        assert preserved["tags"] == TAGS and preserved["collections"] == COLLECTIONS
        update["draft"].update(tags=[], collections=[], genres=["Drama"])
        plan = _call(wire, "shortlist_plan_theme", update)
        _apply(wire, plan["change_id"], "theme-sources-clear")
        cleared = _call(wire, "shortlist_get_theme", {"id": theme_id})
        assert cleared["tags"] == [] and cleared["collections"] == []
        assert cleared["revision"] != preserved["revision"]
        assert app.state.matrix_provider_calls == []


def test_sdk_setup_bundle_preserves_theme_sources(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        plan = _call(
            wire,
            "shortlist_plan_setup",
            {
                "theme": {"name": "Bundled sources", "media": ["movie"], "tags": TAGS, "collections": COLLECTIONS},
                "row": {
                    "action": "create",
                    "template_id": "describe-a-row",
                    "values": {
                        "name": "Bundled sources row",
                        "enabled": False,
                        "schedule": "",
                        "library_keys": ["1"],
                        "theme_mode": "fixed",
                        "ai_paused": True,
                    },
                },
            },
        )
        result = _apply(wire, plan["change_id"], "theme-sources-bundle")
        theme_id = result["result"]["theme_id"]
        saved = _call(wire, "shortlist_get_theme", {"id": theme_id})
        assert saved["tags"] == TAGS and saved["collections"] == COLLECTIONS
        assert app.state.matrix_provider_calls == []


def test_sdk_theme_update_rejects_changed_sources_before_apply(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, _app, _state):
        initial = {"name": "Reviewed sources", "media": ["movie"], "tags": TAGS, "collections": COLLECTIONS}
        plan = _call(wire, "shortlist_plan_theme", {"action": "update", "theme_id": 20, "draft": initial})
        _apply(wire, plan["change_id"], "theme-source-stale-initial")
        pending = _call(
            wire,
            "shortlist_plan_theme",
            {
                "action": "update",
                "theme_id": 20,
                "draft": {"name": "Pending rename", "media": ["movie"]},
            },
        )
        changed_tags = [{"id": 222, "name": "New source"}]
        changed = {**initial, "tags": changed_tags}
        concurrent = _call(wire, "shortlist_plan_theme", {"action": "update", "theme_id": 20, "draft": changed})
        _apply(wire, concurrent["change_id"], "theme-source-stale-concurrent")
        wire.expect_error(
            "shortlist_apply_change",
            {
                "change_id": pending["change_id"],
                "idempotency_key": "theme-source-stale-apply",
            },
            code="stale_plan",
        )
        saved = _call(wire, "shortlist_get_theme", {"id": 20})
        assert saved["name"] == "Reviewed sources"
        assert saved["tags"] == changed_tags and saved["collections"] == COLLECTIONS


@pytest.mark.parametrize("section_key", ["2", "unknown-section"])
def test_sdk_theme_collection_scope_denies_apply_and_redacts_after_exact_approval(tmp_path, monkeypatch, section_key):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        repository = app.state.assistant_auth.repository
        grant = repository.find_grant_for_client("mcp-wire-matrix")
        repository.replace_grant_authority(
            grant.grant_id,
            capabilities=grant.capabilities,
            expected_revision=grant.revision,
            constraints=replace(grant.constraints, include_future_libraries=False, library_keys=frozenset({"1"})),
        )
        collection = {"section_key": section_key, "section_title": "PRIVATE_LIBRARY", "title": "PRIVATE_COLLECTION"}
        plan = _call(
            wire,
            "shortlist_plan_theme",
            {
                "action": "create",
                "draft": {"name": "Scoped theme", "media": ["movie"], "tags": TAGS, "collections": [collection]},
            },
        )
        assert plan["authorization"]["can_apply"] is False
        assert "PRIVATE_" not in str(plan)
        wire.expect_error(
            "shortlist_apply_change",
            {
                "change_id": plan["change_id"],
                "idempotency_key": "theme-source-denied",
            },
            code="missing_permission",
        )
        _approve_owner_change(wire, app, plan["change_id"])
        # The exact approval permits saving this source but never expands standing library reads.
        _apply(wire, plan["change_id"], "theme-source-approved")
        listed = _call(wire, "shortlist_list_themes", {})
        theme_id = next(theme["id"] for theme in listed["items"] if theme["name"] == "Scoped theme")
        saved = _call(wire, "shortlist_get_theme", {"id": theme_id})
        assert saved["tags"] == TAGS
        assert "collections" not in saved
        assert "collections" in saved["redacted_fields"]
        assert "PRIVATE_" not in str(saved)
        current = repository.get_grant_context(grant.grant_id)
        assert current.constraints.library_keys == frozenset({"1"})
        assert current.constraints.include_future_libraries is False


@pytest.mark.parametrize(
    "fields",
    [
        {"tags": [{"id": "111", "name": "Tag"}]},
        {"tags": [{"id": 111, "name": "Tag", "api_key": "untrusted"}]},
        {"tags": TAGS * 21},
        {"collections": [{**COLLECTIONS[0], "section_key": 1}]},
        {"collections": [{**COLLECTIONS[0], "url": "https://unapproved.test"}]},
        {"collections": COLLECTIONS * 11},
    ],
)
def test_assistant_theme_sources_remain_strict_and_bounded(fields):
    with pytest.raises(ValidationError):
        AssistantTheme.model_validate({"name": "Invalid source", "media": ["movie"], **fields})
