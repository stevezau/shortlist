"""API contract tests: settings validation, the settings API, settings that trigger real Plex work,
and repointing at another server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.server.db.models import User
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration


class TestSettingsValidation:
    """PUT /api/settings validated the KEY but never the VALUE, so any non-UI client could push a
    value the engine then choked on — or, worse, one that quietly disabled a safety rule."""

    def test_the_plextv_throttle_floor_accepts_zero_and_rejects_out_of_range(self, client: TestClient):
        # `plextv.throttle_s` is now the FLOOR (min seconds) between plex.tv writes: 0 = as fast as
        # plex.tv accepts, safe because the client backs off adaptively on a 429 (rule 6). So 0 is
        # valid now — it's no longer an "off switch". Out-of-range values are still refused.
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": 0}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": 2.5}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": -1}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": 61}}).status_code == 422

    def test_a_bad_plex_timeout_is_refused(self, client: TestClient):
        # It's read unguarded as int(...) in build_context, so a bad stored value would crash every run.
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": 45}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": "abc"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": 0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": 301}}).status_code == 422

    def test_the_dislike_threshold_is_capped_below_the_top_of_the_scale(self, client: TestClient):
        """The cap is the whole reason this validator exists. `userRating` runs 0..10, so a naive
        0..10 bound would let someone set 10 — at which point EVERY rated title counts as disliked
        and every rating anyone ever gave stops seeding. 6 (three stars) is the last value where
        "disliked" is still a fair reading of what the person meant."""
        assert client.put("/api/settings", json={"values": {"recommendations.dislike_threshold": 0}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"recommendations.dislike_threshold": 2}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"recommendations.dislike_threshold": 6}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"recommendations.dislike_threshold": 8}}).status_code == 422
        assert (
            client.put("/api/settings", json={"values": {"recommendations.dislike_threshold": 10}}).status_code == 422
        )
        assert (
            client.put("/api/settings", json={"values": {"recommendations.dislike_threshold": -1}}).status_code == 422
        )
        assert (
            client.put("/api/settings", json={"values": {"recommendations.dislike_threshold": "low"}}).status_code
            == 422
        )

    def test_respecting_plex_ratings_is_a_boolean(self, client: TestClient):
        assert (
            client.put("/api/settings", json={"values": {"recommendations.use_plex_ratings": False}}).status_code == 200
        )
        assert (
            client.put("/api/settings", json={"values": {"recommendations.use_plex_ratings": "yes"}}).status_code == 422
        )

    def test_a_non_numeric_row_size_is_refused(self, client: TestClient):
        # "abc" was stored happily, then raised ValueError inside every run and 500'd two endpoints.
        assert client.put("/api/settings", json={"values": {"row.size": "abc"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"row.size": 99}}).status_code == 422
        # The free number picker allows anything 5..40 (widened from 30; ceiling = pre-rank pool cap).
        assert client.put("/api/settings", json={"values": {"row.size": 40}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"row.size": 41}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"row.size": 4}}).status_code == 422

    def test_hub_anchor_shape_is_validated(self, client: TestClient):
        # Bad shapes used to reach the engine and skip ordering silently.
        bad = {"2": {"before": True}}  # missing 'anchor'
        assert client.put("/api/settings", json={"values": {"rows.hub_anchor": bad}}).status_code == 422
        assert (
            client.put("/api/settings", json={"values": {"rows.hub_anchor": {"2": {"anchor": ""}}}}).status_code == 422
        )
        good = {"2": {"anchor": "New Series (Unwatched)", "before": False}}
        assert client.put("/api/settings", json={"values": {"rows.hub_anchor": good}}).status_code == 200
        # A 'top' entry is valid without an anchor.
        assert (
            client.put("/api/settings", json={"values": {"rows.hub_anchor": {"2": {"top": True}}}}).status_code == 200
        )
        assert client.put("/api/settings", json={"values": {"rows.hub_anchor": {}}}).status_code == 200  # clears it

    def test_request_year_bounds_are_validated(self, client: TestClient):
        # Both ends of the request year window share the 0..2100 bound (0 = that end disabled).
        assert client.put("/api/settings", json={"values": {"requests.max_year": 3000}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"requests.max_year": 1990}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"requests.min_year": 2000}}).status_code == 200

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

    def test_curator_models_is_empty_for_the_built_in_picker(self, client: TestClient):
        client.put("/api/settings", json={"values": {"curator.provider": "none"}})
        body = client.post("/api/settings/curator/models").json()
        assert body == {"provider": "none", "models": []}
        # Whole key set, because the endpoint declares a response model now and a model that dropped
        # `provider` would leave the picker unable to tell a stale reply from a live one.
        assert set(body) == {"provider", "models"}

    def test_curator_models_lists_the_saved_providers_models(self, client: TestClient, monkeypatch):

        import shortlist.engine.curator as curator_mod

        client.put("/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-secret"}})
        captured: dict = {}

        def fake_make(provider, **kw):
            captured["provider"] = provider
            captured.update(kw)
            return SimpleNamespace(list_models=lambda: ["claude-a", "claude-b"])

        monkeypatch.setattr(curator_mod, "make_curator", fake_make)
        # No body → fall back to the SAVED provider + key server-side.
        body = client.post("/api/settings/curator/models").json()
        assert body == {"provider": "anthropic", "models": ["claude-a", "claude-b"]}
        assert captured["provider"] == "anthropic"
        assert captured["api_key"] == "sk-secret"

    def test_curator_models_lists_the_provider_being_edited_from_the_request(self, client: TestClient, monkeypatch):

        import shortlist.engine.curator as curator_mod

        # A DIFFERENT provider is saved; the form is editing OpenAI with a not-yet-saved key. The picker
        # must list what's in the request, so the dropdown updates before Save — the bug Steve hit.
        client.put("/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-saved"}})
        captured: dict = {}

        def fake_make(provider, **kw):
            captured["provider"] = provider
            captured.update(kw)
            return SimpleNamespace(list_models=lambda: ["gpt-x", "gpt-y"])

        monkeypatch.setattr(curator_mod, "make_curator", fake_make)
        body = client.post(
            "/api/settings/curator/models",
            json={"provider": "openai", "api_key": "sk-new-openai"},
        ).json()
        assert body == {"provider": "openai", "models": ["gpt-x", "gpt-y"]}
        # The edited provider + its typed key reached make_curator — not the saved anthropic ones.
        assert captured["provider"] == "openai"
        assert captured["api_key"] == "sk-new-openai"

    def test_curator_models_redacted_key_falls_back_to_saved(self, client: TestClient, monkeypatch):

        import shortlist.engine.curator as curator_mod

        client.put("/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-saved"}})
        captured: dict = {}

        def fake_make(provider, **kw):
            captured.update(kw)
            return SimpleNamespace(list_models=lambda: ["claude-a"])

        monkeypatch.setattr(curator_mod, "make_curator", fake_make)
        # The UI sends the redacted placeholder for an unchanged key → use the real saved key.
        client.post("/api/settings/curator/models", json={"provider": "anthropic", "api_key": "•••••"})
        assert captured["api_key"] == "sk-saved"

    def test_curator_models_degrades_to_empty_when_listing_fails(self, client: TestClient, monkeypatch):
        import shortlist.engine.curator as curator_mod

        client.put("/api/settings", json={"values": {"curator.provider": "anthropic"}})

        def boom(provider, **kw):  # a bad/absent key or an offline provider must never 500 the picker
            raise RuntimeError("unauthorized at http://api?X-Plex-Token=SEKRET")

        monkeypatch.setattr(curator_mod, "make_curator", boom)

        # rule 9: an LLM SDK error can embed the api_key in a shape redact() doesn't cover, so the
        # handler logs only the exception CLASS, never the exception itself — prove the secret never
        # reaches the log. loguru doesn't route through stdlib logging, so `caplog` sees nothing here;
        # capture with a real sink instead (same pattern as test_ranking.py's `_captured`).
        from loguru import logger

        lines: list[str] = []
        sink_id = logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            body = client.post("/api/settings/curator/models").json()
        finally:
            logger.remove(sink_id)
        captured = "".join(lines)

        assert body == {"provider": "anthropic", "models": []}
        assert "SEKRET" not in captured
        assert "RuntimeError" in captured

    def test_web_search_provider_is_validated(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"llm_web.search_provider": "bogus"}}).status_code == 422
        for mode in ("auto", "native", "exa", "searxng"):
            assert client.put("/api/settings", json={"values": {"llm_web.search_provider": mode}}).status_code == 200

    def test_log_level_is_validated_and_applied(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"log.level": "LOUD"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"log.level": "DEBUG"}}).status_code == 200
        assert client.get("/api/settings").json()["log.level"] == "DEBUG"

    def test_run_concurrency_is_bounded(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"run.concurrency": 0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"run.concurrency": 99}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"run.concurrency": 4}}).status_code == 200

    def test_a_valid_settings_payload_still_saves(self, client: TestClient):
        r = client.put(
            "/api/settings",
            json={"values": {"row.size": 15, "requests.min_rating": 7.5, "requests.max_per_run": 5}},
        )
        assert r.status_code == 200


class TestSettingsChangeAudit:
    """Every owner-made settings change leaves an auditable before/after (rule 10) — with the
    values of secrets never appearing in it, in either direction (rule 9)."""

    @staticmethod
    def _changes(client: TestClient) -> list[dict]:
        """Newest first. `/api/events/log` — NOT `/api/events`, which is the endless SSE stream."""
        return client.get("/api/events/log", params={"scope": "settings.change"}).json()

    def test_a_changed_setting_records_its_before_and_after(self, client: TestClient):
        client.put("/api/settings", json={"values": {"requests.auto_min_rating": 8.0}})
        client.put("/api/settings", json={"values": {"requests.auto_min_rating": 7.1}})

        latest = self._changes(client)[0]["message"]["changed"]
        assert latest["requests.auto_min_rating"] == {"from": 8.0, "to": 7.1}

    def test_switching_an_off_able_schedule_off_is_recorded_the_first_time(self, client: TestClient):
        """Turning the drift check off must be auditable, and on a fresh install it was not.

        `store.get` folds the DEFAULT in, so an absent `sync.check_cron` row and a stored blank both
        read as "" — the first time an owner switched it off, "" -> "" looked like no change and
        nothing was written. That is the one unattended job that writes corrections to Plex and can
        delete a collection, so it is exactly the change rule 10 exists to capture.
        """
        before = len(self._changes(client))
        assert client.put("/api/settings", json={"values": {"sync.check_cron": ""}}).status_code == 200

        after = self._changes(client)
        assert len(after) == before + 1, "switching the drift check off wrote no audit event"
        assert "sync.check_cron" in after[0]["message"]["changed"]

    def test_an_unchanged_key_is_not_recorded(self, client: TestClient):
        """The form PUTs the whole object, so recording every key would bury the one that moved."""
        client.put("/api/settings", json={"values": {"row.size": 20, "requests.max_per_run": 5}})
        before = len(self._changes(client))
        client.put("/api/settings", json={"values": {"row.size": 20, "requests.max_per_run": 5}})

        assert len(self._changes(client)) == before, "a no-op save must not add an event"

    def test_a_secrets_value_never_reaches_the_audit_in_either_direction(self, client: TestClient):
        """The change must be visible; the key must not. Asserted over the WHOLE serialised event,
        so a value leaking through some other field still fails."""
        client.put("/api/settings", json={"values": {"plex.token": "OLD-SECRET-VALUE"}})
        client.put("/api/settings", json={"values": {"plex.token": "NEW-SECRET-VALUE"}})

        events = self._changes(client)
        blob = str(events)
        assert "OLD-SECRET-VALUE" not in blob, "the previous secret leaked into the audit log"
        assert "NEW-SECRET-VALUE" not in blob, "the new secret leaked into the audit log"
        assert events[0]["message"]["changed"]["plex.token"] == {"from": "<redacted>", "to": "<redacted>"}

    def test_the_redacted_placeholder_is_not_recorded_as_a_change(self, client: TestClient):
        """The UI echoes '•••••' for a secret it never received — that is 'leave it alone', and the
        write loop skips it. Recording it would report a rotation that never happened."""
        client.put("/api/settings", json={"values": {"plex.token": "real-token"}})
        before = len(self._changes(client))
        client.put("/api/settings", json={"values": {"plex.token": "•••••"}})

        assert len(self._changes(client)) == before

    def test_a_long_value_is_summarised_rather_than_copied_whole(self, client: TestClient):
        """Object-valued settings would otherwise put a second copy of the config in every event."""
        client.put("/api/settings", json={"values": {"candidates.sources": ["tmdb_similar"]}})
        client.put(
            "/api/settings",
            json={"values": {"candidates.sources": ["tmdb_similar", "tmdb_discover", "trakt", "llm_web"] * 12}},
        )

        recorded = self._changes(client)[0]["message"]["changed"]["candidates.sources"]["to"]
        assert isinstance(recorded, str) and recorded.endswith("chars)")


class TestSettingsApi:
    def test_get_put_round_trip_and_unknown_key_rejected(self, client: TestClient):
        settings = client.get("/api/settings").json()
        assert settings["row.size"] == 15
        r = client.put("/api/settings", json={"values": {"row.size": 20}})
        assert r.status_code == 200
        assert r.json()["row.size"] == 20
        assert client.put("/api/settings", json={"values": {"evil.key": 1}}).status_code == 422

    def test_every_stored_setting_survives_the_response_model(self, client: TestClient):
        """This endpoint's key set is genuinely dynamic — `DEFAULTS` plus whatever the DB holds — so
        its response model declares no fields at all and relies on `extra="allow"` to pass them
        through. That is exactly the arrangement a stricter model would break SILENTLY: every
        undeclared key would be dropped, and the settings page would render blank controls with a
        200 and nothing in the log.

        Asserted against the store itself rather than a hard-coded list, so a setting added tomorrow
        is covered the day it is written.
        """
        client.put("/api/settings", json={"values": {"row.size": 20, "plex.token": "real-token"}})
        with client.app.state.sessions() as session:
            expected = set(SettingsStore(session, client.app.state.secrets).all_public())

        served = client.get("/api/settings").json()
        put_back = client.put("/api/settings", json={"values": {"row.size": 21}}).json()

        assert set(served) == expected
        assert set(put_back) == expected, "PUT returns the same store, so it must not filter either"
        assert len(expected) > 50, "a vacuous pass if the store were somehow empty"
        assert served["plex.token"] == "•••••"  # …and secrets are still redacted on the way out

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

    def test_exa_key_is_encrypted_at_rest_and_redacted_in_the_api(self, client: TestClient):
        # The Exa web-search key is a secret: stored encrypted, shown as the redacted sentinel, and
        # a round-tripped sentinel must not overwrite the real value (rule 9).
        client.put("/api/settings", json={"values": {"exa.apikey": "exa-secret-123"}})
        assert client.get("/api/settings").json()["exa.apikey"] == "•••••"
        client.put("/api/settings", json={"values": {"exa.apikey": "•••••"}})  # UI round-trip
        with client.app.state.sessions() as session:
            from shortlist.server.settings_store import SettingsStore

            assert SettingsStore(session, client.app.state.secrets).get("exa.apikey") == "exa-secret-123"

    def test_exa_test_connection_probes_the_key(self, client: TestClient, monkeypatch):
        # No key → a plain-English error, not a crash.
        no_key = client.post("/api/settings/test/exa").json()
        assert no_key["ok"] is False and "Exa" in no_key["message"]
        # With a key, it pings Exa (mocked — no test may touch the network).
        client.put("/api/settings", json={"values": {"exa.apikey": "exa-secret-123"}})
        monkeypatch.setattr("shortlist.engine.clients.search.ExaClient.ping", lambda self: "ok — 1 result")
        ok = client.post("/api/settings/test/exa").json()
        assert ok["ok"] is True and "ok" in ok["message"]
        # Both branches build their own dict, so both are checked against the response model.
        assert set(no_key) == {"ok", "message"} and set(ok) == {"ok", "message"}

    def test_searxng_password_is_encrypted_at_rest_and_redacted_in_the_api(self, client: TestClient):
        """A reverse-proxy password in front of SearXNG is a credential like any other (rule 9). The
        URL and username are not secrets and stay readable, so the card can show what's configured."""
        values = {"searxng.url": "http://searx:8080", "searxng.username": "admin", "searxng.password": "hunter2"}
        client.put("/api/settings", json={"values": values})
        public = client.get("/api/settings").json()
        assert public["searxng.password"] == "•••••"
        assert public["searxng.url"] == "http://searx:8080" and public["searxng.username"] == "admin"
        client.put("/api/settings", json={"values": {"searxng.password": "•••••"}})  # UI round-trip
        with client.app.state.sessions() as session:
            from shortlist.server.settings_store import SettingsStore

            assert SettingsStore(session, client.app.state.secrets).get("searxng.password") == "hunter2"

    def test_a_password_in_the_searxng_url_is_refused_at_the_boundary(self, client: TestClient):
        """`searxng.url` is deliberately NOT a secret: it is shown in the clear so an owner can spot a
        typo, it rides into the `settings.change` audit event, and that event is immutable history
        exported by the support bundle. So a credential must never be allowed INTO it — stripping it
        later in the client only ever protected the client's own error strings.

        The rejection is the fix, not just a guard: the message sends the owner to the fields that
        encrypt it.
        """
        bad = "http://admin:hunter2@searx.local:8080"
        response = client.put("/api/settings", json={"values": {"searxng.url": bad}})
        assert response.status_code == 422
        assert "username" in response.text.lower()

        # And nothing about it survives anywhere: not the setting, not the audit trail. The `events`
        # row is checked at the table, because that is the immutable record the support bundle ships.
        assert "hunter2" not in client.get("/api/settings").text
        with client.app.state.sessions() as session:
            import sqlalchemy

            rows = session.execute(sqlalchemy.text("SELECT message FROM events")).all()
        assert not [r for r in rows if "hunter2" in str(r[0])]

    def test_a_plain_searxng_url_is_still_accepted(self, client: TestClient):
        ok = client.put("/api/settings", json={"values": {"searxng.url": "http://searx.local:8080"}})
        assert ok.status_code == 200

    def test_searxng_test_connection_probes_the_instance(self, client: TestClient, monkeypatch):
        no_url = client.post("/api/settings/test/searxng").json()
        assert no_url["ok"] is False and "SearXNG" in no_url["message"]
        client.put("/api/settings", json={"values": {"searxng.url": "http://searx:8080"}})
        monkeypatch.setattr("shortlist.engine.clients.search.SearxngClient.ping", lambda self: "ok — 8 results")
        ok = client.post("/api/settings/test/searxng").json()
        assert ok["ok"] is True and "ok" in ok["message"]
        assert set(no_url) == {"ok", "message"} and set(ok) == {"ok", "message"}

    def test_searxng_json_misconfiguration_reaches_the_owner_verbatim(self, client: TestClient, monkeypatch):
        """The 403-means-enable-JSON message is the whole point of the probe — it must survive the
        error path intact and not be flattened into a generic "connection failed"."""
        client.put("/api/settings", json={"values": {"searxng.url": "http://searx:8080"}})

        def _raise(self):
            raise RuntimeError("SearXNG refused the JSON format (403). Add `json` to `search.formats` in its ...")

        monkeypatch.setattr("shortlist.engine.clients.search.SearxngClient.ping", _raise)
        body = client.post("/api/settings/test/searxng").json()
        assert body["ok"] is False and "search.formats" in body["message"]

    def test_arr_options_serve_the_dropdowns_the_settings_form_needs(self, client: TestClient, monkeypatch):
        """Quality profiles and root folders, so a non-technical owner picks from a list instead of
        hunting down a numeric profile id and a server path."""
        from types import SimpleNamespace

        client.put(
            "/api/settings",
            json={"values": {"requests.radarr.url": "http://radarr", "requests.radarr.apikey": "k"}},
        )
        fake = SimpleNamespace(
            quality_profiles=lambda: [{"id": 1, "name": "HD-1080p"}],
            root_folders=lambda: [{"id": 2, "path": "/movies"}],
        )
        monkeypatch.setattr("shortlist.engine.clients.arr.make_arr_client", lambda service, target: fake)

        body = client.get("/api/settings/arr/radarr/options").json()

        assert set(body) == {"quality_profiles", "root_folders"}
        assert [set(p) for p in body["quality_profiles"]] == [{"id", "name"}]
        assert [set(f) for f in body["root_folders"]] == [{"id", "path"}]
        assert body["quality_profiles"] == [{"id": 1, "name": "HD-1080p"}]
        assert body["root_folders"] == [{"id": 2, "path": "/movies"}]

    def test_arr_options_are_409_before_the_arr_is_connected(self, client: TestClient):
        assert client.get("/api/settings/arr/sonarr/options").status_code == 409

    def test_connection_error_redacts_a_plex_token(self, client: TestClient, monkeypatch):
        # plexapi errors can embed the tokened request URL; the connection-test response must never
        # echo it back (plex-safety rule 9). Simulate a probe that raises with a token in the message.
        client.put("/api/settings", json={"values": {"plex.url": "http://pms:32400", "plex.token": "tok"}})

        def boom(self, *a, **k):
            raise RuntimeError("BadRequest: http://pms:32400/library?X-Plex-Token=super-secret-abc failed")

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient.__init__", boom)
        body = client.post("/api/settings/test/plex").json()
        assert body["ok"] is False
        assert "super-secret-abc" not in body["message"]
        assert "X-Plex-Token=REDACTED" in body["message"]


class TestSettingsThatDoRealWork:
    """Two settings change what is on somebody's Plex server, and saving them used to change only the
    database: the toggle flipped, the page said "saved", and nothing on the server moved until the
    next nightly run — or, for the row name, ever.
    """

    def test_flipping_the_hide_shared_from_disabled_toggle_queues_the_filter_pass(self, client: TestClient):
        """This toggle decides whether an opted-out account still sees the public shared rows, which is
        purely a `label!=` exclude on their share. Only a filter write makes it true — in BOTH
        directions: turning it off owes those accounts the REMOVAL of an exclude, which is just as
        invisible until something writes."""
        client.put("/api/settings", json={"values": {"privacy.hide_shared_from_disabled": False}})

        assert [j["kind"] for j in client.get("/api/system/jobs").json()] == ["privacy.sync"]

    def test_saving_the_same_value_queues_nothing(self, client: TestClient):
        """A settings save sends the whole form, so every unrelated edit would otherwise trigger a
        server-wide pass over throttled plex.tv."""
        client.put("/api/settings", json={"values": {"privacy.hide_shared_from_disabled": False}})
        before = len(client.get("/api/system/jobs").json())

        client.put("/api/settings", json={"values": {"privacy.hide_shared_from_disabled": False, "row.size": 20}})

        assert len(client.get("/api/system/jobs").json()) == before

    def test_renaming_the_default_row_from_settings_retitles_the_collections(self, client: TestClient, monkeypatch):
        """The default row's title IS `row.name_template`. The Rows page reconciled a rename; this door
        did not — so the next run built a SECOND collection under the new name and left the old one
        labelled and promoted for ever, with nothing able to address a title no run would write again."""
        from unittest.mock import MagicMock

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import EngineConfig

        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id

        renames: list[tuple[str, str]] = []
        col = MagicMock(title="✨ Movies Picked for You" + row_marker(acct))
        col.editTitle.side_effect = lambda new: renames.append((col.title, new))
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda s, label: [col] if label == f"shortlist_{uslug}" else []
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)

        r = client.put("/api/settings", json={"values": {"row.name_template": "🔥 {library_name} Handpicked"}})
        assert r.status_code == 200

        marker = row_marker(acct)
        assert renames == [("✨ Movies Picked for You" + marker, "🔥 Movies Handpicked" + marker)]


class TestRepointingPlexAtAnotherServer:
    """Everything Shortlist knows is scoped to ONE machine: which collection is whose (the delivery
    ledger), whose share filters were snapshotted before we touched them, which account is the owner.

    Silently accepting a `plex.url`/`plex.token` that points somewhere else leaves all of that
    describing a machine nobody is talking to — and the next reconcile goes looking for those
    collections on a server that never had them. Switching servers is a re-link, not a settings edit.
    """

    def _machine(self, client: TestClient, monkeypatch, machine_id: str | None):
        """Point PlexClient at a fake whose machine_id is `machine_id`; None = unreachable."""
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.commit()

        class Fake:
            def __init__(self, url, token, **kw):
                if machine_id is None:
                    raise RuntimeError("connection refused")
                self.machine_id = machine_id

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", Fake)

    def test_a_different_machine_id_is_refused_with_an_explanation(self, client: TestClient, monkeypatch):
        self._machine(client, monkeypatch, "some-other-server")

        r = client.put("/api/settings", json={"values": {"plex.url": "http://elsewhere:32400"}})

        assert r.status_code == 409
        assert "different plex server" in r.json()["detail"].lower()
        # And the value is NOT stored — a rejected save must change nothing.
        assert client.get("/api/settings").json()["plex.url"] != "http://elsewhere:32400"

    def test_the_same_server_saves_normally(self, client: TestClient, monkeypatch):
        self._machine(client, monkeypatch, "m1")  # the fixture links machine_id "m1"

        r = client.put("/api/settings", json={"values": {"plex.url": "http://pms:32400/"}})

        assert r.status_code == 200
        assert client.get("/api/settings").json()["plex.url"] == "http://pms:32400/"

    def test_an_unreachable_server_is_saved_rather_than_refused(self, client: TestClient, monkeypatch):
        """A box that is merely down, or a URL not routable yet, must still be savable — otherwise a
        broken connection becomes unfixable through the UI, which is where you go to fix it."""
        self._machine(client, monkeypatch, None)

        r = client.put("/api/settings", json={"values": {"plex.url": "http://not-up-yet:32400"}})

        assert r.status_code == 200
        assert client.get("/api/settings").json()["plex.url"] == "http://not-up-yet:32400"

    def test_settings_unrelated_to_plex_never_probe(self, client: TestClient, monkeypatch):
        """The check costs a PMS round-trip. Every other save on the page must not pay it."""
        probed: list[str] = []

        class Fake:
            def __init__(self, url, token, **kw):
                probed.append(url)
                self.machine_id = "m1"

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", Fake)

        assert client.put("/api/settings", json={"values": {"row.size": 25}}).status_code == 200
        assert probed == []
