"""A run the previous process died inside must not still claim to be running.

`create_app` already aborts orphaned runs at boot; this pins that behaviour, which had no test. A
run only ever leaves `running` via the code that finishes it, so without the reap a container
restart (or an OOM kill) would leave a run that ended days ago reporting itself as in progress for
ever — the Runs page spinning on a run with no process behind it, and "is a run happening right
now?" (which gates starting another) answering wrongly.

Nothing on Plex needs repairing here: a run that dies leaves its rows DELIVERED BUT UNPROMOTED
(plex-safety rule 1), so a half-finished run is visible to nobody. This is about the record telling
the truth.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from shortlist.server.db.models import Run
from tests.shared_app import app_for

pytestmark = pytest.mark.integration


@pytest.fixture
def boot() -> Iterator[Callable[[Path], TestClient]]:
    with ExitStack() as engines:

        def start(tmp_path: Path) -> TestClient:
            app = app_for(tmp_path)

            def dispose() -> None:
                sessions = getattr(app.state, "sessions", None)
                if sessions is not None:
                    sessions.kw["bind"].dispose()

            # Own partial startup and pools reopened by this test's post-shutdown reads/writes.
            engines.callback(dispose)
            with TestClient(app) as client:
                pass
            return client

        yield start


def _seed_run(client: TestClient, status: str) -> int:
    with client.app.state.sessions() as session:
        run = Run(status=status, started_at=datetime.now(UTC), trigger="manual")
        session.add(run)
        session.commit()
        return run.id


def _run(client: TestClient, run_id: int) -> Run:
    with client.app.state.sessions() as session:
        return session.get(Run, run_id)


@pytest.mark.parametrize("stale_status", ["running", "queued"])
def test_a_run_left_mid_flight_is_aborted_on_the_next_boot(
    tmp_path: Path, stale_status: str, boot: Callable[[Path], TestClient]
):
    first = boot(tmp_path)
    run_id = _seed_run(first, stale_status)

    second = boot(tmp_path)  # same config dir -> same database, exactly as a container restart

    assert _run(second, run_id).status == "aborted"


def test_an_aborted_run_gets_a_finish_time_so_it_stops_looking_live(tmp_path: Path, boot: Callable[[Path], TestClient]):
    """Without finished_at the UI has a terminal run with no end — it renders as still going."""
    first = boot(tmp_path)
    run_id = _seed_run(first, "running")

    second = boot(tmp_path)

    assert _run(second, run_id).finished_at is not None


def test_a_finished_run_is_left_exactly_as_it_was(tmp_path: Path, boot: Callable[[Path], TestClient]):
    """The reap must only ever touch runs that never got to write their own outcome."""
    first = boot(tmp_path)
    done = _seed_run(first, "ok")
    failed = _seed_run(first, "error")

    second = boot(tmp_path)

    assert _run(second, done).status == "ok"
    assert _run(second, failed).status == "error"


def test_a_crash_queues_a_consistency_pass_so_it_does_not_wait_for_the_schedule(
    tmp_path: Path, boot: Callable[[Path], TestClient]
):
    """A run that died left rows delivered but UNPROMOTED — safe, but nobody sees them and nothing
    would put the server right until the next schedule, potentially a day away."""
    from shortlist.server.db.models import Job

    first = boot(tmp_path)
    _seed_run(first, "running")

    second = boot(tmp_path)

    with second.app.state.sessions() as session:
        assert [j.kind for j in session.query(Job).all()] == ["privacy.sync"]


def test_a_clean_boot_queues_nothing(tmp_path: Path, boot: Callable[[Path], TestClient]):
    """Otherwise every restart would fire a server-wide share-filter pass for no reason — minutes of
    throttled plex.tv writes on a container that simply restarted."""
    from shortlist.server.db.models import Job

    first = boot(tmp_path)
    _seed_run(first, "ok")

    second = boot(tmp_path)

    with second.app.state.sessions() as session:
        assert session.query(Job).count() == 0


@pytest.mark.parametrize(("stale_status", "announced"), [("running", True), ("queued", False)])
def test_a_run_a_restart_cut_short_is_announced_as_stopped_if_it_had_started(
    tmp_path: Path, stale_status: str, announced: bool, boot: Callable[[Path], TestClient]
):
    """`run.stopped` covers a restart as well as the Stop button. A run still queued never started, so
    there is nothing to say it stopped."""
    from shortlist.server.db.models import Job
    from shortlist.server.settings_store import SettingsStore

    first = boot(tmp_path)
    with first.app.state.sessions() as session:
        store = SettingsStore(session)
        store.set("notify.webhook.enabled", True)
        store.set("notify.webhook.events", ["run.stopped"])
        run = Run(status=stale_status, started_at=datetime.now(UTC), trigger="manual")
        if stale_status == "running":
            run.began_at = datetime.now(UTC)
        session.add(run)
        session.commit()
        run_id = run.id

    second = boot(tmp_path)

    with second.app.state.sessions() as session:
        items = [j.payload["item"] for j in session.query(Job).filter(Job.kind == "notify.send")]
    assert [(i["event"], i["id"]) for i in items] == ([("run.stopped", f"run-stopped-{run_id}")] if announced else [])


class TestAScheduledRunCutShortIsFinishedOnce:
    """A scheduled run a restart cut short rebuilds only the people it never reached, once (owner decision
    2026-09-14). On a big server Watchtower replaced the container at 04:30 while the 03:30 run was half way,
    and 23 of 46 people went a day without a rebuild, the same people every night an image was published.
    Only once: the resumed run is not resumed again, so a crash loop cannot re-curate the server over and over."""

    @pytest.fixture
    def started(self, monkeypatch) -> list[dict]:
        from shortlist.server.services.run_service import RunService

        calls: list[dict] = []

        async def start_run(self, **kwargs):
            calls.append(kwargs)
            return 0

        monkeypatch.setattr(RunService, "start_run", start_run)
        return calls

    @staticmethod
    def _seed(client: TestClient, *, trigger="schedule", dry_run=False, hours_ago=1.0, reached=("amy",)) -> dict:
        from datetime import timedelta

        from shortlist.server.db.models import Collection, RunUser, User

        with client.app.state.sessions() as session:
            people = {
                slug: User(plex_account_id=1000 + i, username=slug, slug=slug, enabled=True)
                for i, slug in enumerate(("amy", "bob", "cat", "dan"))
            }
            people["dan"].enabled = False  # turned off since the run started
            rows = [
                Collection(slug="night_a", name="Night A", enabled=True),
                Collection(slug="night_b", name="Night B", enabled=True),
                Collection(slug="other_cron", name="Other cron", enabled=True),
            ]
            session.add_all([*people.values(), *rows])
            session.flush()
            run = Run(
                status="running",
                trigger=trigger,
                dry_run=dry_run,
                started_at=datetime.now(UTC) - timedelta(hours=hours_ago),
                stats={
                    "expected_users": [
                        {"slug": s, "username": s, "display_name": s} for s in ("amy", "bob", "cat", "dan")
                    ],
                    "expected_rows": [
                        {"slug": "night_a", "title": "Night A", "build": "picked"},
                        {"slug": "night_b", "title": "Night B", "build": "picked"},
                    ],
                },
            )
            session.add(run)
            session.flush()
            for slug in reached:
                session.add(RunUser(run_id=run.id, user_id=people[slug].id, status="ok"))
            session.commit()
            return {"users": {s: u.id for s, u in people.items()}, "rows": {r.slug: r.id for r in rows}}

    def test_it_rebuilds_only_the_people_the_run_never_reached(
        self, tmp_path: Path, started, boot: Callable[[Path], TestClient]
    ):
        ids = self._seed(boot(tmp_path))

        boot(tmp_path)

        assert started == [
            {
                "trigger": "resume",
                "dry_run": False,
                "user_ids": sorted([ids["users"]["bob"], ids["users"]["cat"]]),
                "collection_ids": sorted([ids["rows"]["night_a"], ids["rows"]["night_b"]]),
            }
        ]

    def test_the_consistency_pass_is_still_queued(self, tmp_path: Path, started, boot: Callable[[Path], TestClient]):
        from shortlist.server.db.models import Job

        self._seed(boot(tmp_path))
        second = boot(tmp_path)

        with second.app.state.sessions() as session:
            assert [j.kind for j in session.query(Job).all()] == ["privacy.sync"]

    @pytest.mark.parametrize(
        "seed",
        [
            {"trigger": "resume"},  # the resumed run itself: once, never a loop
            {"trigger": "manual"},  # a run someone started by hand is theirs to start again
            {"dry_run": True},  # safe mode wrote nothing to rebuild
            {"hours_ago": 21},  # the row's next scheduled run is the better answer by now
            {"reached": ("amy", "bob", "cat")},  # everyone enabled was reached before the restart
        ],
        ids=["resumed-run", "manual", "dry-run", "too-old", "all-reached"],
    )
    def test_nothing_is_rerun_when(self, tmp_path: Path, started, seed, boot: Callable[[Path], TestClient]):
        self._seed(boot(tmp_path), **seed)

        boot(tmp_path)

        assert started == []

    def test_rows_turned_off_since_are_not_rebuilt(self, tmp_path: Path, started, boot: Callable[[Path], TestClient]):
        from shortlist.server.db.models import Collection

        first = boot(tmp_path)
        ids = self._seed(first)
        with first.app.state.sessions() as session:
            session.get(Collection, ids["rows"]["night_b"]).enabled = False
            session.commit()

        boot(tmp_path)

        assert [call["collection_ids"] for call in started] == [[ids["rows"]["night_a"]]]

    def test_restoring_a_backup_that_caught_a_run_mid_flight_starts_no_run(
        self, tmp_path: Path, started, boot: Callable[[Path], TestClient]
    ):
        """A backup taken at 04:00 holds that night's run as `running`. Restoring it is not a restart
        cutting that run short, and a real run must not start on the restored configuration by itself."""
        from shortlist.server.services.backup import request_restore, take_backup

        first = boot(tmp_path)
        self._seed(first)
        backup = take_backup(tmp_path, label="manual")
        with first.app.state.sessions() as session:
            for run in session.query(Run).all():
                run.status = "ok"
            session.commit()
        assert request_restore(tmp_path, backup.name)

        boot(tmp_path)

        assert started == []
