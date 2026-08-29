"""API contract tests: the in-app logs view, the audit log, and the notifications bell."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.server.api.settings import REDACTED_PLACEHOLDER
from shortlist.server.auth import SESSION_COOKIE
from shortlist.server.settings_store import SettingsStore
from tests.conftest import plextv_user

pytestmark = pytest.mark.integration


_HUB_IDS = iter(range(500, 999))


def _hub(title: str, *, promoted: bool = True, recommended: bool = False):
    """A COLLECTION's managed hub, as `managedHubs()` really returns one.

    Two things a lazier fake would leave out, both of which the endpoint now reads (testing rule: the
    fake must be no easier than the real server). The three promotion flags — without them it reads as
    promoted-nowhere, the very state being told apart. And a `custom.collection.<sectionID>.<ratingKey>`
    identifier, which is how a collection's hub is told from one of Plex's built-ins; a built-in is
    modelled by a plain `SimpleNamespace(title=...)`, carrying an identifier of another kind or none.

    Default promoted-on-shared-Home, because that is what a collection on the shelf looks like.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        title=title,
        identifier=f"custom.collection.1.{next(_HUB_IDS)}",
        promotedToSharedHome=promoted,
        promotedToOwnHome=False,
        promotedToRecommended=recommended,
    )


class TestLogsApi:
    """The in-app Logs view. Owner-only, and redacted — it exists to be copied into bug reports."""

    def _write_log(self, client: TestClient, *lines: str) -> None:
        """Seed the log file these tests read back.

        The app's own logger holds this file OPEN while we truncate it, so loguru's next write lands
        at its stale offset and can leave a fragment of a real line spliced onto ours. That is why the
        assertions below compare the first line of each message rather than the whole string: the
        contract under test is "the endpoint filters by level", never "this file contains only what
        the test put there", and asserting the latter made the test fail about one run in eight.
        """
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _messages(body: dict) -> list[str]:
        """Each line's message, minus any fragment of a concurrent write spliced onto the end."""
        return [x["message"].splitlines()[0] if x["message"] else "" for x in body["lines"]]

    LINE = "2026-07-21 07:27:18.100 | {level:<8} | shortlist.server.main:lifespan:168 - {message}"

    def test_returns_parsed_lines_filtered_by_level(self, client: TestClient):
        self._write_log(
            client,
            self.LINE.format(level="DEBUG", message="quiet"),
            self.LINE.format(level="ERROR", message="loud"),
        )

        body = client.get("/api/system/logs?level=ERROR").json()

        assert "loud" in self._messages(body), "the ERROR line must come back"
        assert "quiet" not in self._messages(body), "the DEBUG line must not"
        assert all(x["level"] == "ERROR" for x in body["lines"]), "nothing but ERROR"
        assert body["file"] == "shortlist.log"
        # Spelled out because the endpoint now declares a Pydantic response model, and a model that
        # forgot a key would DROP it from the payload rather than fail — the Logs page would simply
        # stop paginating (`truncated`) or stop naming its file, with nothing red anywhere.
        assert set(body) == {"lines", "total_matched", "truncated", "file"}
        assert set(body["lines"][0]) == {"ts", "level", "source", "message"}

    def test_an_empty_log_directory_still_returns_the_whole_shape(self, client: TestClient):
        """The no-file branch builds its own dict, so it is a second place a key can go missing."""
        import shutil

        shutil.rmtree(client.app.state.config_dir / "logs", ignore_errors=True)

        body = client.get("/api/system/logs").json()

        assert set(body) == {"lines", "total_matched", "truncated", "file"}
        assert body == {"lines": [], "total_matched": 0, "truncated": False, "file": None}

    def test_never_serves_a_credential(self, client: TestClient):
        """The whole point of the view is that it gets shared, so this is the load-bearing test."""
        self._write_log(client, self.LINE.format(level="INFO", message="GET /x?X-Plex-Token=LEAKME -> 200"))

        assert "LEAKME" not in client.get("/api/system/logs").text

    def test_the_zip_download_is_attached_and_redacted(self, client: TestClient):
        import io
        import zipfile

        self._write_log(client, self.LINE.format(level="INFO", message="token: X-Plex-Token: LEAKME"))

        r = client.get("/api/system/logs/download")

        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment; filename=" in r.headers["content-disposition"]
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        assert "LEAKME" not in archive.read("logs/shortlist.log").decode()

    def test_logs_are_owner_only(self, client: TestClient):
        """Logs describe the whole server and name every user on it — they are not public."""
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/system/logs").status_code == 401
        assert client.get("/api/system/logs/download").status_code == 401


class TestEventsApi:
    def test_audit_log_empty(self, client: TestClient):
        assert client.get("/api/events/log").json() == []


class TestNotifications:
    def test_surface_paused_and_failed_run_most_severe_first(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif
        from shortlist.server.db.models import Run

        monkeypatch.setattr(notif, "check_for_update", lambda _v: None)  # never touch GitHub in a test
        with client.app.state.sessions() as session:
            SettingsStore(session).set("paused_all", True)
            session.add(Run(trigger="manual", status="error"))
            session.commit()

        body = client.get("/api/notifications").json()
        items = body["notifications"]
        ids = {n["id"] for n in items}
        assert "runs-paused" in ids
        assert any(i.startswith("run-failed-") for i in ids)
        order = {"error": 0, "warning": 1, "info": 2}
        severities = [order[n["severity"]] for n in items]
        assert severities == sorted(severities)  # error before warning before info
        # Every field the bell renders, spelled out: the endpoint declares a response model now, and
        # one that missed `action_url` or `dismissable` would quietly serve un-clickable, un-hideable
        # alerts instead of erroring.
        assert set(body) == {"notifications", "dismissed"}
        # `dismissed` is not decoration: the owner-shelf warning also renders inline on the Users
        # page, so both surfaces read this one list and dismiss as one. A payload that dropped it
        # would leave that note with no way to know it had already been acknowledged.
        assert body["dismissed"] == []
        for note in items:
            assert set(note) == {
                "id",
                "severity",
                "title",
                "body",
                "action_url",
                "action_label",
                "dismissable",
            }, note

    def test_a_dismissed_id_comes_back_in_the_dismissed_list(self, client: TestClient):
        """The round trip the inline owner-shelf note depends on. Without it that note cannot tell
        it was dismissed and would reappear on every page load."""
        client.post("/api/notifications/dismiss", json={"id": "owner-sees-all-rows"})

        assert client.get("/api/notifications").json()["dismissed"] == ["owner-sees-all-rows"]

    def test_a_partial_run_and_recent_errors_surface_as_warnings(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif
        from shortlist.server.db.models import Event, Run

        monkeypatch.setattr(notif, "check_for_update", lambda _v: None)
        with client.app.state.sessions() as session:
            session.add(Run(trigger="manual", status="ok", stats={"users_ok": 1, "users_error": 2}))
            session.add(Event(scope="requests.send", level="error", message={"detail": "arr down"}))
            session.commit()

        items = client.get("/api/notifications").json()["notifications"]
        by_id = {n["id"]: n for n in items}
        partial = next(n for k, n in by_id.items() if k.startswith("run-partial-"))
        assert "2 people failed" in partial["title"]  # pluralized
        errors = next(n for k, n in by_id.items() if k.startswith("recent-errors-"))
        assert errors["severity"] == "warning"
        # Dismissable: it counts what already HAPPENED, and there is nothing to do about the past but
        # acknowledge it. Only alerts describing a condition still true now stay undismissable.
        assert errors["dismissable"] is True

    def test_update_notification_can_be_dismissed_per_version(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif

        monkeypatch.setattr(notif, "check_for_update", lambda _v: {"latest": "9.9.9", "url": "https://example/rel"})
        first = client.get("/api/notifications").json()["notifications"]
        assert any(n["id"] == "update-9.9.9" and n["dismissable"] for n in first)

        assert client.post("/api/notifications/dismiss", json={"id": "update-9.9.9"}).json() == {"ok": True}
        after = client.get("/api/notifications").json()["notifications"]
        assert not any(n["id"] == "update-9.9.9" for n in after)  # dismissed by id

    def test_a_dismissable_flag_is_served_for_every_alert(self, client: TestClient, monkeypatch):
        """The other branch of the notification shape: an alert that CAN be hidden. `dismissable` is
        the field the bell keys its hide button on, so it must survive serialization in both states."""
        import shortlist.server.notifications as notif

        monkeypatch.setattr(notif, "check_for_update", lambda _v: {"latest": "9.9.9", "url": "https://example/rel"})
        with client.app.state.sessions() as session:
            SettingsStore(session).set("paused_all", True)
            session.commit()

        by_id = {n["id"]: n for n in client.get("/api/notifications").json()["notifications"]}

        assert by_id["update-9.9.9"]["dismissable"] is True
        assert by_id["runs-paused"]["dismissable"] is False

    def test_debug_bundle_reports_facts_but_never_a_secret(self, client: TestClient):

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "SUPERSECRETTOKEN")
            SettingsStore(session).set("plex.url", "http://pms")
            session.commit()

        r = client.get("/api/system/debug")
        assert r.status_code == 200
        text = r.text
        assert "Shortlist debug bundle" in text and "db migration head:" in text
        assert "plex=True" in text  # connection reported as configured...
        assert "SUPERSECRETTOKEN" not in text  # ...but the token itself is never in the bundle


class TestSystemResponseShapes:
    """The endpoints now declare Pydantic response models, and a model that misses a key DROPS it
    from the payload — silently, in production, blanking whatever read it. So each key set is
    asserted in full rather than sampled.
    """

    def test_version_reports_current_latest_and_install_type(self, client: TestClient, monkeypatch):
        from shortlist.server import version_check

        # No test may touch the network. The release check is cached in-process, so the whole cache
        # is REPLACED (not just cleared) — monkeypatch then puts the real one back untouched, rather
        # than leaving a reset cache behind for a later test to refill over the network.
        monkeypatch.setattr(version_check, "_fetch_latest", lambda: {"tag": "v9.9.9", "url": "https://example/rel"})
        monkeypatch.setattr(version_check, "_cache", {"at": None, "value": None})

        body = client.get("/api/system/version").json()

        assert set(body) == {
            "current_version",
            "latest_version",
            "update_available",
            "install_type",
            "git_sha",
            "git_branch",
        }
        assert body["latest_version"] == "9.9.9"  # the "v" is stripped for display
        assert body["update_available"] is True

    def test_version_reports_a_null_latest_when_the_check_is_unavailable(self, client: TestClient, monkeypatch):
        """GitHub being down must not change the SHAPE — `latest_version` goes null, nothing goes
        missing, or the About panel loses the fields it renders around it."""
        from shortlist.server import version_check

        monkeypatch.setattr(version_check, "_fetch_latest", lambda: None)
        monkeypatch.setattr(version_check, "_cache", {"at": None, "value": None})

        body = client.get("/api/system/version").json()

        assert set(body) == {
            "current_version",
            "latest_version",
            "update_available",
            "install_type",
            "git_sha",
            "git_branch",
        }
        assert body["latest_version"] is None and body["update_available"] is False

    def test_syncs_reports_each_schedule_with_its_nested_shape(self, client: TestClient):
        body = client.get("/api/system/syncs").json()

        assert set(body) == {"watched", "users", "backup"}
        assert set(body["watched"]) == {"last", "next", "cron"}
        assert set(body["users"]) == {"last", "next", "cron"}
        # Backups carry no "last": the backup list itself is that answer, so the nested shape differs.
        assert set(body["backup"]) == {"next", "cron", "max_keep"}
        assert isinstance(body["backup"]["max_keep"], int)

    def test_image_provider_explains_itself_when_it_cannot_generate(self, client: TestClient):
        body = client.get("/api/system/image-provider").json()

        assert set(body) == {"capable", "provider", "reason"}
        assert body["capable"] is False
        assert body["reason"], "the row editor disables the option, so it must be able to say why"

    def test_image_provider_reports_capable_with_an_image_model_configured(self, client: TestClient):
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("curator.provider", "openai")
            store.set("curator.api_key", "sk-test")
            session.commit()

        body = client.get("/api/system/image-provider").json()

        assert set(body) == {"capable", "provider", "reason"}
        assert body["capable"] is True and body["reason"] == ""

    def _connect_plex(self, client: TestClient) -> None:
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "tok")
            session.commit()

    def test_libraries_lists_the_delivery_targets(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace

        self._connect_plex(client)

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                # `key` is an int on a real PMS and a string in the API — the model must not lose it.
                return [SimpleNamespace(key=1, title="Movies", type="movie")]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        body = client.get("/api/system/libraries").json()

        assert [set(x) for x in body] == [{"key", "title", "type"}]
        assert body == [{"key": "1", "title": "Movies", "type": "movie"}]

    def test_library_collections_offers_only_foreign_anchors(self, client: TestClient, monkeypatch):
        """Our own collections are never title-anchors: a per-person row is one collection PER PERSON,
        so a title names one account's copy and would place the row for that account alone. Anchoring
        to another Shortlist row is done by row slug instead (issue #81).

        The fake serves a MARKED title, because that is what a real PMS returns for a collection we
        wrote, and the exclusion is marker-based for a reason: the old label-based filter went through
        `collection.labels`, whose re-read can come back empty (plex-safety rule 4), and on those page
        loads every Shortlist row appeared here as a selectable anchor that then placed nothing.
        """
        from types import SimpleNamespace

        from shortlist.engine.delivery import row_marker

        self._connect_plex(client)
        section = SimpleNamespace(
            key=1,
            title="Movies",
            type="movie",
            managedHubs=lambda: [
                _hub("Picked for You" + row_marker(100)),
                _hub("New Series (Unwatched)"),
            ],
        )

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [section]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        body = client.get("/api/system/libraries/1/collections").json()

        assert body == [{"title": "New Series (Unwatched)", "on_shelf": True}]
        assert set(body[0]) == {"title", "on_shelf"}

    def test_library_collections_excludes_our_rows_even_when_plex_reports_no_labels(
        self, client: TestClient, monkeypatch
    ):
        """The flicker that produced issue #81, pinned. A collection of ours carrying NO labels at all
        must still be excluded — the marker in the title is proof enough, and it is the only proof
        that cannot come back empty."""
        from types import SimpleNamespace

        from shortlist.engine.delivery import row_marker

        self._connect_plex(client)
        section = SimpleNamespace(
            key=1,
            title="Movies",
            type="movie",
            collections=lambda: [
                SimpleNamespace(title="Picked for You" + row_marker(100), labels=[]),
                SimpleNamespace(title="Kometa Genre"),
            ],
            managedHubs=lambda: [
                _hub("Picked for You" + row_marker(100)),
                _hub("Kometa Genre"),
            ],
        )

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [section]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        assert client.get("/api/system/libraries/1/collections").json() == [{"title": "Kometa Genre", "on_shelf": True}]

    def test_library_collections_flags_a_collection_with_no_shelf_position(self, client: TestClient, monkeypatch):
        """Issue #106. `managedHubs()` lists every hub the library CAN manage, promoted or not, so the
        picker was offering collections with no position on the shelf — and the engine followed one
        and buried the row underneath every standard Plex hub. The flag is what lets the editor say
        so; it is NOT filtered out here, because an owner whose saved anchor vanished from the list
        cannot tell "not on the shelf" from "deleted".

        The matrix that matters is COLLECTION vs BUILT-IN, told apart by the hub's own
        `custom.collection.*` identifier rather than by title. Only a collection is judged, because
        Plex sends the promotion flags for those and the app reads them on every promote. A built-in
        hub is never marked unusable: the engine never refuses one, so saying so here would be a lie
        the owner acts on — and titles collide, so a title check would refuse one for real.
        """
        from types import SimpleNamespace

        self._connect_plex(client)
        section = SimpleNamespace(
            key=1,
            title="Movies",
            type="movie",
            managedHubs=lambda: [
                # A built-in Plex hub: no promotion flags, and an identifier of another kind — the
                # shape this repo has never recorded (plex-safety rule 11). It must stay selectable.
                SimpleNamespace(title="Recently Added", identifier="home.television.recentlyadded"),
                _hub("New Series (Unwatched)"),
                _hub("Archive 2019", promoted=False),
                # Any ONE flag is a real, visible position — a Kometa anchor is usually this one.
                _hub("Kometa Genre", promoted=False, recommended=True),
            ],
        )

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [section]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        assert client.get("/api/system/libraries/1/collections").json() == [
            {"title": "Recently Added", "on_shelf": True},
            {"title": "New Series (Unwatched)", "on_shelf": True},
            {"title": "Archive 2019", "on_shelf": False},
            {"title": "Kometa Genre", "on_shelf": True},
        ]

    def test_the_library_list_is_read_from_plex_once_not_once_per_page_load(self, client: TestClient, monkeypatch):
        """`/libraries` backs every row card, the library picker and the placement settings, and each
        read is a PlexServer handshake plus a sections read. Plex serialises against its own database,
        so on a busy server (one DELETE took 15.8s during a collection sweep, SFLIX 2026-08-04) every
        page wanting a library list queued behind it. The list changes when someone adds a library."""
        from types import SimpleNamespace

        import shortlist.server.api.system as system_api

        self._connect_plex(client)
        reads = {"n": 0}
        built: list[dict] = []

        class FakePlex:
            def __init__(self, *a, **k):
                built.append(k)

            def sections(self):
                reads["n"] += 1
                return [SimpleNamespace(key=1, title="Movies", type="movie")]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        first = client.get("/api/system/libraries").json()
        again = client.get("/api/system/libraries").json()

        assert first == again
        assert reads["n"] == 1, "the second page load must not go back to Plex"
        # The timeout is what bounds how long the single-flight lock is held. At the 20s default,
        # one page load could hold it for four retries plus backoff while everyone else waits.
        assert built[0]["timeout"] == system_api._INTERACTIVE_TIMEOUT_S

    def test_a_plex_that_fails_after_a_good_read_serves_the_cached_copy(self, client: TestClient, monkeypatch):
        """A library list two minutes old is a far better answer than a broken page, and it is used
        to populate a picker, never to decide a write. Without this, one slow moment on Plex empties
        the library picker mid-edit."""
        from types import SimpleNamespace

        import shortlist.server.api.system as system_api

        self._connect_plex(client)
        state = {"fail": False}

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                if state["fail"]:
                    raise TimeoutError("PMS is busy")
                return [SimpleNamespace(key=1, title="Movies", type="movie")]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)
        good = client.get("/api/system/libraries").json()

        # Expire the entry so the next call really does go back to Plex, and make Plex fail.
        client.app.state.__dict__["_plex_read_cache"]["libraries"] = (0.0, good)
        state["fail"] = True

        assert client.get("/api/system/libraries").json() == good
        assert system_api._PLEX_READ_TTL_S > 0  # the knob this behaviour hangs off still exists

    def test_an_unknown_library_leaves_no_lock_behind(self, client: TestClient, monkeypatch):
        """`key` is a caller-supplied path segment, so a lock kept per value ever asked for would
        grow for as long as the process lives. Owner-authed, so this is hygiene rather than a DoS —
        but an unbounded dict keyed on request input is worth not having."""
        from types import SimpleNamespace

        self._connect_plex(client)

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [SimpleNamespace(key=1, title="Movies", type="movie")]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        for bogus in ("999", "998", "997"):
            assert client.get(f"/api/system/libraries/{bogus}/collections").status_code == 404

        locks = client.app.state.__dict__.get("_plex_read_locks", {})
        assert not [k for k in locks if k.startswith("collections:99")], f"locks accumulated: {locks}"

    def test_re_pointing_plex_drops_the_cached_library_list(self, client: TestClient, monkeypatch):
        """The cache is keyed by the READ, not by the server, so a connection change has to clear it.

        Note what is NOT the risk here: pointing at a DIFFERENT machine is refused outright (409 —
        switching servers is a re-link, not a settings edit), so the cache can never straddle two
        servers. What this covers is the reachable case — the same server at a new address or with a
        rotated token — plus any future path that writes those keys.
        """
        from types import SimpleNamespace

        self._connect_plex(client)
        current = {"title": "Movies"}

        class FakePlex:
            # Matches the linked server in the fixture, so re-pointing is allowed rather than 409'd.
            machine_id = "m1"

            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [SimpleNamespace(key=1, title=current["title"], type="movie")]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)
        assert client.get("/api/system/libraries").json()[0]["title"] == "Movies"

        current["title"] = "Films"
        # An unrelated setting must NOT throw the cache away — that would put Plex back on every page
        # load for anyone who touches Settings.
        assert client.put("/api/settings", json={"values": {"row.size": 12}}).status_code == 200
        assert client.get("/api/system/libraries").json()[0]["title"] == "Movies"

        saved = client.put("/api/settings", json={"values": {"plex.url": "http://pms-new:32400"}})
        assert saved.status_code == 200, saved.text
        assert client.get("/api/system/libraries").json()[0]["title"] == "Films"

        # A REAL token change re-points the same way a URL does.
        current["title"] = "Cinema"
        saved = client.put("/api/settings", json={"values": {"plex.token": "rotated-token"}})
        assert saved.status_code == 200, saved.text
        assert client.get("/api/system/libraries").json()[0]["title"] == "Cinema"

        # ...but the placeholder the UI round-trips for a secret it never received is NOT a change —
        # the write loop skips it, so the cache must survive it for the same reason `row.size` does.
        # Otherwise saving the Settings page at all puts Plex back on the next page load.
        current["title"] = "Pictures"
        saved = client.put("/api/settings", json={"values": {"plex.token": REDACTED_PLACEHOLDER}})
        assert saved.status_code == 200, saved.text
        assert client.get("/api/system/libraries").json()[0]["title"] == "Cinema"

    def test_the_cache_drop_lands_after_the_new_connection_commits(self, client: TestClient, monkeypatch):
        """Dropping the cache BEFORE the write commits re-caches the server you just left.

        `/libraries` runs its read on an executor thread, so it genuinely interleaves with a
        `put_settings` that has not committed yet: the read repopulates the entry from the OLD
        url/token and pins it for the whole TTL — the exact staleness the drop exists to prevent.

        Asserting the ORDER is what gives this teeth. "The cache ends up empty" passes just as
        happily with the drop back on the wrong side of the commit.
        """
        from types import SimpleNamespace

        import shortlist.server.api.system as system_module

        self._connect_plex(client)

        class FakePlex:
            machine_id = "m1"

            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [SimpleNamespace(key=1, title="Movies", type="movie")]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        seen: dict[str, object] = {}
        real = system_module.invalidate_plex_reads

        def spy(state):
            # What a concurrent reader would find in the DB at the instant the cache is dropped.
            with client.app.state.sessions() as session:
                seen["url"] = SettingsStore(session, client.app.state.secrets).get("plex.url")
            return real(state)

        # `put_settings` imports this inside the function, so patching the source module is what lands.
        monkeypatch.setattr(system_module, "invalidate_plex_reads", spy)

        saved = client.put("/api/settings", json={"values": {"plex.url": "http://pms-new:32400"}})
        assert saved.status_code == 200, saved.text
        assert seen["url"] == "http://pms-new:32400"

    def test_a_plex_that_fails_with_nothing_cached_still_errors(self, client: TestClient, monkeypatch):
        """Serving stale is a kindness, not a cover-up: with no previous answer there is nothing
        honest to return, so the failure has to reach the caller."""
        self._connect_plex(client)

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                raise TimeoutError("PMS is busy")

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        with pytest.raises(TimeoutError):
            client.get("/api/system/libraries")


class TestClosedSetFieldsMatchWhatTheCodeWrites:
    """`LibraryOut.type` and `NotificationOut.severity` are `Literal`s now, not bare `str`.

    Both are validated on the way OUT, so a value outside the set raises instead of degrading — the
    libraries picker or the notification bell would 500 rather than show something unfamiliar. Each
    is therefore driven through its real handler for every value its producer can emit.
    """

    def test_both_library_types_reach_the_picker(self, client: TestClient, monkeypatch):
        """`PlexClient.sections()` filters to `("movie", "show")`, which is what closes this set — a
        music or photo library never gets this far. Both surviving cells are asserted; a third would
        mean the read stopped filtering."""
        from types import SimpleNamespace

        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "tok")
            session.commit()

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [
                    SimpleNamespace(key=1, title="Movies", type="movie"),
                    SimpleNamespace(key=2, title="TV Shows", type="show"),
                ]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        body = client.get("/api/system/libraries").json()

        assert [(x["key"], x["type"]) for x in body] == [("1", "movie"), ("2", "show")]

    def test_every_notification_severity_the_builders_emit_reaches_the_bell(self, client: TestClient, monkeypatch):
        """All three at once: `info` (an update is out), `warning` (runs paused) and `error` (the
        last run failed). The severities are typed as `audit.Level` rather than a Literal repeated in
        the router, so the audit levels and these can never drift into two vocabularies."""
        import shortlist.server.notifications as notif
        from shortlist.server.db.models import Run
        from shortlist.server.services.audit import LEVELS

        monkeypatch.setattr(notif, "check_for_update", lambda _v: {"latest": "9.9.9", "url": "https://example/rel"})
        with client.app.state.sessions() as session:
            SettingsStore(session).set("paused_all", True)
            session.add(Run(trigger="manual", status="error"))
            session.commit()

        items = client.get("/api/notifications").json()["notifications"]

        assert {n["severity"] for n in items} == LEVELS


class TestSseEventPayloadsAreDocumented:
    """`GET /api/events` is `text/event-stream`, so it has no response model — but the payload SHAPES
    are still declared (`api/schemas_events.py`) and hung off the route's OpenAPI `responses`, which
    is the one hook FastAPI offers for registering a schema no handler returns. Without that the SPA
    is left hand-writing all five, which is what `.claude/rules/frontend.md` forbids.

    Nothing validates an SSE frame at runtime, so these models are only as true as what asserts them.
    Both tests below therefore drive REAL publishers and validate what actually went on the bus.
    """

    def test_every_event_shape_reaches_the_openapi_components(self, client: TestClient):
        schemas = client.app.openapi()["components"]["schemas"]

        assert {
            "RunProgressEvent",
            "RunFinishedEvent",
            "UninstallProgressEvent",
            "SyncProgressEvent",
            "SyncFinishedEvent",
        } <= set(schemas)
        # The stream is labelled honestly too — an `application/json` body would be a lie about an
        # endpoint that never sends one.
        assert list(client.app.openapi()["paths"]["/api/events"]["get"]["responses"]["200"]["content"]) == [
            "text/event-stream"
        ]

    def test_a_real_run_publishes_the_progress_and_finished_shapes(self, client: TestClient, monkeypatch):
        """The run fails fast (no Plex configured here), which is the point: `run.finished` has to
        carry the `error` branch, and `status` is a Literal now — a fourth word would fail here."""
        import time

        from shortlist.server.api.schemas_events import RunFinishedEvent, RunProgressEvent

        published: list[tuple[str, dict]] = []
        real_publish = client.app.state.bus.publish
        monkeypatch.setattr(
            client.app.state.bus,
            "publish",
            lambda event, data: (published.append((event, data)), real_publish(event, data))[0],
        )

        client.post("/api/runs", json={"dry_run": True})
        for _ in range(100):
            if any(e == "run.finished" for e, _ in published):
                break
            time.sleep(0.05)

        by_event = {"run.progress": RunProgressEvent, "run.finished": RunFinishedEvent}
        seen = {event for event, _ in published if event in by_event}
        assert seen == set(by_event), "both run events must fire, or half the shape is untested"
        for event, data in published:
            if event in by_event:
                assert set(by_event[event].model_validate(data).model_dump(exclude_unset=True)) == set(data), data

    def test_a_real_uninstall_publishes_the_progress_shape(self, client: TestClient, monkeypatch):
        """Only a REAL uninstall streams — the dry-run preview is instant and emits nothing. The
        per-user restore lines are the ones carrying `done`/`total`, so the run needs a snapshot."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine import privacy
        from shortlist.server.api.schemas_events import UninstallProgressEvent
        from shortlist.server.db.models import RestrictionSnapshotRow, User

        with client.app.state.sessions() as session:
            user = session.query(User).first()
            user_id, account_id = user.id, user.plex_account_id
            session.add(RestrictionSnapshotRow(user_id=user_id, reason="initial", filters_before={"filterMovies": ""}))
            session.commit()

        monkeypatch.setattr(privacy, "restore_user_restrictions", lambda *a, **k: True)
        plex = MagicMock()
        plex.sections.return_value = []
        plextv = MagicMock()
        # A roster carrying that user: the restore resolves every snapshot against ONE read now, and
        # an empty roster is refused outright rather than read as "everyone left" (issue #96).
        plextv.list_users.return_value = [plextv_user(account_id, "sarah")]
        monkeypatch.setattr(
            client.app.state.run_service, "build_context", lambda **kw: SimpleNamespace(plex=plex, plextv=plextv)
        )
        published: list[tuple[str, dict]] = []
        real_publish = client.app.state.bus.publish
        monkeypatch.setattr(
            client.app.state.bus,
            "publish",
            lambda event, data: (published.append((event, data)), real_publish(event, data))[0],
        )

        assert client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).status_code == 200

        frames = [d for e, d in published if e == "uninstall.progress"]
        assert frames, "a real uninstall narrates itself"
        assert any("done" in f for f in frames), "the per-user restore line carries the progress count"
        for data in frames:
            assert set(UninstallProgressEvent.model_validate(data).model_dump(exclude_unset=True)) == set(data), data
