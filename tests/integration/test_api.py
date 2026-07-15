"""API contract tests — full app via TestClient (real lifespan, tmp SQLite, forged owner session)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server, User
from shortlist.server.main import create_app

pytestmark = pytest.mark.integration

OWNER_ID = 555000001


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as test_client:
        # Link a server so owner checks are active, and add users.
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="m1",
                    url="http://pms:32400",
                    token_enc="x",
                    owner_account_id=OWNER_ID,
                    plex_pass=True,
                    capabilities={},
                )
            )
            session.add(User(plex_account_id=555000100, username="sarah", slug="sarah", enabled=True))
            session.add(User(plex_account_id=555000200, username="mike", slug="mike"))
            session.commit()
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ID, "username": "owner"})
        test_client.cookies.set(SESSION_COOKIE, cookie)
        test_client.headers[CSRF_HEADER] = "1"
        yield test_client


class TestAuthBoundary:
    def test_health_needs_no_auth(self, client: TestClient):
        fresh = TestClient(client.app)
        with fresh:
            r = fresh.get("/api/system/health")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

    def test_users_requires_session(self, client: TestClient):
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/users").status_code == 401

    def test_non_owner_session_rejected_everywhere(self, client: TestClient):
        """A session issued during the pre-link window must lose access once an owner exists."""
        cookie = session_serializer(client.app.state.session_secret).dumps({"account_id": 999, "username": "intruder"})
        client.cookies.set(SESSION_COOKIE, cookie)
        assert client.get("/api/users").status_code == 403
        assert client.get("/api/runs").status_code == 403
        assert client.get("/api/settings").status_code == 403
        assert client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).status_code == 403
        assert client.get("/api/setup/state").status_code == 403

    def test_mutations_require_csrf_header(self, client: TestClient):
        del client.headers[CSRF_HEADER]
        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
        r = client.patch(f"/api/users/{user_id}", json={"enabled": True})
        assert r.status_code == 403
        assert CSRF_HEADER in r.json()["detail"]


class TestUsersApi:
    def test_list_and_patch(self, client: TestClient):
        users = client.get("/api/users").json()
        assert [u["username"] for u in users] == ["mike", "sarah"]
        target = next(u for u in users if u["username"] == "mike")
        # NB: `row_size`/`max_rating` used to live in prefs and were read by NOTHING. Per-person row
        # size is a row override (PUT /users/{id}/rows/{cid}); a maturity cap filtered no content at
        # all, so it was removed rather than left looking enforced.
        r = client.patch(f"/api/users/{target['id']}", json={"enabled": True, "prefs": {"excluded_genres": ["Horror"]}})
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert r.json()["prefs"]["excluded_genres"] == ["Horror"]

    def test_patch_unknown_user_404(self, client: TestClient):
        assert client.patch("/api/users/9999", json={"enabled": True}).status_code == 404

    def test_patch_prompt_prefs_persist(self, client: TestClient):
        users = client.get("/api/users").json()
        target = next(u for u in users if u["username"] == "sarah")
        r = client.patch(
            f"/api/users/{target['id']}",
            json={"prefs": {"prompt_tone": "cinephile", "prompt_guidance": "she loves slow burns"}},
        )
        assert r.status_code == 200
        prefs = r.json()["prefs"]
        assert prefs["prompt_tone"] == "cinephile"
        assert prefs["prompt_guidance"] == "she loves slow burns"


class TestUserRowsApi:
    def _sarah_id(self, client: TestClient) -> int:
        return next(u["id"] for u in client.get("/api/users").json() if u["slug"] == "sarah")

    def test_rows_lists_the_default_row_with_no_picks_yet(self, client: TestClient):
        uid = self._sarah_id(client)
        rows = client.get(f"/api/users/{uid}/rows").json()
        assert [r["slug"] for r in rows] == ["picked"]
        assert rows[0]["is_default"] is True
        assert rows[0]["muted"] is False
        assert rows[0]["picks"] == []

    def test_override_mute_and_resize_round_trip(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        r = client.put(f"/api/users/{uid}/rows/{cid}", json={"muted": True, "row_size": 20})
        assert r.status_code == 200
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert row["muted"] is True
        assert row["override"]["row_size"] == 20

    def test_size_override_can_be_reset_to_default(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["row_size"] == 20
        # Sending an explicit null (the "Default" choice) must clear it, not be ignored.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": None})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["row_size"] is None

    def test_mute_toggle_preserves_a_saved_size(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        # A mute toggle sends only {muted}; the saved size must survive it.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"muted": True})
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert row["muted"] is True
        assert row["override"]["row_size"] == 20

    def test_override_curation_recipe_persists_and_clears(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"prompt_tone": "playful"})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["prompt_tone"] == "playful"
        # An all-blank recipe clears it back to inheriting the row's own.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"prompt_tone": "", "prompt_guidance": ""})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["prompt_tone"] == ""

    def test_override_on_unknown_user_or_row_404(self, client: TestClient):
        assert client.put("/api/users/9999/rows/1", json={"muted": True}).status_code == 404
        uid = self._sarah_id(client)
        assert client.put(f"/api/users/{uid}/rows/9999", json={"muted": True}).status_code == 404

    def test_runs_empty_then_unknown_user_404(self, client: TestClient):
        uid = self._sarah_id(client)
        assert client.get(f"/api/users/{uid}/runs").json() == []
        assert client.get("/api/users/9999/runs").status_code == 404


class TestRunsApi:
    def test_empty_list_then_trigger(self, client: TestClient):
        assert client.get("/api/runs").json() == []
        r = client.post("/api/runs", json={"dry_run": True})
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        # The run fails fast (Plex unconfigured) but must exist with a terminal/queued status.
        for _ in range(50):
            runs = client.get("/api/runs").json()
            if runs and runs[0]["status"] in ("error", "ok"):
                break
            time.sleep(0.05)
        assert runs[0]["id"] == run_id
        assert runs[0]["status"] == "error"  # no plex configured in this app instance
        detail = client.get(f"/api/runs/{run_id}")
        assert detail.status_code == 200

    def test_unknown_run_404(self, client: TestClient):
        assert client.get("/api/runs/424242").status_code == 404

    def test_run_log_endpoint_returns_a_list(self, client: TestClient):
        # A run whose process never buffered a log returns an empty list, not a 404 — the page seeds
        # its activity feed from this and tops it up over SSE.
        r = client.get("/api/runs/424242/log")
        assert r.status_code == 200 and r.json() == []


class TestSettingsValidation:
    """PUT /api/settings validated the KEY but never the VALUE, so any non-UI client could push a
    value the engine then choked on — or, worse, one that quietly disabled a safety rule."""

    def test_a_throttle_below_one_write_per_second_is_refused(self, client: TestClient):
        # plex-safety rule 6: <=1 write/s to plex.tv. 0 removed the throttle entirely.
        r = client.put("/api/settings", json={"values": {"plextv.throttle_s": 0}})
        assert r.status_code == 422
        assert "plextv.throttle_s" in r.json()["detail"]
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": 2.5}}).status_code == 200

    def test_a_non_numeric_row_size_is_refused(self, client: TestClient):
        # "abc" was stored happily, then raised ValueError inside every run and 500'd two endpoints.
        assert client.put("/api/settings", json={"values": {"row.size": "abc"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"row.size": 99}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"row.size": 20}}).status_code == 200

    def test_paused_all_must_be_a_real_boolean(self, client: TestClient):
        # A non-empty string is truthy in Python, so "false" PAUSED every run while the UI read "off".
        assert client.put("/api/settings", json={"values": {"paused_all": "false"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"paused_all": False}}).status_code == 200

    def test_an_unknown_candidate_source_is_refused(self, client: TestClient):
        # POST /collections already rejected this; the global key accepted it.
        r = client.put("/api/settings", json={"values": {"candidates.sources": ["totally_bogus"]}})
        assert r.status_code == 422
        ok = client.put("/api/settings", json={"values": {"candidates.sources": ["trakt", "tmdb_similar"]}})
        assert ok.status_code == 200

    def test_watched_cap_is_validated(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"recommendations.watched_pct": 1.5}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.watched_pct": 0.25}}).status_code == 200
        assert client.get("/api/settings").json()["recommendations.watched_pct"] == 0.25

    def test_an_unknown_curator_provider_is_refused(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"curator.provider": "bogus"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"curator.provider": "none"}}).status_code == 200

    def test_log_level_is_validated_and_applied(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"log.level": "LOUD"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"log.level": "DEBUG"}}).status_code == 200
        assert client.get("/api/settings").json()["log.level"] == "DEBUG"

    def test_a_valid_settings_payload_still_saves(self, client: TestClient):
        r = client.put(
            "/api/settings",
            json={"values": {"row.size": 15, "requests.min_rating": 7.5, "requests.max_per_run": 5}},
        )
        assert r.status_code == 200


class TestSettingsApi:
    def test_get_put_round_trip_and_unknown_key_rejected(self, client: TestClient):
        settings = client.get("/api/settings").json()
        assert settings["row.size"] == 15
        r = client.put("/api/settings", json={"values": {"row.size": 20}})
        assert r.status_code == 200
        assert r.json()["row.size"] == 20
        assert client.put("/api/settings", json={"values": {"evil.key": 1}}).status_code == 422

    def test_secret_set_then_redacted_and_placeholder_roundtrip_keeps_value(self, client: TestClient):
        client.put("/api/settings", json={"values": {"tmdb.apikey": "abc123"}})
        # tmdb.apikey isn't a SECRET_KEY; use the plex token which is.
        client.put("/api/settings", json={"values": {"plex.token": "real-token"}})
        settings = client.get("/api/settings").json()
        assert settings["plex.token"] == "•••••"
        client.put("/api/settings", json={"values": {"plex.token": "•••••"}})  # UI round-trip
        with client.app.state.sessions() as session:
            from shortlist.server.settings_store import SettingsStore

            assert SettingsStore(session, client.app.state.secrets).get("plex.token") == "real-token"

    def test_prompt_settings_round_trip(self, client: TestClient):
        r = client.put(
            "/api/settings",
            json={"values": {"curator.prompt_tone": "warm", "curator.prompt_guidance": "house style"}},
        )
        assert r.status_code == 200
        assert r.json()["curator.prompt_tone"] == "warm"
        assert r.json()["curator.prompt_guidance"] == "house style"

    def test_prompt_preview_reflects_the_recipe(self, client: TestClient):
        r = client.post("/api/settings/prompt-preview", json={"tone": "cinephile", "guidance": "Prefer noir."})
        assert r.status_code == 200
        system = r.json()["system"]
        assert "Prefer noir." in system
        assert "film buff" in system  # cinephile tone clause
        assert "Use only tmdb_id values from the candidate list" in system  # contract always present
        shared = client.post("/api/settings/prompt-preview", json={"shared": True}).json()
        assert "popular on this server" in shared["system"].lower()


class _FakeStore:
    """Minimal SettingsStore stand-in: .get(key) returns the value or None."""

    def __init__(self, values: dict):
        self._values = values

    def get(self, key: str):
        return self._values.get(key)


class TestResolvePrompt:
    """The global-vs-per-person recipe merge (ContextBuilder._resolve_prompt), cell by cell."""

    def _resolve(self, glob: dict, prefs: dict):
        from shortlist.server.services.context_builder import ContextBuilder

        return ContextBuilder._resolve_prompt(_FakeStore(glob), prefs)

    def test_defaults_when_nothing_is_set(self):
        cfg = self._resolve({}, {})
        assert (cfg.tone, cfg.guidance, cfg.template) == ("balanced", "", "")

    def test_user_tone_overrides_global(self):
        cfg = self._resolve({"curator.prompt_tone": "warm"}, {"prompt_tone": "cinephile"})
        assert cfg.tone == "cinephile"

    def test_empty_user_tone_inherits_global(self):
        cfg = self._resolve({"curator.prompt_tone": "warm"}, {"prompt_tone": ""})
        assert cfg.tone == "warm"

    def test_guidance_is_additive_global_then_user(self):
        cfg = self._resolve(
            {"curator.prompt_guidance": "house rule"},
            {"prompt_guidance": "note for this person"},
        )
        assert cfg.guidance == "house rule\nnote for this person"

    def test_guidance_skips_blank_parts(self):
        assert self._resolve({"curator.prompt_guidance": "only global"}, {}).guidance == "only global"
        assert self._resolve({}, {"prompt_guidance": "only user"}).guidance == "only user"

    def test_template_user_wins_else_global(self):
        assert self._resolve({"curator.prompt_template": "G"}, {}).template == "G"
        assert self._resolve({"curator.prompt_template": "G"}, {"prompt_template": "U"}).template == "U"


class TestCollectionsSeed:
    def test_migration_seeds_the_default_picked_row(self, client: TestClient):
        """Upgrade must be behaviour-neutral: exactly one per-person 'picked' row for everyone."""
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            rows = session.query(Collection).all()
            assert len(rows) == 1
            row = rows[0]
            assert (row.slug, row.build, row.audience, row.enabled) == (
                "picked",
                "per_person",
                "everyone",
                True,
            )

    def test_default_row_size_and_name_follow_the_global_setting(self, client: TestClient, tmp_path):
        """The wizard/Settings set row.size and row.name_template; the default 'picked' row must
        deliver at those values, not a size frozen into the collection at migration time."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        client.put("/api/settings", json={"values": {"row.size": 10}})
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        picked = next(spec for spec in specs if spec.slug == "picked")
        assert picked.size == 10  # follows the setting, not the collection's seeded 15
        assert picked.name_template == ""  # falls through to the global row name

    def test_per_row_watched_pct_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Rewatch Row", "watched_pct": 0.5})
        assert created.status_code == 201 and created.json()["watched_pct"] == 0.5
        # Out of the 0..1 range is rejected.
        assert client.post("/api/collections", json={"name": "X", "watched_pct": 2.0}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "rewatch_row").watched_pct == 0.5

    def test_per_row_freshness_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Fresh Row", "freshness": 0.75})
        assert created.status_code == 201 and created.json()["freshness"] == 0.75
        # Out of the 0..1 range is rejected.
        assert client.post("/api/collections", json={"name": "X", "freshness": 1.5}).status_code == 422
        # And the global freshness setting is range-checked too.
        assert client.put("/api/settings", json={"values": {"recommendations.freshness": 2.0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.freshness": 0.3}}).status_code == 200

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "fresh_row").freshness == 0.75

    def test_per_row_placement_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Top Row", "placement": "library", "pin_top": True})
        assert created.status_code == 201
        assert created.json()["placement"] == "library" and created.json()["pin_top"] is True
        # An unknown placement is rejected.
        assert client.post("/api/collections", json={"name": "X", "placement": "bogus"}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == "top_row")
        assert spec.placement == "library" and spec.pin_top is True
        assert spec.show_library and not spec.show_home  # library-only

    def test_a_disabled_row_becomes_a_retired_row_for_cleanup(self, client: TestClient):
        """A row switched off is not delivered (dropped from _build_rows) AND handed to the engine as
        a retired row, so its lingering collection is removed from its owner's Home on the next run."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        client.patch(f"/api/collections/{cid}", json={"name": "Hidden Gems", "enabled": False})

        from shortlist.server.settings_store import SettingsStore

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            retired = builder._retired_rows(session, store)
            built = builder._build_rows(session, store)

        assert "hidden_gems" not in {s.slug for s in built}  # not delivered
        assert "hidden_gems" in {s.slug for s in retired}  # but queued for removal
        assert next(s for s in retired if s.slug == "hidden_gems").name_template == "Hidden Gems"

    def test_a_disabled_dynamic_title_row_is_not_retired(self, client: TestClient):
        """A {top_seed} title renders to the DEFAULT row's title when there are no picks, and all of a
        user's per-person rows share one label (told apart by title only). Retiring such a row would
        match and DELETE the user's live default row — so it must be skipped, not queued for removal."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Because You Watched"})
        cid = created.json()["id"]
        # Give it a dynamic title, then disable it.
        client.patch(
            f"/api/collections/{cid}",
            json={"name": "Because You Watched", "name_template": "Because you watched {top_seed}", "enabled": False},
        )

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        assert "because_you_watched" not in {s.slug for s in retired}, "a dynamic-title row must not be auto-removed"

    def test_a_disabled_whitespace_title_row_is_not_retired(self, client: TestClient):
        """A whitespace-only template also renders to the DEFAULT title (strip -> empty), so it would
        collide with the live default row just like {top_seed}. The guard tests the RENDERED title,
        not a substring, so this must be skipped too."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Blankish"})
        cid = created.json()["id"]
        client.patch(f"/api/collections/{cid}", json={"name": "Blankish", "name_template": "   ", "enabled": False})

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        assert "blankish" not in {s.slug for s in retired}, "a whitespace-title row must not be auto-removed"

    def test_default_row_prompt_by_build(self, client: TestClient):
        """The default row's style comes from global Settings — but HOW it gets there differs by
        build, and the shared cell used to fall through to a bare default (ignoring Settings).

        per_person: spec.prompt stays None so each user's own resolved prompt (global + their
        overrides) wins. shared: there is no user profile to inherit from, so the global recipe
        must be passed explicitly.
        """
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        client.put("/api/settings", json={"values": {"curator.prompt_tone": "cinephile"}})
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())

        def picked_spec():
            with client.app.state.sessions() as session:
                specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
            return next(spec for spec in specs if spec.slug == "picked")

        assert picked_spec().prompt is None  # per_person: the per-user prompt wins downstream

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        client.patch(f"/api/collections/{picked['id']}", json={"name": picked["name"], "build": "shared"})

        prompt = picked_spec().prompt
        assert prompt is not None, "a shared default row must carry the global recipe, not a bare default"
        assert prompt.tone == "cinephile"
        assert prompt.shared is True


class TestCollectionsApi:
    def test_list_starts_with_the_seeded_default(self, client: TestClient):
        cols = client.get("/api/collections").json()
        assert [c["slug"] for c in cols] == ["picked"]

    def test_create_update_delete_per_person(self, client: TestClient):
        created = client.post(
            "/api/collections",
            json={"name": "Hidden Gems", "size": 10, "prompt": {"tone": "cinephile"}},
        )
        assert created.status_code == 201
        cid = created.json()["id"]
        assert created.json()["slug"] == "hidden_gems"
        assert created.json()["build"] == "per_person"
        assert created.json()["prompt"]["tone"] == "cinephile"

        updated = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Hidden Gems", "size": 20, "enabled": False, "prompt": {"tone": "warm"}},
        )
        assert updated.status_code == 200
        assert updated.json()["size"] == 20 and updated.json()["enabled"] is False

        assert client.delete(f"/api/collections/{cid}").status_code == 204
        assert [c["slug"] for c in client.get("/api/collections").json()] == ["picked"]

    def test_shared_collection_with_subset_audience(self, client: TestClient):
        users = client.get("/api/users").json()
        ids = [u["id"] for u in users]
        created = client.post(
            "/api/collections",
            json={
                "name": "Staff Picks",
                "build": "shared",
                "audience": "subset",
                "audience_user_ids": ids,
                "min_watchers": 3,
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["build"] == "shared"
        assert sorted(body["audience_user_ids"]) == sorted(ids)
        assert body["min_watchers"] == 3

    def test_default_picked_cannot_be_deleted(self, client: TestClient):
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        assert client.delete(f"/api/collections/{picked['id']}").status_code == 422

    def test_validation_rejects_bad_enums(self, client: TestClient):
        assert client.post("/api/collections", json={"name": "X", "build": "nonsense"}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "media": "vinyl"}).status_code == 422

    def test_candidate_sources_round_trip_and_reject_unknown(self, client: TestClient):
        # Empty by default (inherit the global setting).
        created = client.post("/api/collections", json={"name": "Trakt Row"})
        assert created.status_code == 201 and created.json()["candidate_sources"] == []
        cid = created.json()["id"]
        # A per-row override round-trips through PATCH and GET (the client sends the full body).
        patched = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Trakt Row", "candidate_sources": ["trakt", "tmdb_discover"]},
        )
        assert patched.status_code == 200
        assert patched.json()["candidate_sources"] == ["trakt", "tmdb_discover"]
        # An unknown source id is rejected with a helpful 422, not silently stored.
        bad = client.post("/api/collections", json={"name": "Bad Row", "candidate_sources": ["imdb_magic"]})
        assert bad.status_code == 422

    def test_library_keys_round_trip(self, client: TestClient):
        # Empty by default (every library); a per-row selection round-trips as strings.
        created = client.post("/api/collections", json={"name": "4K Only"})
        assert created.status_code == 201 and created.json()["library_keys"] == []
        cid = created.json()["id"]
        patched = client.patch(f"/api/collections/{cid}", json={"name": "4K Only", "library_keys": ["3", "5"]})
        assert patched.status_code == 200
        assert patched.json()["library_keys"] == ["3", "5"]

    def test_a_custom_row_inherits_the_global_curation_style(self, client: TestClient):
        """Settings -> Curation style said it wrote "everyone's rows". It wrote exactly one: the
        default. Every custom row got a bare `balanced` recipe that beat the global one downstream."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        client.put(
            "/api/settings",
            json={"values": {"curator.prompt_tone": "cinephile", "curator.prompt_guidance": "no horror"}},
        )
        client.post("/api/collections", json={"name": "Hidden Gems"})
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        gems = next(spec for spec in specs if spec.slug == "hidden_gems")

        assert gems.prompt.tone == "cinephile"  # inherited, not a bare "balanced"
        assert "no horror" in gems.prompt.guidance

    def test_a_rows_own_style_still_beats_the_global_one(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        client.put("/api/settings", json={"values": {"curator.prompt_tone": "cinephile"}})
        created = client.post(
            "/api/collections",
            json={"name": "Family Night", "prompt": {"tone": "playful", "guidance": "keep it light", "template": ""}},
        )
        assert created.status_code == 201
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        row = next(spec for spec in specs if spec.slug == "family_night")

        assert row.prompt.tone == "playful"  # the row's own choice wins
        assert "keep it light" in row.prompt.guidance

    def test_default_row_never_stores_a_prompt_the_engine_would_ignore(self, client: TestClient):
        # The default row is curated with the GLOBAL recipe (ContextBuilder passes prompt=None for
        # it), so a per-row prompt here would save cleanly and then do nothing. The API blanks it.
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        patched = client.patch(
            f"/api/collections/{picked['id']}",
            json={"name": picked["name"], "prompt": {"tone": "cinephile", "guidance": "x", "template": "y"}},
        )
        assert patched.status_code == 200
        assert patched.json()["prompt"] == {}
        assert client.get("/api/collections").json()[0]["prompt"] == {}

    def test_other_rows_keep_their_own_prompt(self, client: TestClient):
        # The other cell of the same branch: every non-default row's recipe IS honoured, so it must
        # persist exactly as sent.
        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        recipe = {"tone": "cinephile", "guidance": "deep cuts", "template": ""}
        patched = client.patch(f"/api/collections/{cid}", json={"name": "Hidden Gems", "prompt": recipe})
        assert patched.status_code == 200
        assert patched.json()["prompt"] == recipe

    def test_slug_collision_gets_suffixed(self, client: TestClient):
        # Different names (duplicates are rejected) that slugify to the same base collide on slug.
        first = client.post("/api/collections", json={"name": "Date Night"}).json()
        second = client.post("/api/collections", json={"name": "Date-Night!"}).json()
        assert first["slug"] == "date_night"
        assert second["slug"] == "date_night_2"

    def test_duplicate_names_are_rejected(self, client: TestClient):
        assert client.post("/api/collections", json={"name": "Movie Night"}).status_code == 201
        assert client.post("/api/collections", json={"name": "Movie Night"}).status_code == 422


class TestPrivacyApi:
    def test_status_empty(self, client: TestClient):
        r = client.get("/api/privacy/status").json()
        assert r == {"last_check": None, "passed": None, "tiers": {}}

    def test_snapshots_empty(self, client: TestClient):
        assert client.get("/api/privacy/snapshots").json() == []


class TestEventsApi:
    def test_audit_log_empty(self, client: TestClient):
        assert client.get("/api/events/log").json() == []


class TestSetupApi:
    def test_wizard_state_round_trip(self, client: TestClient):
        r = client.get("/api/setup/state").json()
        assert r["completed"] is False
        client.put("/api/setup/state", json={"step": 3, "state": {"picked": [1, 2]}, "completed": False})
        r = client.get("/api/setup/state").json()
        assert r["step"] == 3
        assert r["state"] == {"picked": [1, 2]}


class TestUninstall:
    def test_wrong_confirmation_rejected(self, client: TestClient):
        r = client.post("/api/system/uninstall", json={"confirm": "yes"})
        assert r.status_code == 422
