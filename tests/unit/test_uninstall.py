"""Uninstall: dry-run preview vs real restore+delete, label gating, per-user audit events."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest
from fastapi.testclient import TestClient

from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Event, RestrictionSnapshotRow, Server, User
from shortlist.server.main import create_app

OWNER_ID = 555000001


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as test_client:
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="m1",
                    url="u",
                    token_enc="x",
                    version="1.43.3.10793",
                    owner_account_id=OWNER_ID,
                    plex_pass=True,
                    capabilities={},
                )
            )
            user = User(plex_account_id=555000100, username="sarah", slug="sarah", enabled=True)
            session.add(user)
            session.commit()
            session.add(
                RestrictionSnapshotRow(
                    user_id=user.id,
                    reason="initial",
                    filters_before={"filterMovies": "contentRating!=R", "filterTelevision": ""},
                    filters_after={},
                )
            )
            session.commit()
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ID, "username": "owner"})
        test_client.cookies.set(SESSION_COOKIE, cookie)
        test_client.headers[CSRF_HEADER] = "1"
        yield test_client


def fake_context(monkeypatch, client: TestClient) -> tuple[MagicMock, MagicMock]:
    """Stub build_context with a plex/plextv pair carrying one owned + one foreign collection.

    plextv persists writes so the engine's post-restore read-back verification is exercised
    for real rather than mocked away.
    """
    live_filters = {
        "filterAll": "",
        "filterMovies": "contentRating!=R|label!=Shortlist_mike",
        "filterTelevision": "",
        "filterMusic": "",
        "filterPhotos": "",
    }
    plextv = MagicMock()
    plextv.get_user.side_effect = lambda _id: SimpleNamespace(filters=dict(live_filters))
    plextv.update_user_filters.side_effect = lambda _id, fields: live_filters.update(fields)
    # The restore resolves every snapshot against ONE roster read (issue #96). Lazy, because the
    # write side_effect above mutates `live_filters` and the roster must reflect it when read.
    plextv.list_users.side_effect = lambda: [SimpleNamespace(id=555000100, filters=dict(live_filters))]
    plex = MagicMock()
    ours = MagicMock(ratingKey=1)
    ours.title = "✨ Picked for You"
    ours.labels = [SimpleNamespace(tag="Shortlist_sarah")]
    kometa = MagicMock(ratingKey=2)
    kometa.title = "Kometa Trending"
    kometa.labels = [SimpleNamespace(tag="Overlay")]
    section = MagicMock()
    section.collections.return_value = [ours, kometa]
    plex.sections.return_value = [section]

    def build_context(*, dry_run: bool):
        return SimpleNamespace(plex=plex, plextv=plextv, config=SimpleNamespace(dry_run=dry_run))

    monkeypatch.setattr(client.app.state.run_service, "build_context", build_context)
    return plex, plextv


class TestUninstall:
    def test_wrong_confirmation_rejected(self, client: TestClient):
        assert client.post("/api/system/uninstall", json={"confirm": "yes"}).status_code == 422

    def test_dry_run_previews_without_writing(self, client: TestClient, monkeypatch):
        plex, plextv = fake_context(monkeypatch, client)

        r = client.post("/api/system/uninstall", json={"dry_run": True})

        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is True
        assert body["collections_deleted"] == ["✨ Picked for You"]  # ours only — Kometa untouched
        assert body["filters_restored"] == 1
        assert "Preview only" in body["message"]
        plex.delete_owned_collection.assert_not_called()
        plextv.update_user_filters.assert_not_called()  # engine restore honored dry_run

    def test_real_uninstall_restores_filters_and_deletes_only_ours(self, client: TestClient, monkeypatch):
        plex, plextv = fake_context(monkeypatch, client)

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is False
        assert body["filters_restored"] == 1
        assert body["collections_deleted"] == ["✨ Picked for You"]
        assert "as we found it" in body["message"]
        # Filters restored to the snapshot values, byte-for-byte.
        call = plextv.update_user_filters.call_args
        assert call.args[1] == {"filterMovies": "contentRating!=R"}
        # Only the shortlist-labeled collection was deleted; the label gate is re-checked inside.
        assert plex.delete_owned_collection.call_count == 1
        deleted = plex.delete_owned_collection.call_args.args[0]
        assert deleted.title == "✨ Picked for You"

    def test_disables_every_row_and_clears_its_schedule_so_nothing_rebuilds(self, client, monkeypatch):
        """Uninstall must switch every row off AND clear its cron jobs — otherwise the next scheduled
        run rebuilds the collections it just deleted and re-applies the restrictions it just undid."""
        from shortlist.server.db.models import Collection
        from shortlist.server.scheduler import rebuild_schedule

        fake_context(monkeypatch, client)
        # A scheduled row → a live APScheduler cron job.
        with client.app.state.sessions() as session:
            session.add(Collection(slug="nightly", name="Nightly", enabled=True, schedule="30 3 * * *"))
            session.commit()
            enabled_before = session.query(Collection).filter_by(enabled=True).count()
        rebuild_schedule(client.app)
        jobs = [j for j in client.app.state.scheduler.get_jobs() if j.id.startswith("row-schedule::")]
        assert jobs, "the scheduled row should have a cron job before uninstall"

        # Dry-run counts what WOULD be switched off, and changes nothing.
        preview = client.post("/api/system/uninstall", json={"dry_run": True}).json()
        assert preview["rows_disabled"] == enabled_before
        with client.app.state.sessions() as session:
            assert session.query(Collection).filter_by(enabled=True).count() == enabled_before

        # The real thing switches every row off AND clears every cron job — nothing can rebuild.
        result = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).json()
        assert result["rows_disabled"] == enabled_before
        with client.app.state.sessions() as session:
            assert session.query(Collection).filter_by(enabled=True).count() == 0
        remaining = [j for j in client.app.state.scheduler.get_jobs() if j.id.startswith("row-schedule::")]
        assert remaining == [], "every row cron job must be gone after uninstall"

    def test_per_user_audit_events_recorded(self, client: TestClient, monkeypatch):
        fake_context(monkeypatch, client)
        client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})
        with client.app.state.sessions() as session:
            per_user = session.query(Event).filter_by(scope="uninstall.user").all()
            summary = session.query(Event).filter_by(scope="system.uninstall").all()
        assert len(per_user) == 1
        assert per_user[0].message["user"] == "sarah"
        assert per_user[0].message["restored_to"]["filterMovies"] == "contentRating!=R"
        assert len(summary) == 1


class TestUninstallSurvivesADepartedUser:
    """Issue #96: one account that had left Plex took the whole uninstall down with a 500.

    The restore asked plex.tv for each account as it went, and an account no longer on the share
    raised `LookupError` through a loop with no per-user guard. Everything after it was skipped —
    the remaining restores, the collection deletion, the row disabling — so the rows rebuilt on the
    next scheduled run, and the operator had already typed UNINSTALL.
    """

    def _add_second_user(self, client: TestClient, *, departed: bool) -> None:
        """A second user with a snapshot. `departed` stamps what the roster sweep would have: our own
        record that they are gone, which is what separates "left for good" from "could not reach"."""
        from datetime import UTC, datetime

        with client.app.state.sessions() as session:
            mike = User(
                plex_account_id=555000200,
                username="mike",
                slug="mike",
                enabled=True,
                departed_at=datetime.now(UTC) if departed else None,
            )
            session.add(mike)
            session.commit()
            session.add(
                RestrictionSnapshotRow(
                    user_id=mike.id,
                    reason="initial",
                    filters_before={"filterMovies": "", "filterTelevision": ""},
                    filters_after={},
                )
            )
            session.commit()

    def test_a_departed_account_does_not_abort_the_uninstall(self, client: TestClient, monkeypatch):
        """The headline regression. mike has left; sarah must still be restored AND the phases either
        side of the restore loop — deleting collections, disabling rows — must still happen."""
        plex, _plextv = fake_context(monkeypatch, client)
        self._add_second_user(client, departed=True)

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filters_restored"] == 1
        assert [s["user"] for s in body["filters_skipped"]] == ["mike"]
        assert body["filters_failed"] == []
        # The proof the loop did not abort: the other phases ran too.
        assert body["collections_deleted"] == ["✨ Picked for You"]
        plex.delete_owned_collection.assert_called_once_with(ANY, "shortlist")

    def test_an_account_the_roster_omits_without_corroboration_is_reported_as_needing_a_retry(
        self, client: TestClient, monkeypatch
    ):
        """A truncated roster read looks exactly like a departure, and uninstall is ONE SHOT — it
        clears every schedule, so unlike the nightly sweep there is no later pass to self-correct. An
        account our records say is here must therefore be reported as retryable, never as gone."""
        fake_context(monkeypatch, client)
        self._add_second_user(client, departed=False)

        body = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).json()

        assert body["filters_skipped"] == [], "an unexplained absence is not a departure"
        assert body["filters_failed"] == [], "nor is it a refused write — a consumer must not have to guess"
        assert [u["user"] for u in body["filters_unreachable"]] == ["mike"]
        assert "did not list" in body["message"]

    def test_the_report_does_not_claim_the_server_is_untouched_when_it_skipped_someone(
        self, client: TestClient, monkeypatch
    ):
        """ "Your server is as we found it" is the claim the operator is trusting. It is only true
        when it is true — a departed account's filters stay as Shortlist left them, for ever."""
        fake_context(monkeypatch, client)
        self._add_second_user(client, departed=True)

        body = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).json()

        assert "as we found it" in body["message"]
        assert "since left" in body["message"], body["message"]

    def test_the_summary_line_mentions_both_kinds_of_leftover_account(self, client: TestClient, monkeypatch):
        """The two caveats COMPOSE. Short-circuiting on failures dropped the departed accounts from
        every consumer that reads this line rather than the page — the API and the audit event."""
        _plex, plextv = fake_context(monkeypatch, client)
        self._add_second_user(client, departed=True)
        plextv.update_user_filters.side_effect = RuntimeError("plex.tv said no")

        body = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).json()

        assert "could not be restored" in body["message"]
        assert "since left" in body["message"], body["message"]

    def test_one_accounts_failure_does_not_cost_the_others_their_restore(self, client: TestClient, monkeypatch):
        """Same bug class, different trigger: plex.tv refusing ONE write must not strand everybody
        after them. The operator can retry, but only for the accounts that actually failed."""
        plex, plextv = fake_context(monkeypatch, client)
        self._add_second_user(client, departed=False)
        live = {"filterMovies": "contentRating!=R|label!=Shortlist_mike", "filterTelevision": ""}
        plextv.list_users.side_effect = lambda: [
            SimpleNamespace(id=555000100, filters=dict(live)),
            SimpleNamespace(id=555000200, filters={"filterMovies": "label!=Shortlist_sarah", "filterTelevision": ""}),
        ]
        plextv.get_user.side_effect = lambda _id: SimpleNamespace(filters=dict(live))

        def write(account_id, fields):
            if account_id == 555000200:
                raise RuntimeError("plex.tv said no")
            live.update(fields)

        plextv.update_user_filters.side_effect = write

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filters_restored"] == 1, "sarah's restore must still have gone through"
        assert [f["user"] for f in body["filters_failed"]] == ["mike"]
        plex.delete_owned_collection.assert_called_once_with(ANY, "shortlist")

    def test_a_write_plextv_accepted_but_we_could_not_verify_is_still_audited(self, client: TestClient, monkeypatch):
        """Rule 10. The read-back disagreeing means the PUT landed and the account's filters have
        already changed — reporting only an error string would leave that write recorded nowhere."""
        _plex, plextv = fake_context(monkeypatch, client)
        plextv.update_user_filters.side_effect = lambda _id, fields: None  # accepted, never applied

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 200, r.text
        assert [f["user"] for f in r.json()["filters_failed"]] == ["sarah"]
        with client.app.state.sessions() as session:
            events = [e.message for e in session.query(Event).filter_by(scope="uninstall.user").all()]
        assert events and events[0]["verified"] is False
        assert events[0]["attempted"] == {"filterMovies": "contentRating!=R"}, events

    def test_rows_are_switched_off_before_anything_touches_plex(self, client: TestClient, monkeypatch):
        """Disabling rows LAST meant a failure in any Plex phase left the schedule armed, so the
        nightly run rebuilt the collections and re-merged the excludes the operator just removed —
        the "silently reinstalling Shortlist" outcome the flow exists to prevent."""
        from shortlist.server.db.models import Collection

        plex, _plextv = fake_context(monkeypatch, client)
        with client.app.state.sessions() as session:
            session.add(Collection(slug="second-row", name="Another row", enabled=True))
            session.commit()
            armed = session.query(Collection).filter_by(enabled=True).count()
        assert armed, "the test needs at least one armed row to prove anything"
        plex.sections.side_effect = RuntimeError("PMS went away mid-uninstall")

        assert client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).status_code == 500

        with client.app.state.sessions() as session:
            assert session.query(Collection).filter_by(enabled=True).count() == 0, (
                "a failed uninstall left rows armed to rebuild what it deleted"
            )

    def test_collections_are_deleted_before_the_excludes_hiding_them_are_removed(self, client: TestClient, monkeypatch):
        """Rule 1 in reverse. Restoring first strips each account's `label!=shortlist_*` while the
        rows still exist and are still promoted — issue #88's exact state — for as long as a full
        section walk takes. Deleting first cannot leak: a failure leaves the excludes in place."""
        plex, plextv = fake_context(monkeypatch, client)
        order: list[str] = []
        plex.delete_owned_collection.side_effect = lambda *a, **k: order.append("delete")
        plextv.update_user_filters.side_effect = lambda *a, **k: order.append("restore")

        client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert order == ["delete", "restore"], order

    def test_a_write_that_may_have_landed_is_audited_even_when_the_put_itself_times_out(
        self, client: TestClient, monkeypatch
    ):
        """The ambiguous half of rule 10, and the one where the audit row is the ONLY artifact.
        `update_user_filters` retries what is safe to resend (a full-value PUT converges) and lets a
        READ timeout through, because the write may have applied — so it must carry what it sent."""
        _plex, plextv = fake_context(monkeypatch, client)
        plextv.update_user_filters.side_effect = TimeoutError("timed out reading the response")

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 200, r.text
        with client.app.state.sessions() as session:
            events = [e.message for e in session.query(Event).filter_by(scope="uninstall.user").all()]
        assert events, "a write that may have landed on plex.tv was recorded nowhere"
        assert events[0]["verified"] is False
        assert events[0]["attempted"] == {"filterMovies": "contentRating!=R"}

    def test_the_preview_says_when_plextv_could_not_see_some_accounts(self, client: TestClient, monkeypatch):
        """The dry run is the rehearsal the FAQ tells people to trust (rule 8). Swallowing "plex.tv
        could not see N of your accounts" hides the one signal that should stop an operator from
        typing UNINSTALL."""
        _plex, plextv = fake_context(monkeypatch, client)
        plextv.list_users.side_effect = lambda: []

        body = client.post("/api/system/uninstall", json={"dry_run": True}).json()

        assert [u["user"] for u in body["filters_unreachable"]] == ["sarah"]
        assert "Preview only" in body["message"]
        assert "listed none" in body["message"], body["message"]

    def test_an_owner_who_stopped_sharing_with_everyone_is_not_told_to_retry_for_ever(
        self, client: TestClient, monkeypatch
    ):
        """plex.tv listing NONE of our accounts has two readings and nothing can tell them apart.
        "Run it again to retry" sends an owner who really has un-shared everything round a loop that
        can never close: `user_sync` never stamps `departed_at` from an empty roster, so their
        accounts can never become corroborated."""
        _plex, plextv = fake_context(monkeypatch, client)
        plextv.list_users.side_effect = lambda: []

        body = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).json()

        assert "stopped sharing this server with those people" in body["message"], body["message"]
        assert "nothing left to put back" in body["message"]

    def test_a_run_in_flight_blocks_the_uninstall_rather_than_racing_it(self, client: TestClient, monkeypatch):
        """Clearing the schedule does not cancel a run already going. That run keeps merging
        `label!=shortlist_*` from a roster it loaded earlier, straight into accounts this uninstall
        has just restored — the read-modify-write clobber rule 3 exists for."""
        fake_context(monkeypatch, client)
        monkeypatch.setattr(client.app.state.run_service, "is_running", lambda: True)

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 409
        assert "run is in progress" in r.json()["detail"]

    def test_a_context_that_cannot_be_built_still_reports_that_rows_were_switched_off(
        self, client: TestClient, monkeypatch
    ):
        """Phase 1 changes real state before Plex is ever reached, so a failure to build the client
        must not escape as a bare 500 that says nothing about what it already did."""
        monkeypatch.setattr(
            client.app.state.run_service,
            "build_context",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("no Plex server is linked")),
        )

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 500
        assert "switched off" in r.json()["detail"]
        with client.app.state.sessions() as session:
            summary = session.query(Event).filter_by(scope="system.uninstall").all()
        assert summary and "no Plex server is linked" in summary[0].message["stopped_by"]

    def test_a_second_run_with_a_healthy_roster_is_not_told_plextv_listed_nobody(self, client: TestClient, monkeypatch):
        """The instructed path. The page says "run the uninstall again to retry", and on that second
        run everyone already matches their snapshot — so `filters_restored` is 0 while plex.tv listed
        every account perfectly. Keyed on restores rather than on the roster, this told an operator
        with a healthy roster that they might have unshared everybody: the opposite of the truth, and
        it stops them retrying the one account that really does still carry our labels."""
        _plex, plextv = fake_context(monkeypatch, client)
        self._add_second_user(client, departed=False)
        # sarah is listed and already matches her snapshot; mike is the one genuine omission.
        plextv.list_users.side_effect = lambda: [
            SimpleNamespace(id=555000100, filters={"filterMovies": "contentRating!=R", "filterTelevision": ""})
        ]

        body = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).json()

        assert body["filters_restored"] == 0, "the premise: nothing needed a write on a second run"
        assert [u["user"] for u in body["filters_unreachable"]] == ["mike"]
        assert "listed none" not in body["message"], body["message"]
        assert "did not list 1 account" in body["message"], body["message"]

    def test_an_owner_who_wound_down_one_account_at_a_time_gets_the_both_readings_message(
        self, client: TestClient, monkeypatch
    ):
        """The realistic way a share is wound down. `user_sync` corroborates every unshare except the
        LAST — that one empties the roster, and an empty roster never stamps `departed_at` — so this
        owner ends with corroborated departures AND one uncorroborated account. Suppressing the
        both-readings copy whenever anything was corroborated sent them back to a retry for ever."""
        _plex, plextv = fake_context(monkeypatch, client)
        self._add_second_user(client, departed=True)
        plextv.list_users.side_effect = lambda: []

        body = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).json()

        assert [u["user"] for u in body["filters_unreachable"]] == ["sarah"]
        assert [s["user"] for s in body["filters_skipped"]] == ["mike"]
        assert "listed none of the 2 accounts on file" in body["message"], body["message"]

    def test_uninstall_holds_the_one_writer_lock_while_it_touches_plex(self, client: TestClient, monkeypatch):
        """Uninstall deletes collections and merges share filters, so it is a Plex writer like any
        other. Without the lock a privacy sync firing mid-uninstall re-merges the excludes onto
        accounts the restore loop has already put back — a read-modify-write clobber (rule 3) that
        nothing catches, since the Privacy Check was removed in 2026-07."""
        import asyncio

        from shortlist.server.services import jobs

        plex, _plextv = fake_context(monkeypatch, client)
        lock = asyncio.Lock()
        monkeypatch.setattr(jobs, "plex_writer_lock", lambda: lock)
        held: list[bool] = []
        sections = plex.sections.return_value
        plex.sections.side_effect = lambda: (held.append(lock.locked()), sections)[1]

        assert client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).status_code == 200

        assert held == [True], "the Plex phase ran without the one-writer lock held"

    def test_a_preview_does_not_take_the_one_writer_lock(self, client: TestClient, monkeypatch):
        """A dry run writes nothing, so it has no business holding the one-writer lock — and taking
        it would quietly undo the `not dry_run` gate on the 409 above, leaving the preview spinning
        for the length of a run with nothing on screen to explain why."""
        import asyncio

        from shortlist.server.services import jobs

        plex, _plextv = fake_context(monkeypatch, client)
        lock = asyncio.Lock()
        monkeypatch.setattr(jobs, "plex_writer_lock", lambda: lock)
        held: list[bool] = []
        sections = plex.sections.return_value
        plex.sections.side_effect = lambda: (held.append(lock.locked()), sections)[1]

        assert client.post("/api/system/uninstall", json={"dry_run": True}).status_code == 200

        assert held == [False], "a preview took the one-writer lock"

    def test_a_uninstall_that_dies_partway_still_records_what_it_changed(self, client: TestClient, monkeypatch):
        """Rule 10: every write is auditable. The audit events were written AFTER the whole job, so an
        exception in any later phase threw away the record of what had already changed on Plex —
        "what changed on whose share at 03:31" was unanswerable."""
        plex, _plextv = fake_context(monkeypatch, client)
        plex.delete_owned_collection.side_effect = RuntimeError("PMS refused the delete")

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 500
        with client.app.state.sessions() as session:
            summary = session.query(Event).filter_by(scope="system.uninstall").all()
        assert summary, "a partial uninstall must still leave an audit row"
        assert summary[0].message["collections_deleted"] == ["✨ Picked for You"]
        assert "PMS refused the delete" in summary[0].message["stopped_by"]
