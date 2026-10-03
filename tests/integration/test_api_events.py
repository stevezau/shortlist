"""API contract tests: the audit feed at `GET /api/events/log` — its shape, its filters, its paging.

`/api/events` (no `/log`) is the endless SSE stream and is not exercised here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.server.services.audit import PLEX_WRITE_SCOPES, add_audit

pytestmark = pytest.mark.integration


def _seed(client: TestClient, *scopes: str) -> None:
    """One audit row per scope, in the order given — so the LAST scope is the newest."""
    with client.app.state.sessions() as session:
        for scope in scopes:
            add_audit(session, scope, "info", slug="default")
        session.commit()


def _scopes(client: TestClient, **params) -> list[str]:
    resp = client.get("/api/events/log", params=params)
    assert resp.status_code == 200, resp.text
    return [entry["scope"] for entry in resp.json()]


class TestAuditLogShape:
    def test_each_entry_carries_exactly_id_ts_level_scope_and_message(self, client: TestClient):
        with client.app.state.sessions() as session:
            add_audit(session, "run.user", "warning", run_id=7, user="sarah", dry_run=False)
            session.commit()

        (entry,) = client.get("/api/events/log").json()

        assert set(entry) == {"id", "ts", "level", "scope", "message"}
        assert isinstance(entry["id"], int)
        assert entry["ts"].endswith("+00:00")  # UTC with an explicit offset, or browsers read it as local
        assert entry["level"] == "warning"
        assert entry["scope"] == "run.user"
        assert entry["message"] == {"run_id": 7, "user": "sarah", "dry_run": False}

    def test_the_openapi_schema_publishes_the_entry_model_and_both_new_params(self, client: TestClient):
        """The SPA generates its types from this; an untyped `list[dict]` left it hand-writing them."""
        schema = client.app.openapi()
        op = schema["paths"]["/api/events/log"]["get"]

        items = op["responses"]["200"]["content"]["application/json"]["schema"]["items"]
        assert items == {"$ref": "#/components/schemas/EventOut"}
        fields = schema["components"]["schemas"]["EventOut"]["properties"]
        assert set(fields) == {"id", "ts", "level", "scope", "message"}
        params = {p["name"] for p in op["parameters"]}
        assert params == {"scope", "scope_prefix", "plex_writes", "limit", "before_id"}


class TestScopePrefix:
    def test_scope_prefix_returns_only_scopes_that_start_with_it(self, client: TestClient):
        _seed(client, "run.user", "settings.change", "prerun.user", "run.shared")

        assert _scopes(client, scope_prefix="run.") == ["run.shared", "run.user"]

    def test_an_underscore_in_the_prefix_is_a_literal_not_a_wildcard(self, client: TestClient):
        # SQL LIKE reads `_` as "any one character", so an unescaped `run.hub_` also matches `run.hubs`.
        _seed(client, "run.hub_order", "run.hubs", "run.hub_unplaced")

        assert _scopes(client, scope_prefix="run.hub_") == ["run.hub_unplaced", "run.hub_order"]

    def test_scope_prefix_and_an_exact_scope_both_apply(self, client: TestClient):
        _seed(client, "run.user", "run.shared", "settings.change")

        assert _scopes(client, scope_prefix="run.", scope="run.user") == ["run.user"]
        assert _scopes(client, scope_prefix="run.", scope="settings.change") == []


class TestPlexWrites:
    NOT_PLEX_WRITES = ("settings.change", "support.read", "run.started", "run.finished", "api_token.create")

    def test_plex_writes_returns_every_plex_write_scope_and_nothing_else(self, client: TestClient):
        _seed(client, *self.NOT_PLEX_WRITES, *sorted(PLEX_WRITE_SCOPES))

        returned = _scopes(client, plex_writes=True, limit=1000)

        assert set(returned) == PLEX_WRITE_SCOPES
        assert len(returned) == len(PLEX_WRITE_SCOPES)
        # Pinned by name too, so emptying the constant cannot pass this test on its own.
        assert {"run.user", "run.privacy_sync", "privacy.restriction_restored", "system.uninstall"} <= set(returned)

    def test_plex_writes_off_is_the_default_and_filters_nothing(self, client: TestClient):
        _seed(client, "settings.change", "run.user", "support.read")

        assert _scopes(client) == ["support.read", "run.user", "settings.change"]
        assert _scopes(client, plex_writes=False) == ["support.read", "run.user", "settings.change"]

    def test_plex_writes_and_scope_prefix_both_apply(self, client: TestClient):
        _seed(client, "run.user", "run.started", "user.pause.hide", "run.sweep")

        assert _scopes(client, plex_writes=True, scope_prefix="run.") == ["run.sweep", "run.user"]


class TestPaging:
    def test_before_id_pages_backwards_through_a_filtered_feed_without_gaps_or_repeats(self, client: TestClient):
        _seed(
            client,
            "run.user",
            "settings.change",
            "run.shared",
            "run.sweep",
            "support.read",
            "run.demote",
            "run.requests",
        )
        everything = client.get("/api/events/log", params={"plex_writes": True}).json()

        pages: list[dict] = []
        before_id = None
        while True:
            params = {"plex_writes": True, "limit": 2}
            if before_id is not None:
                params["before_id"] = before_id
            page = client.get("/api/events/log", params=params).json()
            if not page:
                break
            assert len(page) <= 2
            pages.extend(page)
            before_id = page[-1]["id"]

        assert pages == everything
        assert [e["scope"] for e in pages] == ["run.requests", "run.demote", "run.sweep", "run.shared", "run.user"]

    def test_limit_outside_1_to_1000_is_refused(self, client: TestClient):
        assert client.get("/api/events/log", params={"limit": 0}).status_code == 422
        assert client.get("/api/events/log", params={"limit": 1001}).status_code == 422
