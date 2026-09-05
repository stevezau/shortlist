"""The one external pipe: a whole-run failure reaching the owner's webhook.

Shortlist decides what is worth saying in `notifications.py` and always has. What it had no way to do
was SAY it anywhere but an in-app bell, on a tool that runs unattended at 3am. These tests cover the
pipe out of that registry — not a second opinion about what matters.

Two things here are load-bearing rather than incidental, and both have a test that fails loudly if
someone unpicks them:

* **The URL is a credential.** A Discord/Slack webhook URL is a bearer token in a URL, and
  `http_retry.redact` does not strip one — measured, not assumed
  (`test_redact_alone_does_not_strip_a_webhook_url`). Every string derived from a send failure goes
  through `notify.scrub` first, or the owner's token lands in `Job.error`, the job list and the
  support bundle (plex-safety rule 9).
* **The test button is not a separate implementation.** It builds an item and hands it to the same
  `deliver()` the 3am failure reaches, so a webhook that answers the button is a webhook that will
  answer the failure. A test button with its own send path proves nothing about the real one.

No test may touch the network: `respx` intercepts every httpx call, exactly as `test_arr.py` does.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from shortlist.engine.clients.http_retry import redact
from shortlist.server import notifications
from shortlist.server.db.models import Job, Run
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services import jobs, notify
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import SECRET_KEYS, SettingsStore

pytestmark = pytest.mark.integration

# A real Discord webhook's shape. The token is the second path segment — which is exactly why the
# PATH is what has to be stripped from any error text, not merely the query string.
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/AbCdEf-GhIjKl_MnOpQrStUvWxYz0123456789"


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    return make_session_factory(make_engine(tmp_path))


@pytest.fixture
def secrets(tmp_path: Path) -> SecretBox:
    return SecretBox(tmp_path)


@pytest.fixture
def state(sessions, secrets):
    """Minimal app.state: the notify handler needs sessions and a SecretBox, nothing else."""
    return SimpleNamespace(sessions=sessions, secrets=secrets, run_service=None)


def configure(sessions, secrets, *, url: str = WEBHOOK, enabled: bool = True) -> None:
    with sessions() as session:
        store = SettingsStore(session, secrets)
        store.set("notify.webhook.enabled", enabled)
        store.set("notify.webhook.url", url)


def drain(state) -> int:
    return asyncio.run(jobs.run_pending(state))


def job_row(sessions, job_id: int) -> Job:
    with sessions() as session:
        return session.get(Job, job_id)


def notify_jobs(sessions) -> list[Job]:
    with sessions() as session:
        return session.query(Job).filter(Job.kind == "notify.send").order_by(Job.id).all()


class TestSecretsNeverEscape:
    """Rule 9, for the one credential this feature introduces."""

    def test_the_webhook_url_is_a_secret_key(self):
        # Not a nicety: `SECRET_KEYS` is what encrypts it at rest AND what makes `all_public()`
        # return `•••••` instead of the token. Storing it as an ordinary setting would publish it to
        # every Settings page load.
        assert "notify.webhook.url" in SECRET_KEYS

    def test_redact_alone_does_not_strip_a_webhook_url(self):
        """The premise behind `notify.scrub`, pinned so it cannot quietly stop being true.

        `redact` strips bearer headers, `user:pass@host`, labelled `token`/`apikey` pairs and known
        provider key shapes. A webhook URL is none of those, so it survives untouched — and httpx puts
        the full request URL into the message of a `ConnectError` or an `HTTPStatusError`.
        """
        leaked = redact(f"ConnectError: All connection attempts failed for {WEBHOOK}")
        assert WEBHOOK in leaked, "if redact() ever learns this shape, notify.scrub can be simplified"

    def test_scrub_removes_the_url_path_and_keeps_the_host(self):
        cleaned = notify.scrub(f"ConnectError: All connection attempts failed for {WEBHOOK}")
        assert WEBHOOK not in cleaned
        assert "AbCdEf" not in cleaned and "123456789012345678" not in cleaned
        # The host survives on purpose: it says WHICH service refused, and the owner configured it.
        assert "https://discord.com/" in cleaned

    def test_scrub_removes_a_token_carried_in_the_query_string(self):
        """Not every webhook puts its token in the path.

        Discord and Slack do, which is what makes it easy to write a pattern anchored on `/` and
        believe it covers the case. A generic endpoint — a home-automation box, an n8n node — is just
        as likely to want `?token=…`, and that URL is every bit as much a bearer credential.
        """
        query_style = "https://hooks.example.com?token=s3cr3t-value"
        cleaned = notify.scrub(f"ConnectError: failed to connect to {query_style}")
        assert "s3cr3t-value" not in cleaned and "token=" not in cleaned
        assert "hooks.example.com" in cleaned

    def test_scrub_still_applies_every_other_credential_shape(self):
        # `scrub` must be a superset of `redact`, not a replacement for it.
        assert "REDACTED" in notify.scrub("Authorization: Bearer abcdefghijklmnop")
        assert notify.scrub("https://user:hunter2@example.com/hook").count("hunter2") == 0

    def test_a_failed_send_puts_no_url_in_the_job_error(self, sessions, secrets, state):
        configure(sessions, secrets)
        with respx.mock:
            respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError(f"failed to connect to {WEBHOOK}"))
            job_id = notify.enqueue_run_failure(sessions, _failed_run(sessions))
            drain(state)
        job = job_row(sessions, job_id)
        assert job.error, "a send that could not connect must record why"
        assert WEBHOOK not in job.error and "AbCdEf" not in job.error

    def test_a_rejected_send_puts_no_url_in_the_job_error(self, sessions, secrets, state):
        # The other half of the matrix: the request LANDED and was refused. httpx renders the full
        # URL in `HTTPStatusError` too, via the response it carries.
        configure(sessions, secrets)
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(401, text="invalid webhook token"))
            job_id = notify.enqueue_run_failure(sessions, _failed_run(sessions))
            drain(state)
        job = job_row(sessions, job_id)
        assert job.error and WEBHOOK not in job.error and "AbCdEf" not in job.error
        assert "401" in job.error, "the owner still has to be told what the webhook said"


class TestWebhookBody:
    """The wire format. Asserted field by field, because a receiver's routing rule depends on it."""

    def test_the_body_is_exactly_this(self):
        item = {
            "id": "run-failed-12",
            "severity": "error",
            "title": "The last run failed",
            "body": "The most recent run ended in an error — open it to see what went wrong.",
            "action_url": "/runs/12",
        }
        body = notify.webhook_body(item, now=datetime(2026, 9, 5, 3, 31, tzinfo=UTC))
        assert body == {
            "source": "shortlist",
            "version": 1,
            "id": "run-failed-12",
            "severity": "error",
            "title": "The last run failed",
            "message": "The most recent run ended in an error — open it to see what went wrong.",
            "path": "/runs/12",
            "sent_at": "2026-09-05T03:31:00+00:00",
        }

    def test_the_body_is_json_serialisable_as_sent(self):
        # httpx would raise at send time otherwise, on a code path that only runs at 3am.
        json.dumps(notify.webhook_body(notify.test_item()))

    def test_the_body_names_no_account(self):
        """v1's privacy floor, asserted rather than assumed.

        Sending "sarah can see mike's row" to a third-party webhook would make a privacy incident MORE
        exposed, not less. v1 carries one event that names nobody by construction; this pins that, and
        fails the moment someone routes a per-account alert through here without rewording it.
        """
        with_run = notifications.run_failed_alert(SimpleNamespace(id=12))
        assert set(notify.webhook_body(with_run)) == {
            "source",
            "version",
            "id",
            "severity",
            "title",
            "message",
            "path",
            "sent_at",
        }
        text = f"{with_run['title']} {with_run['body']}"
        assert "sarah" not in text.lower() and "mike" not in text.lower()


class TestDeliver:
    """The single sender both callers reach."""

    def test_it_posts_the_body_to_the_configured_url(self, sessions, secrets):
        configure(sessions, secrets)
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            with sessions() as session:
                notify.deliver(SettingsStore(session, secrets), notify.test_item())
        assert route.called
        sent = json.loads(route.calls.last.request.content)
        assert sent["source"] == "shortlist" and sent["version"] == 1
        assert sent["id"] == "notify-test"
        assert route.calls.last.request.headers["content-type"] == "application/json"

    def test_it_refuses_when_the_webhook_is_switched_off(self, sessions, secrets):
        configure(sessions, secrets, enabled=False)
        with sessions() as session, pytest.raises(notify.NotifyNotConfigured):
            notify.deliver(SettingsStore(session, secrets), notify.test_item())

    def test_it_refuses_when_the_url_is_blank(self, sessions, secrets):
        configure(sessions, secrets, url="")
        with sessions() as session, pytest.raises(notify.NotifyNotConfigured) as caught:
            notify.deliver(SettingsStore(session, secrets), notify.test_item())
        # The error says what to DO, per the house voice — an operator reads this on the test button.
        assert "address" in str(caught.value).lower()

    def test_it_makes_exactly_one_http_call_per_attempt(self, sessions, secrets):
        """Retry belongs to the job queue, not to the client.

        Layering `http_retry`'s three attempts under the queue's five would turn a five-attempt policy
        into fifteen requests at a webhook that is already unhappy.
        """
        configure(sessions, secrets)
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
            with sessions() as session, pytest.raises(notify.NotifyFailed):
                notify.deliver(SettingsStore(session, secrets), notify.test_item())
        assert route.call_count == 1


class TestRunFailureHook:
    """Which runs get an alert, and which deliberately do not."""

    def test_a_failed_run_queues_one_send_carrying_the_bell_s_own_wording(self, sessions, secrets):
        configure(sessions, secrets)
        run_id = _failed_run(sessions)
        assert notify.enqueue_run_failure(sessions, run_id) is not None
        queued = notify_jobs(sessions)
        assert len(queued) == 1
        # The payload is the notification the in-app bell would show for this run — one wording, two
        # destinations. A second wording here is how a webhook message and the bell start disagreeing.
        with sessions() as session:
            expected = notifications.run_failed_alert(session.get(Run, run_id))
        assert queued[0].payload["item"] == expected

    def test_a_successful_run_queues_nothing(self, sessions, secrets):
        configure(sessions, secrets)
        assert notify.enqueue_run_failure(sessions, _ok_run(sessions)) is None
        assert notify_jobs(sessions) == []

    def test_a_dry_run_queues_nothing(self, sessions, secrets):
        """A dry run is a preview somebody is watching. The alert exists for the failure nobody saw."""
        configure(sessions, secrets)
        assert notify.enqueue_run_failure(sessions, _failed_run(sessions, dry_run=True)) is None
        assert notify_jobs(sessions) == []

    def test_nothing_is_queued_while_the_webhook_is_off(self, sessions, secrets):
        configure(sessions, secrets, enabled=False)
        assert notify.enqueue_run_failure(sessions, _failed_run(sessions)) is None
        assert notify_jobs(sessions) == []

    def test_it_never_raises_into_the_run(self, sessions, secrets, monkeypatch):
        """Telling the owner must not be able to change what the run did.

        The caller is `RunService`'s error path — the one place already handling a failure. An
        exception here would replace the run's real error with a notification's.
        """
        configure(sessions, secrets)
        monkeypatch.setattr(jobs, "enqueue", _raise)
        assert notify.enqueue_run_failure(sessions, _failed_run(sessions)) is None

    def test_the_queued_job_is_read_only_and_never_manual(self):
        kind = jobs.BY_KIND["notify.send"]
        # A writer would take the exclusive Plex lock and queue behind every run — for an HTTP POST
        # that touches nothing on Plex.
        assert kind.writes_plex is False
        # `manual` puts a generic "run it" button on the Jobs page. This kind's payload IS the message,
        # so a button that fires it with no payload would send an empty alert.
        assert kind.manual is False


class TestDeliveryThroughTheQueue:
    """End to end: a failed run, the queue, and the request that leaves the box."""

    def test_a_failed_run_sends_the_bell_s_alert_to_the_webhook(self, sessions, secrets, state):
        configure(sessions, secrets)
        run_id = _failed_run(sessions)
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            job_id = notify.enqueue_run_failure(sessions, run_id)
            drain(state)
        assert route.called
        sent = json.loads(route.calls.last.request.content)
        assert sent["id"] == f"run-failed-{run_id}"
        assert sent["severity"] == "error"
        assert sent["title"] == "The last run failed"
        assert sent["path"] == f"/runs/{run_id}"
        assert job_row(sessions, job_id).status == "done"

    def test_switching_the_webhook_off_before_the_job_runs_skips_it_rather_than_failing(self, sessions, secrets, state):
        """A job that fails dead-letters and raises the in-app "jobs have failed" alert. An owner who
        turned notifications off in the meantime has not had a failure — they have changed their mind.
        """
        configure(sessions, secrets)
        job_id = notify.enqueue_run_failure(sessions, _failed_run(sessions))
        configure(sessions, secrets, enabled=False)
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            drain(state)
        assert not route.called
        job = job_row(sessions, job_id)
        assert job.status == "done" and "off" in job.detail.lower()

    def test_a_webhook_that_never_answers_dead_letters_instead_of_retrying_for_ever(
        self, sessions, secrets, state, monkeypatch
    ):
        """The loop that closes back to the channel that always works.

        Once the job lands `failed`, the EXISTING `_failed_jobs` builder raises it in the bell — the
        one destination that cannot be broken by the thing that is broken. Nothing new is needed for
        that; it falls out of using the queue instead of a second one.
        """
        # The schedule is asserted on its own below; here it is only in the way of the dead-letter.
        monkeypatch.setattr(jobs, "_backoff_for", lambda kind: (0,))
        configure(sessions, secrets)
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
            job_id = notify.enqueue_run_failure(sessions, _failed_run(sessions))
            for _ in range(jobs.NOTIFY_ATTEMPTS + 1):
                drain(state)
        job = job_row(sessions, job_id)
        assert job.status == "failed"
        assert job.attempts == jobs.NOTIFY_ATTEMPTS == route.call_count
        # And the bell says so, without any help from this module.
        with sessions() as session:
            assert notifications._failed_jobs(session) is not None

    def test_the_send_backs_off_for_hours_not_minutes(self):
        """A webhook outage lasts hours, so the job has to outlive one.

        Asserted as the span the job ACTUALLY gets, not as `sum(NOTIFY_BACKOFF_S)`. Those are not the
        same number and the difference is a real bug this test used to be blind to: a job waits
        BETWEEN attempts, so N attempts only ever charge N-1 waits, and sizing the attempts at
        `len(schedule)` left the longest entry unreachable — 1.6 hours of retrying under a constant
        that reads as 4.6, with the wrong figure shipped in the Jobs page description.
        """
        schedule = jobs._backoff_for("notify.send")
        assert schedule == jobs.NOTIFY_BACKOFF_S
        charged = [schedule[min(n - 1, len(schedule) - 1)] for n in range(1, jobs.NOTIFY_ATTEMPTS)]
        assert sum(charged) >= 4 * 3600, f"only {sum(charged) / 3600:.1f}h of retries"
        assert schedule[-1] in charged, "the longest wait is never reached"
        # Every other kind keeps the shared default — this must not become a global change.
        assert jobs._backoff_for("sync.users") == jobs._BACKOFF_S


class TestTestButtonSharesTheRealPath:
    """The deliberate antidote to a test button that proves nothing.

    A notifier whose Test succeeds while the 3am path is unwired is worse than no notifier: it is a
    notifier the owner now trusts. So the button builds an item and hands it to `deliver` — the same
    function, the same settings read, the same body builder and the same HTTP call the run failure
    reaches.
    """

    def test_both_items_are_sent_by_the_same_builder_and_sender(self, sessions, secrets):
        configure(sessions, secrets)
        run_item = notifications.run_failed_alert(SimpleNamespace(id=7))
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            with sessions() as session:
                store = SettingsStore(session, secrets)
                notify.deliver(store, notify.test_item())
                notify.deliver(store, run_item)
        assert route.call_count == 2
        test_body, run_body = (json.loads(call.request.content) for call in route.calls)
        # Same URL, same envelope, same field set — the only difference is what happened.
        assert {c.request.url for c in route.calls} == {httpx.URL(WEBHOOK)}
        assert set(test_body) == set(run_body)
        assert test_body["source"] == run_body["source"] == "shortlist"
        assert test_body["version"] == run_body["version"] == 1

    def test_the_test_item_says_it_is_a_test(self, sessions, secrets):
        item = notify.test_item()
        assert item["id"] == "notify-test"
        assert "test" in item["title"].lower()
        # Severity `info`: a receiver routing errors to a pager must not be paged by a button press.
        assert item["severity"] == "info"


def _raise(*_args, **_kwargs):
    raise RuntimeError("the queue is unavailable")


def _failed_run(sessions, *, dry_run: bool = False) -> int:
    with sessions() as session:
        run = Run(trigger="schedule", status="error", dry_run=dry_run, finished_at=datetime.now(UTC), stats={})
        session.add(run)
        session.commit()
        return run.id


def _ok_run(sessions) -> int:
    with sessions() as session:
        run = Run(trigger="schedule", status="ok", finished_at=datetime.now(UTC), stats={})
        session.add(run)
        session.commit()
        return run.id
