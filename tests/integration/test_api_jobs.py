"""API contract tests: the background jobs API, failed-job notifications, share-filter passes queued
by mutations, the job kind catalogue, the schedule API, and backup/restore."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from shortlist.server.auth import SESSION_COOKIE
from shortlist.server.db.models import User
from shortlist.server.settings_store import SettingsStore
from tests.integration.conftest import OWNER_JSON

pytestmark = pytest.mark.integration


class TestJobsApi:
    """`/system/jobs` — the "did that actually happen?" surface for background maintenance.

    Runs keep their own page; this covers everything else, which until now landed only in the logs
    and the events table.
    """

    def test_the_list_is_empty_before_anything_runs(self, client: TestClient):
        r = client.get("/api/system/jobs")
        assert r.status_code == 200
        assert r.json() == []

    def test_a_triggered_job_runs_and_then_appears_in_the_list(self, client: TestClient):
        r = client.post("/api/system/jobs", json={"kind": "sync.check"})
        assert r.status_code == 200
        assert r.json()["kind"] == "sync.check"
        # Drained inline, so the button feels instant rather than leaving a queued row behind.
        assert r.json()["status"] in {"done", "failed", "queued"}
        # Spelled out because both endpoints now declare Pydantic response models, and a model that
        # missed a key would DROP it from the payload with nothing failing — `fixed`/`orphans` are
        # the sync-check preview an operator reads before authorising a real, deleting pass.
        assert set(r.json()) == {"id", "kind", "status", "detail", "error", "fixed", "orphans"}

        listed = client.get("/api/system/jobs").json()
        assert [j["kind"] for j in listed] == ["sync.check"]
        assert listed[0]["created_at"]
        assert set(listed[0]) == {
            "id",
            "kind",
            "status",
            "attempts",
            "max_attempts",
            "detail",
            "error",
            "payload",
            "result",
            "created_at",
            "started_at",
            "finished_at",
        }

    def test_the_job_catalogue_carries_a_whole_card_per_kind(self, client: TestClient):
        """The Jobs page renders a card per kind from this one request; a dropped key is a card that
        silently loses its schedule, its counts or its last result."""
        client.post("/api/system/jobs", json={"kind": "sync.check"})

        entries = client.get("/api/system/jobs/catalog").json()

        by_kind = {e["kind"]: e for e in entries}
        for entry in entries:
            assert set(entry) == {
                "kind",
                "label",
                "description",
                "manual",
                "trigger",
                "scheduled",
                "schedule_optional",
                "schedule_setting",
                "next_run",
                "last",
                "total",
                "queued",
                "running",
                "failed",
            }, entry["kind"]
        # `last` is a nested job, and null for a kind that has never run — both branches, one request.
        assert set(by_kind["sync.check"]["last"]) == set(client.get("/api/system/jobs").json()[0])
        assert by_kind["backup.take"]["last"] is None

    def test_an_unknown_kind_is_rejected(self, client: TestClient):
        assert client.post("/api/system/jobs", json={"kind": "nope"}).status_code == 422

    def test_a_destructive_kind_cannot_be_triggered_from_the_ui(self, client: TestClient):
        """`user.cleanup` DELETES a person's rows and takes a slug. It is a real handler, but it must
        never be reachable from a generic 'run a job' button — hence an allow-list, not the handler
        registry."""
        from shortlist.server.services import jobs

        assert "user.cleanup" in jobs._HANDLERS  # it exists...
        assert "user.cleanup" not in jobs.KINDS  # ...but is not triggerable
        assert (
            client.post("/api/system/jobs", json={"kind": "user.cleanup", "payload": {"slug": "sarah"}}).status_code
            == 422
        )
        # `user.restore` is on the same list for the opposite reason: it is the one job that makes a
        # row MORE visible. Reachable from a generic button, it would let someone be promoted onto
        # Plex outside the un-pause that is supposed to be the only way in.
        assert "user.restore" in jobs._HANDLERS and "user.restore" not in jobs.KINDS
        assert (
            client.post("/api/system/jobs", json={"kind": "user.restore", "payload": {"slug": "sarah"}}).status_code
            == 422
        )
        assert "row.reconcile" in jobs._HANDLERS and "row.reconcile" not in jobs.KINDS

    def test_a_failing_job_is_reported_rather_than_500ing(self, client: TestClient, monkeypatch):
        """A Plex outage during a manual job must surface as a failed job, not an API error."""

        def explode(*a, **k):
            raise RuntimeError("Plex is down")

        monkeypatch.setattr(client.app.state.run_service, "build_context", explode)

        r = client.post("/api/system/jobs", json={"kind": "sync.check"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] in {"queued", "failed"}  # queued = will be retried by the worker
        assert "Plex is down" in (body["error"] or "")


class TestFailedJobNotifications:
    """A job that exhausts its retries must reach the notification bell.

    Without it the retry machinery is invisible exactly when it matters: these are the destructive
    and privacy-relevant jobs — removing a disabled user's rows, hiding a paused user's, writing
    share filters — so a silent failure leaves Plex in a state the operator believes was corrected.
    """

    def _fail_a_job(self, client: TestClient, kind: str = "user.cleanup") -> int:
        from shortlist.server.db.models import Job

        with client.app.state.sessions() as session:
            job = Job(kind=kind, status="failed", attempts=3, max_attempts=3, error="Plex unreachable")
            session.add(job)
            session.commit()
            return job.id

    def test_a_failed_job_raises_a_notification(self, client: TestClient):
        self._fail_a_job(client)

        notes = client.get("/api/notifications").json()["notifications"]

        job_note = next((n for n in notes if n["id"].startswith("failed-jobs-")), None)
        assert job_note is not None
        assert job_note["severity"] == "error"
        assert "user.cleanup" in job_note["body"]
        assert job_note["action_url"] == "/jobs"

    def test_a_job_still_retrying_raises_nothing(self, client: TestClient):
        """`queued` after a failed attempt means the queue is still working on it — telling the
        operator it failed would be wrong, and would cry wolf on every transient blip."""
        from shortlist.server.db.models import Job

        with client.app.state.sessions() as session:
            session.add(Job(kind="user.cleanup", status="queued", attempts=1, max_attempts=3, error="timeout"))
            session.commit()

        notes = client.get("/api/notifications").json()["notifications"]

        assert not [n for n in notes if n["id"].startswith("failed-jobs-")]

    def test_a_newer_failure_resurfaces_after_a_dismissal(self, client: TestClient):
        """The id encodes the newest failed job, so dismissing one failure cannot hide the next."""
        first = self._fail_a_job(client)
        notes = client.get("/api/notifications").json()["notifications"]
        note_id = next(n["id"] for n in notes if n["id"].startswith("failed-jobs-"))
        assert note_id == f"failed-jobs-{first}"

        client.post("/api/notifications/dismiss", json={"id": note_id})
        assert not [
            n for n in client.get("/api/notifications").json()["notifications"] if n["id"].startswith("failed-jobs-")
        ]

        second = self._fail_a_job(client, kind="privacy.sync")
        after = client.get("/api/notifications").json()["notifications"]
        assert next(n["id"] for n in after if n["id"].startswith("failed-jobs-")) == f"failed-jobs-{second}"


class TestFilterWritesAreQueuedWhenOwed:
    """Every mutation that changes WHO MAY SEE WHAT must queue the share-filter pass that enforces it.

    These are the "config says one thing, Plex still says another" bugs. They share a shape: the
    handler updates the database, returns 200, and nothing ever writes the `label!=` excludes that
    make the change real — so the person who was just removed keeps seeing the row until the next
    nightly run, and an operator watching the UI has no way to tell.

    Asserted at the JOB boundary rather than by spying on a helper: the whole mechanism is the
    enqueue, so mocking it out would let the enqueue be deleted with the test still passing.
    """

    @pytest.fixture
    def plextv(self, client: TestClient):
        """The plex.tv roster at the HTTP boundary — the same recorded fixture TestUserSync replays,
        so the departure sweep is tested against a real response shape (rule 11)."""
        from shortlist.server.settings_store import SettingsStore

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.commit()
        users_xml = (Path(__file__).parent.parent / "fixtures" / "plextv_users.xml.txt").read_text()
        with respx.mock:
            respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=users_xml))
            respx.get("https://plex.tv/api/v2/user").mock(return_value=httpx.Response(200, json=dict(OWNER_JSON)))
            yield

    def _kinds(self, client: TestClient) -> list[str]:
        """Queued kinds, OLDEST FIRST. Order is load-bearing on the restore path, so it is asserted."""
        return [j["kind"] for j in reversed(client.get("/api/system/jobs").json())]

    def _sarah_id(self, client: TestClient) -> int:
        return next(u["id"] for u in client.get("/api/users").json() if u["slug"] == "sarah")

    def test_narrowing_a_shared_rows_audience_queues_the_filter_pass(self, client: TestClient):
        """A shared row is ONE collection for everyone, so removing somebody from its audience cannot
        be done by deleting anything — the only mechanism that hides it from one account is a
        `label!=` exclude on that account's share.

        This gap was gated on `build == "per_person"`, which is never true for a shared row, so
        narrowing the audience changed the config and NOTHING else. The dropped accounts kept the row
        on their Home until the next nightly run."""
        created = client.post(
            "/api/collections",
            json={"name": "Popular", "build": "shared", "audience": "subset", "audience_user_ids": [1, 2]},
        )
        assert created.status_code == 201
        cid = created.json()["id"]

        r = client.patch(
            f"/api/collections/{cid}", json={"name": "Popular", "audience": "subset", "audience_user_ids": [1]}
        )
        assert r.status_code == 200

        assert "privacy.sync" in self._kinds(client)

    def test_widening_a_shared_rows_audience_queues_it_too(self, client: TestClient):
        """The other direction is equally stale: someone just ADDED to the audience still carries the
        exclude that was hiding the row from them, and pruning it is a filter write like any other."""
        created = client.post(
            "/api/collections",
            json={"name": "Popular", "build": "shared", "audience": "subset", "audience_user_ids": [1]},
        )
        cid = created.json()["id"]

        r = client.patch(
            f"/api/collections/{cid}", json={"name": "Popular", "audience": "subset", "audience_user_ids": [1, 2]}
        )
        assert r.status_code == 200

        assert "privacy.sync" in self._kinds(client)

    def test_an_unchanged_shared_audience_queues_nothing(self, client: TestClient):
        """A filter pass is throttled plex.tv traffic across every account on the server. Re-sending
        the same audience — which the row editor does on every save — must not trigger one."""
        created = client.post(
            "/api/collections",
            json={"name": "Popular", "build": "shared", "audience": "subset", "audience_user_ids": [1]},
        )
        cid = created.json()["id"]

        r = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Popular", "audience": "subset", "audience_user_ids": [1], "size": 15},
        )
        assert r.status_code == 200

        assert self._kinds(client) == []

    def test_disabling_someone_writes_their_filters_as_well_as_removing_their_rows(self, client: TestClient):
        """Removing the collections is only half of "disabled". The other half is their OWN share
        filter: with `privacy.hide_shared_from_disabled`, an opted-out account must stop seeing even
        the public shared rows, and only a filter write does that.

        Order matters and is asserted: the cleanup deletes the collections FIRST, so the privacy pass
        computes each account's excludes from what is actually left on the server."""
        uid = self._sarah_id(client)

        client.patch(f"/api/users/{uid}", json={"enabled": False})

        assert self._kinds(client) == ["user.cleanup", "privacy.sync"]

    def test_disabling_everyone_queues_one_filter_pass_not_one_per_person(self, client: TestClient):
        """The pass rewrites EVERY account's filter, so N of them is N times the plex.tv traffic for
        an identical result. One cleanup per user, one pass at the end."""
        client.post("/api/users/set-enabled", json={"enabled": True})
        before = len(self._kinds(client))  # turning everyone ON queues a pass of its own

        client.post("/api/users/set-enabled", json={"enabled": False})

        assert self._kinds(client)[before:] == ["user.cleanup", "user.cleanup", "privacy.sync"]

    def test_re_enabling_someone_gives_back_the_shared_rows_that_disabling_hid(self, client: TestClient):
        """Disabling now writes excludes that hide every shared row from that account. Without the
        mirror on the way back, turning them on again left those excludes in place until the next run
        — so "off then on" was not a round trip."""
        uid = self._sarah_id(client)
        client.patch(f"/api/users/{uid}", json={"enabled": False})

        client.patch(f"/api/users/{uid}", json={"enabled": True})

        assert self._kinds(client)[-1] == "privacy.sync"

    def test_un_pausing_queues_one_job_that_owns_its_own_ordering(self, client: TestClient):
        """Restoring an un-paused user is the one maintenance path that makes a row MORE visible, so
        plex-safety rule 1's ordering applies outside a run too: excludes first, promotion second.

        It is ONE job because two would not have been ordered. `_claim` steps over a job whose retry
        backoff has not elapsed and takes the next one — so a `privacy.sync` that failed against a 503
        plex.tv would be skipped and the promotion behind it would land anyway, against a healthy PMS,
        with the excludes unmerged. `user.restore` therefore does the merge itself; the ordering test
        that matters lives in tests/unit/test_jobs.py."""
        uid = self._sarah_id(client)
        client.patch(f"/api/users/{uid}", json={"prefs": {"paused": True}})

        client.patch(f"/api/users/{uid}", json={"prefs": {"paused": False}})

        assert self._kinds(client) == ["user.hide", "user.restore"]

    def test_someone_who_leaves_the_share_is_turned_off_and_cleaned_up(self, client: TestClient, plextv):
        """A user who disappears from the plex.tv roster has lost access to the server, but nothing
        noticed: they stayed enabled, so every run tried their dead share token, and their collection
        sat promoted to Shared Home with no user left to see it.

        Turning them off reuses the whole tested disable path and keeps their history, so the owner
        can switch them back on if they return."""
        with client.app.state.sessions() as session:
            session.add(User(plex_account_id=999999, username="gone", slug="gone", enabled=True))
            session.commit()

        client.post("/api/users/sync")

        assert next(u for u in client.get("/api/users").json() if u["slug"] == "gone")["enabled"] is False
        assert "user.cleanup" in self._kinds(client)

    def test_an_empty_roster_disables_nobody(self, client: TestClient):
        """ "plex.tv returned nothing" is indistinguishable from "nobody shares this server any more",
        and one of those readings switches off every user and queues deletion of all their collections.

        This runs UNATTENDED on the daily user sync, and the disable commits before the cleanup jobs
        even run — so a Plex outage would not save anyone; every account would have to be turned back
        on by hand. An incomplete picture must therefore do nothing, exactly as converge gates orphan
        deletion on a non-empty user list."""
        from shortlist.server.settings_store import SettingsStore

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.commit()
        with respx.mock:
            respx.get("https://plex.tv/api/users").mock(
                return_value=httpx.Response(200, text='<MediaContainer size="0" />')
            )
            respx.get("https://plex.tv/api/v2/user").mock(return_value=httpx.Response(200, json=dict(OWNER_JSON)))

            client.post("/api/users/sync")

        assert [u["enabled"] for u in client.get("/api/users").json() if u["slug"] == "sarah"] == [True]
        assert "user.cleanup" not in self._kinds(client)

    def test_a_roster_missing_half_the_server_disables_nobody(self, client: TestClient):
        """A TRUNCATED read is as indistinguishable from a mass departure as an empty one, and just as
        destructive: every one of those people is switched off and their collections queued for
        deletion. One person leaving is routine and still acts immediately; half of them at once is not
        a thing that happens, so it is refused and reported instead."""
        from shortlist.server.db.models import Event
        from shortlist.server.settings_store import SettingsStore

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.add(User(plex_account_id=555000300, username="ana", slug="ana", enabled=True))
            session.query(User).filter_by(slug="mike").update({"enabled": True})
            session.commit()
        # The roster names only sarah — so mike and ana (2 of 3 enabled) would be departed.
        one_user = '<MediaContainer size="1"><User id="555000100" title="sarah" username="sarah"/></MediaContainer>'
        with respx.mock:
            respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=one_user))
            respx.get("https://plex.tv/api/v2/user").mock(return_value=httpx.Response(200, json=dict(OWNER_JSON)))

            client.post("/api/users/sync")

        assert {u["slug"] for u in client.get("/api/users").json() if u["enabled"]} >= {"mike", "ana"}
        assert "user.cleanup" not in self._kinds(client)
        with client.app.state.sessions() as session:
            assert session.query(Event).filter_by(scope="user.departed.refused").count() == 1

    def test_one_person_leaving_the_share_still_acts_immediately(self, client: TestClient):
        """The guard must not swallow the case it exists to serve. One departure out of three is a
        person being removed from the share, not a bad read."""
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.add(User(plex_account_id=555000300, username="ana", slug="ana", enabled=True))
            session.query(User).filter_by(slug="mike").update({"enabled": True})
            session.commit()
        two_of_three = (
            '<MediaContainer size="2">'
            '<User id="555000100" title="sarah" username="sarah"/>'
            '<User id="555000200" title="mike" username="mike"/>'
            "</MediaContainer>"
        )
        with respx.mock:
            respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=two_of_three))
            respx.get("https://plex.tv/api/v2/user").mock(return_value=httpx.Response(200, json=dict(OWNER_JSON)))

            client.post("/api/users/sync")

        assert next(u for u in client.get("/api/users").json() if u["slug"] == "ana")["enabled"] is False
        assert "user.cleanup" in self._kinds(client)

    def test_someone_still_on_the_share_is_left_enabled(self, client: TestClient, plextv):
        """The departure sweep reads the roster, so a normal sync must leave everybody alone — mass
        auto-disabling on a transient plex.tv shape change would be far worse than the bug it fixes."""
        client.patch(f"/api/users/{self._sarah_id(client)}", json={"enabled": True})

        client.post("/api/users/sync")

        assert next(u for u in client.get("/api/users").json() if u["slug"] == "sarah")["enabled"] is True


class TestBackupRestoreSaysWhatItChanges:
    """A restore is not a neutral rollback. The database is what decides WHO MAY SEE WHAT, so
    restoring a copy taken before a shared row's audience was narrowed puts the wider audience back —
    and the shared-exclude prune, the only un-hiding path Shortlist has, then removes the `label!=`
    excludes that were hiding it on the next run.

    That is correct for the config being restored, and it is exactly the kind of change nobody expects
    from a button labelled "Restore". So it is said out loud, and audited.
    """

    def test_the_response_names_the_visibility_change_and_audits_it(self, client: TestClient):
        from shortlist.server.db.models import Event

        created = client.post("/api/system/backups", json={})
        assert created.status_code in (200, 201), created.text
        name = created.json()["name"]
        assert set(created.json()) == {"name", "size_bytes"}

        r = client.post("/api/system/backups/restore", json={"name": name})

        assert r.status_code == 200
        note = r.json()["privacy_note"]
        # Separate from `message` so the UI can render it as a warning rather than a receipt.
        assert "see" in note.lower() and "row" in note.lower()
        # These three endpoints declared no response content at all before, so the key sets are
        # spelled out: a response model that dropped `privacy_note` would turn the one warning the
        # owner gets about a visibility change back into a silent restore.
        assert set(r.json()) == {"restored", "message", "privacy_note"}
        with client.app.state.sessions() as session:
            audit = session.query(Event).filter_by(scope="backup.restore").one()
        assert audit.message["backup"] == name
        assert audit.level == "warning"

    def test_the_backup_list_names_each_file_with_its_size_and_age(self, client: TestClient):
        client.post("/api/system/backups", json={})

        listed = client.get("/api/system/backups").json()

        assert [set(b) for b in listed] == [{"name", "size_bytes", "created_at"}]
        assert listed[0]["name"].endswith(".db") and listed[0]["size_bytes"] > 0

    def test_an_empty_backup_directory_is_an_empty_list(self, client: TestClient):
        assert client.get("/api/system/backups").json() == []

    def test_a_missing_backup_is_a_404_and_audits_nothing(self, client: TestClient):
        from shortlist.server.db.models import Event

        assert client.post("/api/system/backups/restore", json={"name": "nope.db"}).status_code == 404

        with client.app.state.sessions() as session:
            assert session.query(Event).filter_by(scope="backup.restore").count() == 0


class TestEveryJobKindIsExplained:
    """A registered handler with no catalogue entry shows up as its raw kind — `backup.take` on
    somebody's screen — and cannot be described or triggered from the UI at all.

    The catalogue lives in `jobs.py` beside the handlers precisely so this is checkable in one place;
    it used to be a map in the SPA, which drifted the moment three kinds were added.
    """

    def test_the_catalogue_names_every_registered_kind(self):
        from shortlist.server.services import jobs

        missing = sorted(set(jobs._HANDLERS) - set(jobs.BY_KIND))
        assert not missing, f"job kind(s) with no catalogue entry: {missing}"

    def test_every_catalogue_entry_has_a_handler(self):
        """The other direction: a catalogue entry with no handler is a button that always fails."""
        from shortlist.server.services import jobs

        orphans = sorted(set(jobs.BY_KIND) - set(jobs._HANDLERS))
        assert not orphans, f"catalogue entr(ies) with no handler: {orphans}"

    def test_the_destructive_kinds_are_not_manually_triggerable(self):
        """`user.cleanup` DELETES someone's rows and `user.restore` makes rows visible. Neither may be
        reachable from a generic 'run a job' button — hence `manual` as an allow-list."""
        from shortlist.server.services import jobs

        for kind in ("user.cleanup", "user.hide", "user.restore", "row.reconcile"):
            assert kind in jobs.BY_KIND, kind
            assert not jobs.BY_KIND[kind].manual, f"{kind} must not be manually triggerable"
            assert kind not in jobs.KINDS


class TestScheduleApi:
    """`GET /api/schedule` — everything on a timer, in one place.

    Each cron was already editable, but a job's lived inside that job's expanded settings and a
    row's inside the row editor, so "what happens overnight, and in what order?" could only be
    answered by opening a dozen panels.
    """

    def test_it_lists_every_scheduled_job_with_the_setting_that_edits_it(self, client: TestClient):
        body = client.get("/api/schedule").json()
        kinds = {entry["kind"]: entry for entry in body["jobs"]}

        assert {"sync.users", "sync.history", "backup.take", "privacy.sync", "sync.check"} <= set(kinds)
        assert set(body) == {"jobs", "rows"}
        for entry in body["jobs"]:
            # Without the settings key the UI would need a hard-coded kind -> key map to save a change.
            assert entry["setting"], entry["kind"]
            # Spelled out: the endpoint declares a response model now, and `writes_plex` — the flag
            # that tells the owner which of these touches other people's servers unattended — is
            # exactly the kind of field a model that forgot it would drop without a word.
            assert set(entry) == {
                "type",
                "kind",
                "label",
                "description",
                "setting",
                "cron",
                "using_default",
                "default_cron",
                "optional",
                "writes_plex",
                "next_run",
            }, entry["kind"]

    def test_every_job_reports_the_built_in_cron_it_falls_back_to(self, client: TestClient):
        """The SPA has no copy of `DEFAULT_CRONS` and must not grow one, so the only way it can offer
        "put this back on its built-in schedule" is for the server to say what that schedule is."""
        from shortlist.server.scheduler import DEFAULT_CRONS

        jobs = {entry["kind"]: entry for entry in client.get("/api/schedule").json()["jobs"]}
        assert {entry["setting"]: entry["default_cron"] for entry in jobs.values()} == DEFAULT_CRONS
        # Not merely non-empty: the value has to be the built-in itself, so a chip labelled with it
        # names the time the job would actually run at.
        assert jobs["sync.check"]["default_cron"] == "45 5 * * *"

    def test_the_privacy_sync_is_scheduled_by_default(self, client: TestClient):
        """The automatic Privacy Check was removed on 2026-07-16, so nothing verifies hiding after
        the fact. A nightly re-merge is the cheapest remaining safety net, and it can only ever make
        the server more private."""
        privacy = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "privacy.sync")
        assert privacy["cron"], "privacy sync must ship with a schedule"
        assert privacy["writes_plex"] is True

    def test_a_blank_cron_that_means_a_built_in_default_is_not_reported_as_off(self, client: TestClient):
        """`backup.cron` ships blank, meaning "use 03:00". Returning the RAW setting filed a backup
        that runs nightly under "Not scheduled" — a schedule the UI claimed did not exist."""
        backup = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "backup.take")

        assert backup["cron"], "an inherited default must be reported as the schedule it is"
        assert backup["using_default"] is True, "and flagged as inherited, not as a choice the owner made"

    def test_an_owner_set_cron_is_not_flagged_as_a_default(self, client: TestClient):
        client.put("/api/settings", json={"values": {"backup.cron": "0 4 * * *"}})

        backup = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "backup.take")
        assert backup["cron"] == "0 4 * * *"
        assert backup["using_default"] is False

    def test_the_drift_check_is_scheduled_by_default_but_can_be_switched_off(self, client: TestClient):
        """Drift is the failure nobody notices, so the thing that repairs it ships switched ON.

        It stays the one schedule that can be turned OFF entirely — it writes corrections to Plex, so
        an owner who wants to review drift before it is fixed must be able to say so, and clearing the
        box has to mean off rather than "inherit the default".
        """
        from shortlist.server.settings_store import SettingsStore

        check = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "sync.check")
        assert check["cron"] == "45 5 * * *", "runs after the rows build and the privacy pass"
        assert check["optional"] is True

        with client.app.state.sessions() as session:
            SettingsStore(session).set("sync.check_cron", "")

        cleared = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "sync.check")
        assert cleared["cron"] == "", "cleared must mean off, not fall back to the default"

    def test_switching_the_drift_check_off_removes_the_live_trigger(self, client: TestClient):
        """Saving the setting is not enough — the running scheduler has to lose the job.

        `PUT /api/settings` rebuilt the schedule for a hardcoded four-key set that covered the
        watch/user/backup crons only. So the one schedule the UI offers to switch OFF saved its blank
        and went on firing at 05:45 until the container restarted: an owner who turned off a job that
        writes corrections to Plex got no such thing that night.
        """
        from shortlist.server.scheduler import SYNC_CHECK_JOB_ID

        assert client.app.state.scheduler.get_job(SYNC_CHECK_JOB_ID), "ships on"

        assert client.put("/api/settings", json={"values": {"sync.check_cron": ""}}).status_code == 200

        assert client.app.state.scheduler.get_job(SYNC_CHECK_JOB_ID) is None, "off means off tonight, not next boot"
        off = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "sync.check")
        assert off["cron"] == "" and off["next_run"] is None

    def test_restoring_the_built_in_default_puts_the_live_trigger_back(self, client: TestClient):
        """`null` means "use the built-in default" — the only way back once a job is switched off.

        Asserts the LIVE trigger, not that a next_run exists: the job is REMOVED while off, so it
        has to be registered again, and a job left on a stale registration still reports a next run.

        The other two assertions pin the state the restore has to land in, because several wrong
        implementations produce the right times:
        - writing "45 5 * * *" schedules the same minute but PINS a copy of the default, so a later
          change to the built-in time would never reach this install (`using_default` catches it);
        - writing `null` into the row instead of deleting it leaves `sync.check_cron` reading back
          as `None` where every other unset cron reads "" (the settings assertion catches it).
        """
        from shortlist.server.scheduler import SYNC_CHECK_JOB_ID

        assert client.put("/api/settings", json={"values": {"sync.check_cron": ""}}).status_code == 200
        assert client.app.state.scheduler.get_job(SYNC_CHECK_JOB_ID) is None, "off first"

        assert client.put("/api/settings", json={"values": {"sync.check_cron": None}}).status_code == 200

        trigger = str(client.app.state.scheduler.get_job(SYNC_CHECK_JOB_ID).trigger)
        assert "hour='5'" in trigger and "minute='45'" in trigger, f"not back on the built-in cron: {trigger}"
        check = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "sync.check")
        assert check["cron"] == "45 5 * * *"
        assert check["using_default"] is True, "restored means INHERITING the built-in, not pinning a copy of it"
        assert client.get("/api/settings").json()["sync.check_cron"] == "", "back to the shape of a never-set cron"

    def test_restoring_a_default_that_is_already_in_force_changes_nothing(self, client: TestClient):
        """A form that PUTs its whole object must not log a change it did not make (rule 10 is only
        useful if the change log is all real changes)."""
        from shortlist.server.db.models import Event

        with client.app.state.sessions() as session:
            before = session.query(Event).filter_by(scope="settings.change").count()

        assert client.put("/api/settings", json={"values": {"sync.check_cron": None}}).status_code == 200

        with client.app.state.sessions() as session:
            assert session.query(Event).filter_by(scope="settings.change").count() == before
        check = next(e for e in client.get("/api/schedule").json()["jobs"] if e["kind"] == "sync.check")
        assert check["cron"] == "45 5 * * *" and check["using_default"] is True

    def test_every_schedulable_cron_takes_effect_on_save(self, client: TestClient):
        """The trigger set is derived from DEFAULT_CRONS, so a cron added there can never be the next
        one whose edits silently wait for a restart.

        Asserts the LIVE trigger, not that a next_run exists: a job left on its boot-time cron still
        reports a next run, so `next_run is not None` passes whether or not the edit was applied.
        """
        from shortlist.server.scheduler import DEFAULT_CRONS
        from shortlist.server.services.jobs import CATALOG

        for key in DEFAULT_CRONS:
            assert client.put("/api/settings", json={"values": {key: "7 2 * * *"}}).status_code == 200

        job_ids = {e.schedule_setting: e.schedule_job_id for e in CATALOG if e.schedule_setting}
        assert set(job_ids) == set(DEFAULT_CRONS), "every schedulable cron must belong to a catalogue job"
        for key, job_id in job_ids.items():
            trigger = str(client.app.state.scheduler.get_job(job_id).trigger)
            assert "hour='2'" in trigger and "minute='7'" in trigger, f"{key} still on its boot-time trigger"

    def test_the_jobs_page_calls_a_cron_off_able_exactly_when_the_scheduler_does(self):
        """Two sets, maintained apart, decide the same thing — so pin them together.

        `schedule_optional` on the catalogue entry is what labels the picker's blank preset "Off"
        instead of "Daily"; `scheduler._OFF_ABLE` is what makes a stored blank actually mean off. If
        a job joins one and not the other, a chip labelled Off quietly means "run at the built-in
        default" on a job that writes to Plex — the exact lie the label exists to stop.
        """
        from shortlist.server.scheduler import _OFF_ABLE
        from shortlist.server.services.jobs import CATALOG

        assert {e.schedule_setting for e in CATALOG if e.schedule_optional} == _OFF_ABLE

    def test_backup_max_keep_reaches_the_live_job_without_a_restart(self, client: TestClient):
        """`backup.max_keep` is the one member of the rebuild set that is NOT a cron.

        It rides along in a union, so a tidy-up to `set(DEFAULT_CRONS)` would still pass every other
        test here while the nightly backup kept pruning to the old count until the container
        restarted — `_register_backup` bakes the value into the job payload at registration.
        """
        assert client.put("/api/settings", json={"values": {"backup.max_keep": 4}}).status_code == 200

        # `_register_backup` closes over the value rather than passing it as an argument, so the
        # closure cell is where a stale registration actually shows.
        job = client.app.state.scheduler.get_job("db-backup")
        captured = {cell.cell_contents for cell in (job.func.__closure__ or ()) if isinstance(cell.cell_contents, int)}
        assert 4 in captured, f"scheduled backup still on the boot-time max_keep: {captured}"

    def test_rows_sharing_a_cron_are_one_entry_not_three(self, client: TestClient):
        """Three rows on `30 3 * * *` are ONE trigger that builds all three — listing them as three
        separate 03:30 entries would misrepresent what the server actually does."""
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            session.query(Collection).update({Collection.schedule: "30 3 * * *"}, synchronize_session=False)
            session.add(Collection(slug="late", name="Late row", schedule="0 5 * * *", enabled=True))
            session.commit()

        rows = client.get("/api/schedule").json()["rows"]
        by_cron = {entry["cron"]: entry for entry in rows}
        assert set(by_cron) == {"30 3 * * *", "0 5 * * *"}
        assert len(by_cron["30 3 * * *"]["rows"]) >= 1
        assert by_cron["0 5 * * *"]["rows"][0]["slug"] == "late"
        assert set(by_cron["0 5 * * *"]) == {"type", "cron", "rows", "next_run"}
        assert set(by_cron["0 5 * * *"]["rows"][0]) == {"id", "slug", "name"}

    def test_a_disabled_row_is_not_on_the_schedule(self, client: TestClient):
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            session.query(Collection).update({Collection.enabled: False}, synchronize_session=False)
            session.commit()

        assert client.get("/api/schedule").json()["rows"] == []

    def test_it_is_owner_only(self, client: TestClient):
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/schedule").status_code == 401


class TestJobStatusIsAClosedSet:
    """`status` is `Literal["queued", "running", "done", "failed"]` on both job shapes.

    A response Literal is validated on the way out, so a fifth word in `jobs.status` would raise —
    500ing the Jobs page rather than rendering an unknown badge. This pushes each of the four
    through the real endpoints, and checks the set still matches everything `services/jobs.py`
    assigns, so a new state has to be added to the type at the same time.
    """

    def test_the_four_statuses_are_exactly_what_the_job_runner_assigns(self):
        """Read off the runner itself: every `job.status = "..."` in `services/jobs.py`."""
        import ast
        import inspect

        from shortlist.server.api.system import JobStatus
        from shortlist.server.services import jobs as jobs_module

        tree = ast.parse(inspect.getsource(jobs_module))
        assigned = {
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and any(isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets)
        }

        assert assigned == set(JobStatus.__args__), "services/jobs.py writes a status the Literal does not carry"

    def test_every_status_survives_the_jobs_list_and_the_catalogue(self, client: TestClient):
        from shortlist.server.api.system import JobStatus
        from shortlist.server.db.models import Job

        with client.app.state.sessions() as session:
            for status in JobStatus.__args__:
                session.add(Job(kind="sync.check", status=status, payload={}, result={}))
            session.commit()

        listed = client.get("/api/system/jobs").json()

        assert {j["status"] for j in listed} == set(JobStatus.__args__)
        # `/jobs/catalog` renders the newest row per kind through the SAME model, so it validates too.
        card = next(c for c in client.get("/api/system/jobs/catalog").json() if c["kind"] == "sync.check")
        assert card["last"]["status"] in set(JobStatus.__args__)
