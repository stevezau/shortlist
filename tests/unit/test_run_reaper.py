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

from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from shortlist.server.db.models import Run
from shortlist.server.main import create_app

pytestmark = pytest.mark.integration


def _boot(tmp_path: Path) -> TestClient:
    with TestClient(create_app(config_dir=tmp_path)) as client:
        return client


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
def test_a_run_left_mid_flight_is_aborted_on_the_next_boot(tmp_path: Path, stale_status: str):
    first = _boot(tmp_path)
    run_id = _seed_run(first, stale_status)

    second = _boot(tmp_path)  # same config dir -> same database, exactly as a container restart

    assert _run(second, run_id).status == "aborted"


def test_an_aborted_run_gets_a_finish_time_so_it_stops_looking_live(tmp_path: Path):
    """Without finished_at the UI has a terminal run with no end — it renders as still going."""
    first = _boot(tmp_path)
    run_id = _seed_run(first, "running")

    second = _boot(tmp_path)

    assert _run(second, run_id).finished_at is not None


def test_a_finished_run_is_left_exactly_as_it_was(tmp_path: Path):
    """The reap must only ever touch runs that never got to write their own outcome."""
    first = _boot(tmp_path)
    done = _seed_run(first, "ok")
    failed = _seed_run(first, "error")

    second = _boot(tmp_path)

    assert _run(second, done).status == "ok"
    assert _run(second, failed).status == "error"


def test_a_crash_queues_a_consistency_pass_so_it_does_not_wait_for_the_schedule(tmp_path: Path):
    """A run that died left rows delivered but UNPROMOTED — safe, but nobody sees them and nothing
    would put the server right until the next schedule, potentially a day away."""
    from shortlist.server.db.models import Job

    first = _boot(tmp_path)
    _seed_run(first, "running")

    second = _boot(tmp_path)

    with second.app.state.sessions() as session:
        assert [j.kind for j in session.query(Job).all()] == ["privacy.sync"]


def test_a_clean_boot_queues_nothing(tmp_path: Path):
    """Otherwise every restart would fire a server-wide share-filter pass for no reason — minutes of
    throttled plex.tv writes on a container that simply restarted."""
    from shortlist.server.db.models import Job

    first = _boot(tmp_path)
    _seed_run(first, "ok")

    second = _boot(tmp_path)

    with second.app.state.sessions() as session:
        assert session.query(Job).count() == 0
