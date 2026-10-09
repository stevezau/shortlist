"""Which events reach the owner's webhook, and the rules that keep each one from becoming noise or a leak.

The owner chooses the events (`notify.webhook.events`). The defaults keep what v1 did plus the privacy
alert, so an install that switched the webhook on before this gets nothing new it did not ask for. The
rules under each event are what make "every event" usable: dry runs are previews, routine jobs start and
finish every few minutes, the sender must never report on itself, and no message names a person.

No test touches the network: `respx` intercepts every httpx call.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from shortlist.server.db.models import Job, RequestCandidate, Run
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services import jobs, notify
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import PRIVATE_KEYS, SECRET_KEYS, SettingsStore
from tests.db_helpers import disposing_engine

pytestmark = pytest.mark.integration

WEBHOOK = "https://hooks.example.com/shortlist/AbCdEf0123456789"
ALL_EVENTS = list(notify.EVENTS)


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    with disposing_engine(make_engine(tmp_path)) as engine:
        yield make_session_factory(engine)


@pytest.fixture
def secrets(tmp_path: Path) -> SecretBox:
    return SecretBox(tmp_path)


@pytest.fixture
def state(sessions, secrets):
    return SimpleNamespace(sessions=sessions, secrets=secrets, run_service=None)


def configure(sessions, secrets, *, events: list[str] | None = None, enabled: bool = True, **extra) -> None:
    with sessions() as session:
        store = SettingsStore(session, secrets)
        store.set("notify.webhook.enabled", enabled)
        store.set("notify.webhook.url", WEBHOOK)
        if events is not None:
            store.set("notify.webhook.events", events)
        for key, value in extra.items():
            store.set(key, value)


def queued(sessions) -> list[dict]:
    """The items waiting to be sent, oldest first."""
    with sessions() as session:
        rows = session.query(Job).filter(Job.kind == "notify.send").order_by(Job.id).all()
        return [row.payload["item"] for row in rows]


def events_queued(sessions) -> list[str]:
    return [item["event"] for item in queued(sessions)]


def make_run(sessions, *, status: str = "ok", dry_run: bool = False, stats: dict | None = None) -> int:
    with sessions() as session:
        run = Run(
            trigger="schedule",
            status=status,
            dry_run=dry_run,
            began_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            stats=stats if stats is not None else {},
        )
        session.add(run)
        session.commit()
        return run.id


def make_job(sessions, kind: str, *, payload: dict | None = None, attempts: int = 1, max_attempts: int = 3) -> int:
    with sessions() as session:
        job = Job(kind=kind, payload=payload or {}, status="running", attempts=attempts, max_attempts=max_attempts)
        session.add(job)
        session.commit()
        return job.id


def assert_names_nobody(item: dict) -> None:
    text = f"{item['title']} {item['body']}".lower()
    assert "sarah" not in text and "mike" not in text, item


class TestChoosingEvents:
    def test_the_defaults_are_the_run_failure_and_the_privacy_alert(self, sessions, secrets):
        with sessions() as session:
            assert SettingsStore(session, secrets).get("notify.webhook.events") == ["run.failed", "privacy.exposure"]

    def test_an_event_the_owner_did_not_choose_queues_nothing(self, sessions, secrets):
        configure(sessions, secrets)  # defaults: run.failed + privacy.exposure
        notify.enqueue_run_outcome(sessions, make_run(sessions, status="ok"))
        assert queued(sessions) == []

    def test_a_chosen_event_is_queued_and_says_which_event_it_is(self, sessions, secrets):
        configure(sessions, secrets, events=["run.finished"])
        notify.enqueue_run_outcome(sessions, make_run(sessions, status="ok", stats={"users_ok": 3}))
        assert events_queued(sessions) == ["run.finished"]

    def test_nothing_is_queued_while_the_webhook_is_off_whatever_is_chosen(self, sessions, secrets):
        configure(sessions, secrets, events=ALL_EVENTS, enabled=False)
        notify.enqueue_run_outcome(sessions, make_run(sessions, status="error"))
        notify.enqueue_job_event(sessions, make_job(sessions, "backup.take"), "job.started")
        assert queued(sessions) == []

    def test_every_event_is_in_the_body_that_leaves_the_server(self):
        item = {"id": "x", "severity": "info", "title": "t", "body": "b", "action_url": "/", "event": "run.started"}
        assert notify.webhook_body(item)["event"] == "run.started"
        assert notify.webhook_body(notify.sample_item())["event"] == "test"


class TestRunEvents:
    @pytest.mark.parametrize(
        ("status", "stats", "event"),
        [
            ("ok", {"users_ok": 4, "users_error": 0}, "run.finished"),
            ("ok", {"users_ok": 3, "users_error": 1}, "run.partial"),
            ("error", {}, "run.failed"),
            ("aborted", {}, "run.stopped"),
        ],
    )
    def test_each_outcome_is_its_own_event(self, sessions, secrets, status, stats, event):
        configure(sessions, secrets, events=ALL_EVENTS)
        notify.enqueue_run_outcome(sessions, make_run(sessions, status=status, stats=stats))
        assert events_queued(sessions) == [event]
        assert_names_nobody(queued(sessions)[0])

    def test_a_run_stopped_before_it_began_says_nothing(self, sessions, secrets):
        """Cancelled while queued, or queued when the server restarted: it never started."""
        configure(sessions, secrets, events=ALL_EVENTS)
        run_id = make_run(sessions, status="aborted")
        with sessions() as session:
            session.get(Run, run_id).began_at = None
            session.commit()
        notify.enqueue_run_outcome(sessions, run_id)
        assert queued(sessions) == []

    def test_a_run_that_started_is_announced(self, sessions, secrets):
        configure(sessions, secrets, events=["run.started"])
        run_id = make_run(sessions, status="running")
        notify.enqueue_run_started(sessions, run_id)
        [item] = queued(sessions)
        assert item["event"] == "run.started" and item["action_url"] == f"/runs/{run_id}"

    def test_a_dry_run_announces_nothing_at_all(self, sessions, secrets):
        """A dry run is a preview somebody is sitting and watching."""
        configure(sessions, secrets, events=ALL_EVENTS)
        notify.enqueue_run_started(sessions, make_run(sessions, status="running", dry_run=True))
        for status in ("ok", "error", "aborted"):
            notify.enqueue_run_outcome(sessions, make_run(sessions, status=status, dry_run=True))
        assert queued(sessions) == []

    def test_the_failure_keeps_the_bells_wording(self, sessions, secrets):
        from shortlist.server import notifications

        configure(sessions, secrets)
        run_id = make_run(sessions, status="error")
        notify.enqueue_run_outcome(sessions, run_id)
        with sessions() as session:
            expected = notifications.run_failed_alert(session.get(Run, run_id))
        assert queued(sessions) == [expected | {"event": "run.failed"}]

    def test_it_never_raises_into_the_run(self, sessions, secrets, monkeypatch):
        configure(sessions, secrets, events=ALL_EVENTS)
        monkeypatch.setattr(jobs, "enqueue", _raise)
        assert notify.enqueue_run_outcome(sessions, make_run(sessions, status="error")) is None
        assert notify.enqueue_run_started(sessions, make_run(sessions, status="running")) is None


class TestJobEvents:
    def test_a_job_that_starts_and_finishes_is_announced_by_its_label(self, sessions, secrets):
        configure(sessions, secrets, events=["job.started", "job.finished"])
        job_id = make_job(sessions, "backup.take")
        notify.enqueue_job_event(sessions, job_id, "job.started")
        notify.enqueue_job_event(sessions, job_id, "job.finished")
        items = queued(sessions)
        assert [i["event"] for i in items] == ["job.started", "job.finished"]
        assert all(jobs.BY_KIND["backup.take"].label in i["title"] for i in items)

    def test_a_skipped_scheduled_job_is_announced_by_its_label(self, sessions, secrets):
        configure(sessions, secrets, events=["job.skipped"])
        notify.enqueue_job_skipped(sessions, "watch.sync", "Watch history sync", "2026-10-02T03:30:00+00:00")
        [item] = queued(sessions)
        assert item["event"] == "job.skipped"
        assert "Watch history sync" in item["title"]
        assert "skipped" in item["title"].lower()

    def test_a_skipped_scheduled_job_queues_nothing_unless_chosen(self, sessions, secrets):
        configure(sessions, secrets)  # defaults
        assert notify.enqueue_job_skipped(sessions, "watch.sync", "Watch history sync", "2026-10-02") is None
        assert queued(sessions) == []

    def test_routine_jobs_do_not_announce_starting_or_finishing_but_do_announce_failing(self, sessions, secrets):
        """The privacy sync runs every 30 minutes and a playback credit runs per play."""
        configure(sessions, secrets, events=ALL_EVENTS)
        routine = jobs.routine_kinds()
        assert routine, "the catalogue marks some kinds routine"
        job_id = make_job(sessions, routine[0])
        notify.enqueue_job_event(sessions, job_id, "job.started")
        notify.enqueue_job_event(sessions, job_id, "job.finished")
        notify.enqueue_job_event(sessions, job_id, "job.failed")
        assert events_queued(sessions) == ["job.failed"]

    @pytest.mark.parametrize(
        ("payload", "result", "event", "sent"),
        [
            ({"scheduled": True}, None, "job.started", False),  # every 30 minutes: never news that it began
            ({}, None, "job.started", True),  # the owner pressed the button
            ({"scheduled": True}, {"quiet": True}, "job.finished", False),  # nothing changed
            ({"scheduled": True}, {"quiet": False}, "job.finished", True),  # it wrote or found something
            ({"scheduled": True}, None, "job.failed", True),
        ],
    )
    def test_the_privacy_sync_speaks_only_when_it_has_news(self, sessions, secrets, payload, result, event, sent):
        """It runs every 30 minutes and is not a `routine` kind, so it needs the rule the Jobs page's
        Recent list already applies: a scheduled pass that changed nothing is quiet."""
        configure(sessions, secrets, events=ALL_EVENTS)
        job_id = make_job(sessions, "privacy.sync", payload=payload)
        if result is not None:
            with sessions() as session:
                session.get(Job, job_id).result = result
                session.commit()
        notify.enqueue_job_event(sessions, job_id, event)
        assert events_queued(sessions) == ([event] if sent else [])

    def test_a_retry_is_not_announced_as_starting_again(self, sessions, secrets):
        configure(sessions, secrets, events=["job.started"])
        notify.enqueue_job_event(sessions, make_job(sessions, "backup.take", attempts=2), "job.started")
        assert queued(sessions) == []

    def test_the_sender_never_reports_on_itself(self, sessions, secrets):
        """A webhook that is down would otherwise queue an alert about failing to send an alert, for ever."""
        configure(sessions, secrets, events=ALL_EVENTS)
        job_id = make_job(sessions, "notify.send", payload={"item": {}})
        for event in ("job.started", "job.finished", "job.failed"):
            notify.enqueue_job_event(sessions, job_id, event)
        assert queued(sessions) == [{}], "only the send job the test made; nothing queued about it"

    def test_a_dry_run_job_announces_nothing(self, sessions, secrets):
        configure(sessions, secrets, events=ALL_EVENTS)
        job_id = make_job(sessions, "sync.check", payload={"dry_run": True})
        notify.enqueue_job_event(sessions, job_id, "job.started")
        notify.enqueue_job_event(sessions, job_id, "job.failed")
        assert queued(sessions) == []

    def test_the_message_carries_neither_the_jobs_detail_nor_its_error(self, sessions, secrets):
        """Both can name a person ("Put 2 row(s) back for sarah")."""
        configure(sessions, secrets, events=ALL_EVENTS)
        job_id = make_job(sessions, "user.restore", payload={"slug": "sarah"})
        with sessions() as session:
            job = session.get(Job, job_id)
            job.detail, job.error = "Put 2 row(s) back for sarah", "RuntimeError: sarah's filter"
            session.commit()
        notify.enqueue_job_event(sessions, job_id, "job.finished")
        notify.enqueue_job_event(sessions, job_id, "job.failed")
        for item in queued(sessions):
            assert_names_nobody(item)

    def test_a_failure_with_retries_left_is_not_announced_but_the_last_one_is(self, sessions, secrets):
        configure(sessions, secrets, events=["job.failed"])
        retrying = make_job(sessions, "backup.take", attempts=1, max_attempts=3)
        jobs._finish(sessions, retrying, error="boom")
        assert queued(sessions) == []
        last = make_job(sessions, "backup.take", attempts=3, max_attempts=3)
        jobs._finish(sessions, last, error="boom")
        assert events_queued(sessions) == ["job.failed"]

    def test_the_queue_announces_a_job_it_runs(self, sessions, secrets, state, monkeypatch):
        configure(sessions, secrets, events=["job.started", "job.finished"])
        monkeypatch.setitem(jobs._HANDLERS, "backup.take", lambda _state, _payload: {"detail": "ok"})
        jobs.enqueue(sessions, "backup.take", {})
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            asyncio.run(jobs.run_pending(state))
            # A drain runs what was queued when it started; the alerts it queued go out on the
            # worker's next tick (`scheduler.py`, every 60s).
            assert not route.called
            asyncio.run(jobs.run_pending(state))
        sent = [json.loads(call.request.content)["event"] for call in route.calls]
        assert sorted(sent) == ["job.finished", "job.started"]


class TestPrivacyExposure:
    def test_an_exposure_is_sent_as_a_count_with_no_names(self, sessions, secrets):
        configure(sessions, secrets)
        run_id = make_run(sessions, stats={"unhideable_rows": {"sarah": [1, 2]}, "filters_not_enforced": {"mike": [3]}})
        notify.after_run(sessions, run_id, current_version="1.9.0")
        [item] = queued(sessions)
        assert item["event"] == "privacy.exposure"
        assert "2 accounts" in item["title"]
        assert_names_nobody(item)

    def test_an_unreadable_filter_counts_as_an_exposure(self, sessions, secrets):
        configure(sessions, secrets)
        notify.after_run(sessions, make_run(sessions, stats={"unreadable_filters": {"sarah": "label has &"}}), "1.9.0")
        assert events_queued(sessions) == ["privacy.exposure"]

    def test_nothing_is_sent_when_the_newest_measurement_is_clean(self, sessions, secrets):
        configure(sessions, secrets)
        make_run(sessions, stats={"unhideable_rows": {"sarah": [1]}})
        notify.after_run(sessions, make_run(sessions, stats={"unhideable_rows": {}}), "1.9.0")
        assert queued(sessions) == []

    def test_a_standing_exposure_is_repeated_at_most_once_a_day(self, sessions, secrets):
        configure(sessions, secrets)
        exposed = {"unhideable_rows": {"sarah": [1]}}
        notify.after_run(sessions, make_run(sessions, stats=exposed), "1.9.0")
        notify.after_run(sessions, make_run(sessions, stats=exposed), "1.9.0")
        assert len(queued(sessions)) == 1
        with sessions() as session:
            SettingsStore(session, secrets).set(
                "notify.webhook.privacy_sent_at", (datetime.now(UTC) - timedelta(hours=25)).isoformat()
            )
        notify.after_run(sessions, make_run(sessions, stats=exposed), "1.9.0")
        assert len(queued(sessions)) == 2

    def test_a_new_exposure_after_a_clean_run_is_sent_at_once(self, sessions, secrets):
        configure(sessions, secrets)
        notify.after_run(sessions, make_run(sessions, stats={"unhideable_rows": {"sarah": [1]}}), "1.9.0")
        notify.after_run(sessions, make_run(sessions, stats={"unhideable_rows": {}}), "1.9.0")
        notify.after_run(sessions, make_run(sessions, stats={"unhideable_rows": {"mike": [2]}}), "1.9.0")
        assert events_queued(sessions) == ["privacy.exposure", "privacy.exposure"]


class TestRequestsWaiting:
    def _pending(self, sessions, n: int) -> None:
        with sessions() as session:
            session.query(RequestCandidate).delete()
            for i in range(n):
                session.add(RequestCandidate(tmdb_id=i + 1, media_type="movie", title=f"T{i}", status="pending"))
            session.commit()

    def test_it_is_sent_only_when_more_titles_wait_than_last_time(self, sessions, secrets):
        configure(sessions, secrets, events=["requests.waiting"])
        self._pending(sessions, 3)
        notify.after_run(sessions, make_run(sessions), "1.9.0")
        notify.after_run(sessions, make_run(sessions), "1.9.0")  # still 3: nothing new
        self._pending(sessions, 1)
        notify.after_run(sessions, make_run(sessions), "1.9.0")  # fewer: nothing, and the count follows
        self._pending(sessions, 2)
        notify.after_run(sessions, make_run(sessions), "1.9.0")
        items = queued(sessions)
        assert [i["event"] for i in items] == ["requests.waiting", "requests.waiting"]
        assert "3 titles" in items[0]["title"] and "2 titles" in items[1]["title"]
        assert items[1]["action_url"] == "/requests"


class TestUpdateAvailable:
    def test_each_newer_version_is_announced_once(self, sessions, secrets, monkeypatch):
        configure(sessions, secrets, events=["update.available"])
        latest = {"latest": "2.0.0", "url": "https://github.com/stevezau/shortlist/releases/tag/v2.0.0"}
        monkeypatch.setattr(notify, "check_for_update", lambda _current: latest)
        notify.after_run(sessions, make_run(sessions), "1.9.0")
        notify.after_run(sessions, make_run(sessions), "1.9.0")
        latest = {"latest": "2.1.0", "url": "https://github.com/stevezau/shortlist/releases/tag/v2.1.0"}
        notify.after_run(sessions, make_run(sessions), "1.9.0")
        bodies = [i["body"] for i in queued(sessions)]  # the bell's wording: "v1.9.0 → v2.0.0"
        assert len(bodies) == 2 and "2.0.0" in bodies[0] and "2.1.0" in bodies[1]

    def test_nothing_is_sent_when_the_server_is_up_to_date(self, sessions, secrets, monkeypatch):
        configure(sessions, secrets, events=["update.available"])
        monkeypatch.setattr(notify, "check_for_update", lambda _current: None)
        notify.after_run(sessions, make_run(sessions), "1.9.0")
        assert queued(sessions) == []


class TestAfterRun:
    def test_a_dry_run_checks_nothing(self, sessions, secrets, monkeypatch):
        configure(sessions, secrets, events=ALL_EVENTS)
        monkeypatch.setattr(notify, "check_for_update", _raise)
        run_id = make_run(sessions, dry_run=True, stats={"unhideable_rows": {"sarah": [1]}})
        notify.after_run(sessions, run_id, "1.9.0")
        assert queued(sessions) == []

    def test_it_never_raises_into_the_run(self, sessions, secrets, monkeypatch):
        configure(sessions, secrets, events=ALL_EVENTS)
        monkeypatch.setattr(notify, "check_for_update", _raise)
        notify.after_run(sessions, make_run(sessions), "1.9.0")


class TestAuthHeader:
    def test_the_header_goes_on_every_send_including_the_test(self, sessions, secrets):
        configure(
            sessions,
            secrets,
            **{"notify.webhook.auth_header_name": "X-Gotify-Key", "notify.webhook.auth_header_value": "s3cr3t-v4lue"},
        )
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
            with sessions() as session:
                notify.deliver(SettingsStore(session, secrets), notify.sample_item())
        assert route.calls.last.request.headers["X-Gotify-Key"] == "s3cr3t-v4lue"

    def test_the_default_header_name_is_authorization(self, sessions, secrets):
        configure(sessions, secrets, **{"notify.webhook.auth_header_value": "Bearer abc123def456"})
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
            with sessions() as session:
                notify.deliver(SettingsStore(session, secrets), notify.sample_item())
        assert route.calls.last.request.headers["Authorization"] == "Bearer abc123def456"

    def test_a_blank_name_sends_no_header(self, sessions, secrets):
        """How the owner stops sending one without removing the webhook: the Connections card says so."""
        configure(
            sessions,
            secrets,
            **{"notify.webhook.auth_header_name": "", "notify.webhook.auth_header_value": "Bearer abc123def456"},
        )
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
            with sessions() as session:
                notify.deliver(SettingsStore(session, secrets), notify.sample_item())
        assert "authorization" not in route.calls.last.request.headers
        assert "bearer abc123def456" not in {v.lower() for v in route.calls.last.request.headers.values()}

    def test_no_value_sends_no_header(self, sessions, secrets):
        configure(sessions, secrets)
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
            with sessions() as session:
                notify.deliver(SettingsStore(session, secrets), notify.sample_item())
        assert "authorization" not in route.calls.last.request.headers

    def test_the_value_never_reaches_an_error_message(self, sessions, secrets):
        value = "zq7-private-header-value"
        configure(sessions, secrets, **{"notify.webhook.auth_header_value": value})
        with respx.mock:
            respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError(f"refused while sending {value}"))
            with sessions() as session, pytest.raises(notify.NotifyFailed) as caught:
                notify.deliver(SettingsStore(session, secrets), notify.sample_item())
        assert value not in str(caught.value)

    def test_the_escaped_form_of_the_value_is_scrubbed_too(self):
        """h11 quotes a refused header value as bytes, so a tab arrives as a backslash and a `t`, which an
        exact match misses."""
        value = "s3cr3t\\KEY\t"
        message = f"LocalProtocolError: Illegal header value {value.encode()!r}"
        assert "s3cr3t" not in notify.scrub(message, value)

    def test_a_value_that_occurs_in_the_url_cannot_shield_the_webhook_token(self):
        message = "ConnectError: https://discord.com/api/webhooks/123/TOKxyzSECRETTAIL refused"
        assert "SECRETTAIL" not in notify.scrub(message, "https")

    def test_the_value_is_a_secret_and_the_senders_own_state_is_private(self):
        assert "notify.webhook.auth_header_value" in SECRET_KEYS
        assert {
            "notify.webhook.privacy_sent_at",
            "notify.webhook.requests_seen",
            "notify.webhook.update_sent",
        } <= PRIVATE_KEYS


def _raise(*_args, **_kwargs):
    raise RuntimeError("unavailable")
