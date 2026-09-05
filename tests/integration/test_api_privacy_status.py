"""`GET /api/privacy/status` — what plex.tv and Plex say about hiding, RIGHT NOW.

The one rule this file exists to hold: **no verdict here may come from what Shortlist WROTE.**
`report.filter_writes` and the `run.privacy_sync` events record an intention that reached plex.tv,
not a fact about what plex.tv now stores. Reading one back as "hidden" would be a second
false-privacy bug, on the one screen whose whole job is to be believed. Every cell is a live read or
the words "not checked".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.models import OwnedRow
from shortlist.server.db.models import Run, User
from tests.integration.conftest import OWNER_ID

pytestmark = pytest.mark.integration

ACCOUNT_KEYS = {
    "user",
    "display_name",
    "slug",
    "account_id",
    "user_id",
    "user_type",
    "restriction_profile",
    "manage_sharing",
    "state",
    "hides",
    "should_hide",
    "missing",
    "other_conditions",
}


def _roster(monkeypatch, filters_by_user: dict[str, dict[str, str]], ids: dict[str, int] | None = None) -> None:
    """What plex.tv reports for every account, right now. Shared + Home users only — `list_users`
    never returns the owner, which is why the owner row is added by the endpoint."""
    from shortlist.server.services import privacy_status

    ids = ids or {}
    users = [
        SimpleNamespace(username=name, id=ids.get(name, 1000 + i), filters=filters)
        for i, (name, filters) in enumerate(filters_by_user.items())
    ]
    monkeypatch.setattr(
        privacy_status, "_plextv_client", lambda _store, _mid: SimpleNamespace(list_users=lambda: users)
    )


def _rows_on_plex(monkeypatch, slugs: list[str], *, marked_but_unlabelled: int = 0) -> None:
    """The per-person rows that exist on the PMS right now.

    `owned_row_surfaces` is NOT optional on this fake. `existing_row_labels` only consults it when
    the label read came back EMPTY — so a fake without it raised `AttributeError` inside the
    fail-safe's own `except`, and every test passing `[]` silently got `rows_error` set instead of
    the "no rows exist" answer it meant to set up. That made the one branch behind the SPA's "there's
    nothing for anyone to hide" banner unreachable from any test. A fake must be no easier than the
    real server; here it was harder, in the direction that hides a bug.
    """
    from shortlist.server.services import privacy_status

    owned = {slug: OwnedRow(label=f"shortlist_{slug}") for slug in slugs}
    surfaces = [{"marked": True}] * marked_but_unlabelled
    monkeypatch.setattr(
        privacy_status,
        "_plex_client",
        lambda _store: SimpleNamespace(
            owned_collections=lambda _prefix: owned,
            owned_row_surfaces=lambda flags=False: surfaces,
        ),
    )


def _seed_users(client: TestClient, people: list[dict]) -> None:
    with client.app.state.sessions() as session:
        for person in people:
            existing = session.query(User).filter_by(slug=person["slug"]).one_or_none()
            if existing is None:
                session.add(User(username=person["slug"], **person))
            else:
                for key, value in person.items():
                    setattr(existing, key, value)
        session.commit()


class TestTheVerdictIsAlwaysALiveRead:
    def test_a_verdict_is_never_taken_from_what_we_wrote(self, client: TestClient, monkeypatch):
        """THE test with teeth. A run records a SUCCESSFUL filter write for sarah's account, and
        plex.tv reports that account without the exclude. The only honest answer is "missing".

        Source `hides` from `run.stats["filter_writes"]` instead of the roster and this fails — which
        is exactly the `ebd4e48` false-privacy bug, moved to a new screen."""
        _seed_users(
            client,
            [
                {"slug": "sarah", "plex_account_id": 1000, "enabled": True},
                {"slug": "mike", "plex_account_id": 1001, "enabled": True},
            ],
        )
        with client.app.state.sessions() as session:
            session.add(
                Run(
                    status="ok",
                    trigger="manual",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    stats={
                        # We wrote it, plex.tv acknowledged it, and it is not there now.
                        "filter_writes": {"1000": {"username": "sarah", "fields": {"filterMovies": "ok"}}},
                    },
                )
            )
            session.commit()
        _rows_on_plex(monkeypatch, ["sarah", "mike"])
        _roster(monkeypatch, {"sarah": {"filterMovies": ""}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        sarah = next(a for a in body["accounts"] if a["slug"] == "sarah")
        assert sarah["missing"] == ["shortlist_mike"], "a recorded write was read back as a hide rule"
        assert sarah["hides"] == []
        assert sarah["state"] == "missing"

    def test_a_failed_plextv_read_reports_unknown_and_no_account_as_hiding(self, client: TestClient, monkeypatch):
        from shortlist.server.services import privacy_status

        def boom(_store, _mid):
            return SimpleNamespace(list_users=lambda: (_ for _ in ()).throw(TimeoutError("plex.tv timed out")))

        _seed_users(client, [{"slug": "sarah", "plex_account_id": 1000, "enabled": True}])
        _rows_on_plex(monkeypatch, ["sarah"])
        monkeypatch.setattr(privacy_status, "_plextv_client", boom)

        body = client.get("/api/privacy/status").json()

        assert body["error"], "a failed roster read must say so"
        assert body["accounts"] == [], "with no roster, nothing may be reported as hidden"

    def test_a_failed_plextv_read_does_not_leave_the_owner_row_behind(self, client: TestClient, monkeypatch):
        """The owner row needs no roster, so it would survive the outage — and the page's banner says
        "nothing below is current". That sentence has to be true of everything below it."""
        from shortlist.server.services import privacy_status

        _seed_users(client, [{"slug": "steve", "plex_account_id": OWNER_ID, "user_type": "owner", "enabled": True}])
        _rows_on_plex(monkeypatch, ["steve"])
        monkeypatch.setattr(
            privacy_status,
            "_plextv_client",
            lambda _store, _mid: SimpleNamespace(list_users=lambda: (_ for _ in ()).throw(TimeoutError("down"))),
        )

        body = client.get("/api/privacy/status").json()

        assert body["accounts"] == []
        assert body["summary"] == "unreadable"

    def test_a_failed_pms_row_read_withholds_the_verdict(self, client: TestClient, monkeypatch):
        """No list of rows means every account trivially hides all zero of them. Saying so beats
        reporting a clean bill of health off a failed read."""
        from shortlist.server.services import privacy_status

        _seed_users(client, [{"slug": "sarah", "plex_account_id": 1000, "enabled": True}])
        monkeypatch.setattr(
            privacy_status,
            "_plex_client",
            lambda _store: SimpleNamespace(
                owned_collections=lambda _prefix: (_ for _ in ()).throw(ConnectionError("PMS down"))
            ),
        )
        _roster(monkeypatch, {"sarah": {"filterMovies": ""}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        assert body["rows_error"], "a failed PMS read must be reported, not swallowed"
        assert body["rows_on_plex"] == []
        assert all(a["state"] != "hiding" for a in body["accounts"]), "nobody is 'hiding' off a failed read"

    def test_a_server_with_no_rows_yet_is_reported_as_clean_not_as_unknown(self, client: TestClient, monkeypatch):
        """The other half of the fail-safe, and the branch behind the SPA's "there's nothing for
        anyone to hide" banner. A read that SUCCEEDED and found nothing is a real answer; only a read
        that failed, or one contradicted by the title marker, is UNKNOWN."""
        _seed_users(client, [{"slug": "sarah", "plex_account_id": 1000, "enabled": True}])
        _rows_on_plex(monkeypatch, [])
        _roster(monkeypatch, {"sarah": {"filterMovies": ""}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        assert body["rows_error"] is None, "a successful read that found nothing is not an error"
        assert body["rows_on_plex"] == []
        assert body["summary"] == "clean"
        assert next(a for a in body["accounts"] if a["slug"] == "sarah")["state"] == "hiding"

    def test_rows_that_lost_their_labels_are_unknown_not_nothing_to_hide(self, client: TestClient, monkeypatch):
        """The most reassuring possible lie, at the API boundary. Rows exist by title marker but read
        as unlabelled — which is exactly the state where they are visible to everyone."""
        _seed_users(client, [{"slug": "sarah", "plex_account_id": 1000, "enabled": True}])
        _rows_on_plex(monkeypatch, [], marked_but_unlabelled=3)
        _roster(monkeypatch, {"sarah": {"filterMovies": ""}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        assert "carry no label" in body["rows_error"]
        assert body["summary"] == "rows_unknown"
        assert all(a["state"] == "unknown" for a in body["accounts"])

    def test_an_account_missing_an_exclude_names_the_rows_it_can_see(self, client: TestClient, monkeypatch):
        _seed_users(
            client,
            [
                {"slug": "sarah", "plex_account_id": 1000, "enabled": True},
                {"slug": "mike", "plex_account_id": 1001, "enabled": True},
                {"slug": "dan", "plex_account_id": 1002, "enabled": True},
            ],
        )
        _rows_on_plex(monkeypatch, ["sarah", "mike", "dan"])
        _roster(
            monkeypatch,
            {"sarah": {"filterMovies": "label!=shortlist_mike,shortlist_dan"}, "dan": {"filterMovies": ""}},
            ids={"sarah": 1000, "dan": 1002},
        )

        body = client.get("/api/privacy/status").json()

        sarah = next(a for a in body["accounts"] if a["slug"] == "sarah")
        dan = next(a for a in body["accounts"] if a["slug"] == "dan")
        assert sarah["missing"] == [] and sarah["state"] == "hiding"
        assert dan["missing"] == ["shortlist_mike", "shortlist_sarah"], "name the rows, don't just count them"


class TestTheThingsItRefusesToClaim:
    def test_the_owner_is_reported_as_a_plex_limitation_not_a_fault(self, client: TestClient, monkeypatch):
        """Plex has no share for the account that owns the server (rule 5). The owner sees every row,
        that is Plex, and rendering it as a fault would train the owner to ignore this screen."""
        _seed_users(
            client,
            [
                {"slug": "steve", "plex_account_id": OWNER_ID, "user_type": "owner", "enabled": True},
                {"slug": "sarah", "plex_account_id": 1000, "enabled": True},
            ],
        )
        _rows_on_plex(monkeypatch, ["steve", "sarah"])
        _roster(monkeypatch, {"sarah": {"filterMovies": "label!=shortlist_steve"}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        owner = next(a for a in body["accounts"] if a["user_type"] == "owner")
        assert owner["state"] == "owner", "the owner is never a fault"
        assert owner["missing"] == [], "the owner has no share, so nothing is 'missing' from it"
        assert body["summary"] != "missing", "an owner row must not make the whole server look broken"

    def test_a_left_alone_account_is_reported_as_a_setting_not_a_fault(self, client: TestClient, monkeypatch):
        _seed_users(
            client,
            [
                {"slug": "sarah", "plex_account_id": 1000, "enabled": True, "manage_sharing": False},
                {"slug": "mike", "plex_account_id": 1001, "enabled": True},
            ],
        )
        _rows_on_plex(monkeypatch, ["sarah", "mike"])
        _roster(monkeypatch, {"sarah": {"filterMovies": ""}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        sarah = next(a for a in body["accounts"] if a["slug"] == "sarah")
        assert sarah["state"] == "left_alone"
        assert sarah["missing"] == ["shortlist_mike"], "still reported truthfully — it is a setting, not a secret"
        assert body["summary"] == "clean", "a deliberate choice is not a leak"

    def test_an_account_with_a_parental_profile_is_reported_as_refused_by_plex(self, client: TestClient, monkeypatch):
        """plex.tv rejects the filter write outright for these (422, live-confirmed 2026-07-29), so
        no exclude can ever be stored and this screen may claim nothing about them."""
        _seed_users(
            client,
            [
                {
                    "slug": "kid",
                    "plex_account_id": 1000,
                    "enabled": True,
                    "user_type": "managed",
                    "restriction_profile": "little_kid",
                },
                {"slug": "mike", "plex_account_id": 1001, "enabled": True},
            ],
        )
        _rows_on_plex(monkeypatch, ["kid", "mike"])
        _roster(monkeypatch, {"kid": {"filterMovies": ""}}, ids={"kid": 1000})

        body = client.get("/api/privacy/status").json()

        kid = next(a for a in body["accounts"] if a["slug"] == "kid")
        assert kid["state"] == "refused_by_plex", "Plex refuses the write; that is not a hide rule we failed to send"


class TestEnforcement:
    """The look-through-their-eyes spot-check, read from the last run that actually MEASURED."""

    @staticmethod
    def _run(client: TestClient, *, stats: dict, minutes_ago: int) -> int:
        with client.app.state.sessions() as session:
            run = Run(
                status="ok",
                trigger="manual",
                started_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
                finished_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
                stats=stats,
            )
            session.add(run)
            session.commit()
            return run.id

    def test_no_run_has_measured_reports_not_checked_never_all_clear(self, client: TestClient, monkeypatch):
        _rows_on_plex(monkeypatch, [])
        _roster(monkeypatch, {})

        enforcement = client.get("/api/privacy/status").json()["enforcement"]

        assert enforcement["measured"] is False
        assert enforcement["run_id"] is None
        assert enforcement["not_enforced"] == {}, "empty AND unmeasured — the UI must not read this as clean"

    def test_enforcement_reads_the_latest_run_that_MEASURED_not_the_latest_run(self, client: TestClient, monkeypatch):
        """An errored run carries no measurement. Reading it as "clean" is exactly how a live alert
        gets cleared — the shape `test_notifications.py` already pins for the notification."""
        measured = self._run(client, stats={"filters_not_enforced": {"sarah": [21, 22]}}, minutes_ago=60)
        self._run(client, stats={"error": "plex.tv timed out"}, minutes_ago=5)
        _rows_on_plex(monkeypatch, [])
        _roster(monkeypatch, {})

        enforcement = client.get("/api/privacy/status").json()["enforcement"]

        assert enforcement["run_id"] == measured, "read the newest run that MEASURED, not the newest run"
        assert enforcement["measured"] is True
        assert enforcement["not_enforced"] == {"sarah": [21, 22]}

    def test_a_measured_exposure_outranks_a_stored_filter_in_the_headline(self, client: TestClient, monkeypatch):
        """The worst thing this screen can do, and the reason the summary exists.

        `_verify_filters_enforced` only spot-checks accounts that ALREADY carry our excludes
        (`pipeline.py:602` skips the rest) — so the exact state discussion #88 describes is: every
        filter is stored, every account reads `hiding`, `missing` is empty everywhere, and Plex is
        serving other people's rows anyway. A summary that only looks at `missing` calls that "clean"
        and prints "Every account hides all N rows that aren't theirs" directly above the red panel
        saying Plex is ignoring the filter."""
        self._run(client, stats={"filters_not_enforced": {"sarah": [21, 22]}}, minutes_ago=30)
        _seed_users(
            client,
            [
                {"slug": "sarah", "plex_account_id": 1000, "enabled": True},
                {"slug": "mike", "plex_account_id": 1001, "enabled": True},
            ],
        )
        _rows_on_plex(monkeypatch, ["sarah", "mike"])
        # Everything is stored correctly. That is exactly the point.
        _roster(monkeypatch, {"sarah": {"filterMovies": "label!=shortlist_mike"}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        assert next(a for a in body["accounts"] if a["slug"] == "sarah")["missing"] == []
        assert body["summary"] == "not_enforced", "a measured exposure must outrank a stored filter"

    def test_a_failed_read_still_outranks_a_measured_exposure(self, client: TestClient, monkeypatch):
        """Ranking, not a flat priority swap: with no roster nothing below is current, including
        which accounts the old measurement named."""
        from shortlist.server.services import privacy_status

        self._run(client, stats={"filters_not_enforced": {"sarah": [21]}}, minutes_ago=30)
        _rows_on_plex(monkeypatch, ["sarah"])
        monkeypatch.setattr(
            privacy_status,
            "_plextv_client",
            lambda _store, _mid: SimpleNamespace(list_users=lambda: (_ for _ in ()).throw(TimeoutError("down"))),
        )

        assert client.get("/api/privacy/status").json()["summary"] == "unreadable"

    def test_an_unmeasured_exposure_key_cannot_reach_the_headline(self, client: TestClient, monkeypatch):
        """No measuring run means no exposure to rank — "nobody looked" is not "somebody is exposed"."""
        _seed_users(client, [{"slug": "sarah", "plex_account_id": 1000, "enabled": True}])
        _rows_on_plex(monkeypatch, ["sarah"])
        _roster(monkeypatch, {"sarah": {"filterMovies": ""}}, ids={"sarah": 1000})

        assert client.get("/api/privacy/status").json()["summary"] == "clean"

    def test_a_clean_measurement_is_reported_as_measured_and_empty(self, client: TestClient, monkeypatch):
        """The empty dict is what lets a fixed server clear the alert, so it must survive as
        `measured: true` + `not_enforced: {}` rather than collapsing into "not checked"."""
        run_id = self._run(client, stats={"filters_not_enforced": {}}, minutes_ago=10)
        _rows_on_plex(monkeypatch, [])
        _roster(monkeypatch, {})

        enforcement = client.get("/api/privacy/status").json()["enforcement"]

        assert enforcement == {
            "measured": True,
            "run_id": run_id,
            "measured_at": enforcement["measured_at"],
            "not_enforced": {},
        }
        assert enforcement["measured_at"], "an owner has to know how old the reading is"


class TestTheEndpointContract:
    def test_the_status_endpoint_is_owner_gated_but_not_support_gated(self, client: TestClient, monkeypatch):
        """Support mode is off by default even for the owner. This screen must work without it —
        being buried behind support mode is the entire reason this item exists."""
        _rows_on_plex(monkeypatch, [])
        _roster(monkeypatch, {})

        assert client.get("/api/privacy/status").status_code == 200

        client.cookies.clear()
        assert client.get("/api/privacy/status").status_code in (401, 403)

    def test_every_account_renders_every_key(self, client: TestClient, monkeypatch):
        """A Pydantic response model FILTERS: a key it forgets to declare is dropped silently, and on
        this screen a dropped key reads as "nothing to hide"."""
        _seed_users(client, [{"slug": "sarah", "plex_account_id": 1000, "enabled": True}])
        _rows_on_plex(monkeypatch, ["sarah"])
        _roster(monkeypatch, {"sarah": {"filterMovies": "label!=Kids"}}, ids={"sarah": 1000})

        body = client.get("/api/privacy/status").json()

        assert set(body) == {"read_at", "accounts", "rows_on_plex", "rows_error", "error", "enforcement", "summary"}
        assert set(body["accounts"][0]) == ACCOUNT_KEYS
        assert body["accounts"][0]["other_conditions"] == ["filterMovies: label!=Kids"], (
            "the owner's own conditions are shown, so rule 3's byte-preservation is visible"
        )

    def test_the_support_tool_and_the_status_endpoint_come_from_one_computation(self, client: TestClient, monkeypatch):
        """Prevents the two drifting as one is fixed. Same fakes, same accounts, same verdict."""
        from shortlist.server.api import support as support_api
        from shortlist.server.services import privacy_status

        _seed_users(
            client,
            [
                {"slug": "sarah", "plex_account_id": 1000, "enabled": True},
                {"slug": "mike", "plex_account_id": 1001, "enabled": True},
            ],
        )
        _rows_on_plex(monkeypatch, ["sarah", "mike"])
        _roster(monkeypatch, {"sarah": {"filterMovies": ""}}, ids={"sarah": 1000})
        # The support tool builds its own clients; point them at the same fakes.
        monkeypatch.setattr(support_api, "_plex_client", privacy_status._plex_client)
        monkeypatch.setattr(support_api, "_plextv_client", privacy_status._plextv_client)
        monkeypatch.setattr(support_api, "_machine_id", lambda _session: "m1")
        assert client.post("/api/support/enable").status_code == 200

        status = client.get("/api/privacy/status").json()
        support = client.get("/api/support/sharing").json()

        assert [a["missing"] for a in status["accounts"] if a["state"] != "owner"] == [
            a["missing"] for a in support["accounts"]
        ]
        assert status["rows_on_plex"] == support["rows_on_plex"]
