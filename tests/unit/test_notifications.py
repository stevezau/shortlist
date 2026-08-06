"""shortlist/server/notifications.py: what fires, under which condition, and not otherwise."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shortlist.server import notifications as notif
from shortlist.server.db.models import Collection, Event, Job, Run, User
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.settings_store import SettingsStore


@pytest.fixture
def session(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    with make_session_factory(engine)() as db:
        yield db
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """`_update_available` hits GitHub through `check_for_update` — never let a test reach it."""
    monkeypatch.setattr(notif, "check_for_update", lambda _v: None)


class TestUpdateAvailable:
    def test_fires_when_a_newer_release_exists(self, session, monkeypatch):
        monkeypatch.setattr(notif, "check_for_update", lambda v: {"latest": "9.9.9", "url": "https://x/9.9.9"})

        result = notif._update_available(SettingsStore(session), "1.0.0")

        assert result == {
            "id": "update-9.9.9",
            "severity": "info",
            "title": "Update available",
            "body": "v1.0.0 → v9.9.9",
            "action_url": "https://x/9.9.9",
            "action_label": "View release",
            "dismissable": True,
        }

    def test_does_not_fire_when_already_up_to_date(self, session):
        assert notif._update_available(SettingsStore(session), "1.0.0") is None


class TestRunsPaused:
    def test_fires_when_paused_all_is_set(self, session):
        SettingsStore(session).set("paused_all", True)

        result = notif._runs_paused(SettingsStore(session))

        assert result["id"] == "runs-paused"
        assert result["severity"] == "warning"
        assert result["dismissable"] is False

    def test_does_not_fire_when_runs_are_not_paused(self, session):
        assert notif._runs_paused(SettingsStore(session)) is None


class TestLastRunProblem:
    def test_a_failed_run_is_reported_as_an_error(self, session):
        run = Run(trigger="manual", status="error")
        session.add(run)
        session.commit()

        result = notif._last_run_problem(session)

        assert result["id"] == f"run-failed-{run.id}"
        assert result["severity"] == "error"
        assert result["action_url"] == f"/runs/{run.id}"

    def test_a_clean_run_reports_nothing(self, session):
        session.add(Run(trigger="manual", status="ok", stats={"users_ok": 3, "users_error": 0}))
        session.commit()

        assert notif._last_run_problem(session) is None

    def test_a_partial_run_is_a_warning_pluralized_by_how_many_failed(self, session):
        session.add(Run(trigger="manual", status="ok", stats={"users_ok": 1, "users_error": 1}))
        session.commit()
        singular = notif._last_run_problem(session)
        assert singular["severity"] == "warning"
        assert "1 person failed" in singular["title"]

        session.add(Run(trigger="manual", status="ok", stats={"users_ok": 1, "users_error": 2}))
        session.commit()
        plural = notif._last_run_problem(session)
        assert "2 people failed" in plural["title"]

    def test_no_completed_runs_at_all_reports_nothing(self, session):
        assert notif._last_run_problem(session) is None

    def test_a_run_still_in_progress_is_ignored_in_favour_of_the_last_completed_one(self, session):
        session.add(Run(trigger="manual", status="ok", stats={"users_ok": 1, "users_error": 0}))
        session.commit()
        session.add(Run(trigger="manual", status="running"))
        session.commit()

        assert notif._last_run_problem(session) is None


class TestRecentServiceErrors:
    def test_fires_on_a_recent_non_run_error(self, session):
        session.add(Event(scope="requests.send", level="error", ts=datetime.now(UTC)))
        session.commit()

        result = notif._recent_service_errors(session)

        assert result["id"] == "recent-errors"
        assert "1 error" in result["title"]

    def test_excludes_run_scoped_errors_already_covered_by_last_run_problem(self, session):
        session.add(Event(scope="run.user", level="error", ts=datetime.now(UTC)))
        session.commit()

        assert notif._recent_service_errors(session) is None

    def test_excludes_events_below_error_level(self, session):
        session.add(Event(scope="requests.send", level="warning", ts=datetime.now(UTC)))
        session.commit()

        assert notif._recent_service_errors(session) is None

    def test_excludes_errors_older_than_a_day(self, session):
        session.add(Event(scope="requests.send", level="error", ts=datetime.now(UTC) - timedelta(days=2)))
        session.commit()

        assert notif._recent_service_errors(session) is None

    def test_pluralizes_the_count(self, session):
        session.add_all(
            [
                Event(scope="requests.send", level="error", ts=datetime.now(UTC)),
                Event(scope="settings.save", level="error", ts=datetime.now(UTC)),
            ]
        )
        session.commit()

        assert "2 errors" in notif._recent_service_errors(session)["title"]


class TestMdblistQuota:
    def test_fires_on_a_recent_rate_limited_event(self, session):
        event = Event(scope="requests.rate_limited", level="warning", ts=datetime.now(UTC))
        session.add(event)
        session.commit()

        result = notif._mdblist_quota(session)

        assert result["id"] == f"mdblist-quota-{event.ts.date().isoformat()}"
        assert result["severity"] == "warning"

    def test_does_not_fire_with_no_recent_rate_limit_event(self, session):
        assert notif._mdblist_quota(session) is None

    def test_does_not_fire_for_an_event_older_than_a_day(self, session):
        session.add(Event(scope="requests.rate_limited", level="warning", ts=datetime.now(UTC) - timedelta(days=2)))
        session.commit()

        assert notif._mdblist_quota(session) is None


class TestFailedJobs:
    def test_fires_when_a_job_exhausts_its_retries(self, session):
        job = Job(kind="user.cleanup", status="failed")
        session.add(job)
        session.commit()

        result = notif._failed_jobs(session)

        assert result["id"] == f"failed-jobs-{job.id}"
        assert result["severity"] == "error"
        assert "user.cleanup" in result["body"]

    def test_does_not_fire_when_no_job_has_failed(self, session):
        session.add(Job(kind="user.cleanup", status="done"))
        session.commit()

        assert notif._failed_jobs(session) is None

    def test_the_id_tracks_the_newest_failure_so_a_new_one_resurfaces_after_dismissal(self, session):
        session.add(Job(kind="user.cleanup", status="failed"))
        session.commit()
        second = Job(kind="row.reconcile", status="failed")
        session.add(second)
        session.commit()

        result = notif._failed_jobs(session)

        assert result["id"] == f"failed-jobs-{second.id}"
        assert result["title"] == "2 background jobs failed"


class TestOwnerSeesAllRows:
    """The condition is a four-way AND, and every leg has to be able to switch it off on its own —
    otherwise the owner gets nagged about a shelf they haven't got. The `build == "shared"` leg is
    the subtle one: a shared row is ONE collection everybody sees on purpose, not a per-person row
    stacked N times, so it must never fire this."""

    @staticmethod
    def _setup(session, *, owner_enabled=True, others=2, build="per_person", placement_friends="both", row=True):
        # The initial migration seeds a default "Picked for You" row, which is per_person and on both
        # surfaces — i.e. it satisfies the condition on its own. Clear it so each case controls the
        # one variable it is about.
        session.query(Collection).delete()
        session.add(User(plex_account_id=1, username="owner", slug="owner", user_type="owner", enabled=owner_enabled))
        for i in range(others):
            session.add(User(plex_account_id=10 + i, username=f"u{i}", slug=f"u{i}", user_type="shared", enabled=True))
        if row:
            session.add(
                Collection(
                    slug="picked", name="Picked for You", build=build, enabled=True, placement_friends=placement_friends
                )
            )
        session.commit()

    def test_fires_when_per_person_rows_are_on_the_friends_recommended_shelf(self, session):
        self._setup(session)

        result = notif._owner_sees_all_rows(session)

        assert result is not None
        assert result["id"] == "owner-sees-all-rows"
        assert result["action_url"] == "/watching-account"
        assert result["dismissable"] is True
        # No number in the copy: the true count is rows x resolved audience, which neither
        # `others` nor `others + 1` gets right once a row is audience="subset" or muted per-user.
        assert "everyone's row" in result["body"]

    def test_fires_even_when_the_owner_has_no_row_of_their_own(self, session):
        """Turning your OWN row off does not stop you seeing everyone else's — you own the server, so
        nothing hides them. Gating on `enabled` silenced this for the person most likely to be
        surprised by it."""
        self._setup(session, owner_enabled=False)

        assert notif._owner_sees_all_rows(session) is not None

    def test_silent_when_the_owner_is_the_only_person(self, session):
        """One row on your own shelf is your row. There is nothing to warn about."""
        self._setup(session, others=0)

        assert notif._owner_sees_all_rows(session) is None

    def test_silent_when_rows_are_home_only(self, session):
        """Home screens are already split by audience — this is only ever about the library shelf."""
        self._setup(session, placement_friends="home")

        assert notif._owner_sees_all_rows(session) is None

    def test_silent_for_a_shared_row(self, session):
        """One collection everyone sees deliberately — not N per-person rows stacked on the shelf."""
        self._setup(session, build="shared")

        assert notif._owner_sees_all_rows(session) is None

    def test_silent_when_no_row_exists_at_all(self, session):
        self._setup(session, row=False)

        assert notif._owner_sees_all_rows(session) is None


class TestSeverityVocabulary:
    def test_every_firing_notification_uses_one_of_the_three_severities(self, session, monkeypatch):
        monkeypatch.setattr(notif, "check_for_update", lambda v: {"latest": "9.9.9", "url": "https://x"})
        store = SettingsStore(session)
        store.set("paused_all", True)
        session.add(Run(trigger="manual", status="ok", stats={"users_ok": 1, "users_error": 1}))
        session.add(Event(scope="requests.rate_limited", level="warning", ts=datetime.now(UTC)))
        session.add(Event(scope="settings.save", level="error", ts=datetime.now(UTC)))
        session.add(Job(kind="user.cleanup", status="failed"))
        session.commit()

        items = notif.build_notifications(session, store, "1.0.0")

        assert items  # sanity: every builder above should have fired
        assert {n["severity"] for n in items} <= {"info", "warning", "error"}


class TestBuildNotifications:
    def test_nothing_fires_on_a_clean_install(self, session):
        assert notif.build_notifications(session, SettingsStore(session), "1.0.0") == []

    def test_orders_error_before_warning_before_info(self, session, monkeypatch):
        monkeypatch.setattr(notif, "check_for_update", lambda v: {"latest": "9.9.9", "url": "https://x"})
        store = SettingsStore(session)
        store.set("paused_all", True)
        session.add(Run(trigger="manual", status="error"))
        session.commit()

        items = notif.build_notifications(session, store, "1.0.0")

        order = {"error": 0, "warning": 1, "info": 2}
        severities = [n["severity"] for n in items]
        assert severities == sorted(severities, key=order.get)
        assert severities[0] == "error"
        assert severities[-1] == "info"

    def test_dismissing_the_update_note_hides_it_but_a_newer_release_resurfaces(self, session, monkeypatch):
        store = SettingsStore(session)
        monkeypatch.setattr(notif, "check_for_update", lambda v: {"latest": "9.9.9", "url": "https://x"})
        store.set(notif.DISMISSED_KEY, ["update-9.9.9"])

        items = notif.build_notifications(session, store, "1.0.0")
        assert not any(n["id"] == "update-9.9.9" for n in items)

        monkeypatch.setattr(notif, "check_for_update", lambda v: {"latest": "9.9.10", "url": "https://x"})
        items = notif.build_notifications(session, store, "1.0.0")
        assert any(n["id"] == "update-9.9.10" for n in items)  # new version, new id -> not dismissed

    def test_a_non_dismissable_notification_survives_being_in_the_dismissed_list(self, session):
        """ "All runs are paused" is `dismissable: False`, and silencing it for good would leave the
        owner believing rows build nightly on a server that has stopped. The flag is enforced on
        READ, so an id that reached the dismissed list by any route still surfaces."""
        store = SettingsStore(session)
        store.set("paused_all", True)
        assert any(n["id"] == "runs-paused" for n in notif.build_notifications(session, store, "1.0.0"))

        store.set(notif.DISMISSED_KEY, ["runs-paused"])

        assert any(n["id"] == "runs-paused" for n in notif.build_notifications(session, store, "1.0.0"))

    def test_a_dismissable_notification_is_still_hidden_by_its_id(self, session, monkeypatch):
        """The enforcement above must not break ordinary dismissal — the update note stays hidden."""
        monkeypatch.setattr(notif, "check_for_update", lambda v: {"latest": "9.9.9", "url": "https://x/9.9.9"})
        store = SettingsStore(session)
        assert any(n["id"] == "update-9.9.9" for n in notif.build_notifications(session, store, "1.0.0"))

        store.set(notif.DISMISSED_KEY, ["update-9.9.9"])

        assert not any(n["id"] == "update-9.9.9" for n in notif.build_notifications(session, store, "1.0.0"))
