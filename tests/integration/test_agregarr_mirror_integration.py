"""The shelf mirror against a live fake agregarr — real httpx, real AgregarrClient, nothing mocked.

The unit tests pin the decisions; this pins the WIRING. It is the only place that proves the run
actually reaches agregarr: that `_order_phase` calls the mirror, that the payload survives the real
client, and that agregarr's stored order afterwards reproduces the shelf.

The fake refuses requests the same way the real server does — notably it serves configs with no
`position` field and rejects a POST whose items lack one, which is the 400 the first live attempt
hit. A lenient fake would have called that code correct.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import uvicorn

from shortlist.engine.clients.agregarr import AgregarrClient
from shortlist.engine.models import EngineConfig, RunReport
from shortlist.engine.pipeline import _order_phase
from tests.fakes.fake_agregarr import API_KEY, make_fake_agregarr, seed_agregarr_state

pytestmark = pytest.mark.integration

LIB = "1"
ROWS = ["100", "101", "102", "103"]
FOREIGN = ["900", "901"]


class _UvicornThread:
    def __init__(self, app):
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.url = ""

    def start(self) -> _UvicornThread:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start within 10s")
            time.sleep(0.01)
        self.url = f"http://127.0.0.1:{self._server.servers[0].sockets[0].getsockname()[1]}"
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def agregarr():
    """A live fake agregarr holding our rows scattered below its own collections."""
    state = seed_agregarr_state(rows=ROWS, foreign=FOREIGN, library_id=LIB)
    server = _UvicornThread(make_fake_agregarr(state)).start()
    yield state, server.url
    server.stop()


def shelf_section() -> MagicMock:
    """Movies as Plex serves it AFTER Shortlist has ordered it: our rows on top, foreign below."""
    section = MagicMock(type="movie", key=LIB, title="Movies")
    section.managedHubs.return_value = [MagicMock(identifier="movie.recentlyadded")] + [
        MagicMock(identifier=f"custom.collection.{LIB}.{rk}") for rk in ROWS + FOREIGN
    ]
    return section


def make_ctx(url: str, **config_kwargs):
    ctx = MagicMock()
    ctx.config = EngineConfig(manage_shelf_order=True, dry_run=False, **config_kwargs)
    ctx.delivery_sections = [shelf_section()]
    # The delivery ledger: one row per USER, which is what a per-person row set looks like. Keying
    # them all under one user would collapse the dict to a single entry.
    ctx.delivered_keys = {(f"user{i}", "picked", LIB): int(rk) for i, rk in enumerate(ROWS)}
    ctx.write_lock = threading.Lock()
    ctx.agregarr = AgregarrClient(url, API_KEY)
    return ctx


def run_order_phase(ctx) -> RunReport:
    report = RunReport(started_at=datetime.now(UTC))
    _order_phase(ctx, report)
    return report


class TestTheRunReachesAgregarr:
    def test_a_run_stores_our_order_and_agregarr_would_then_reproduce_the_shelf(self, agregarr):
        state, url = agregarr
        ctx = make_ctx(url)

        report = run_order_phase(ctx)

        # It actually wrote, to the right library and the right sort space.
        assert len(state.writes) == 1
        library_id, context, _ = state.writes[0]
        assert (library_id, context) == (LIB, "home")

        # The real assertion: what agregarr would push NEXT now matches the live shelf, with our
        # rows contiguous at the top instead of scattered below the foreign collections.
        expected = ["1:movie.recentlyadded"] + [f"row-{rk}" for rk in ROWS] + [f"foreign-{rk}" for rk in FOREIGN]
        assert state.order_for(LIB) == expected

        entry = report.agregarr_mirrors[0]
        assert entry["ok"] is True
        assert entry["changed"] is True
        assert entry["rows_placed"] == len(ROWS)
        assert entry["rows_contiguous"] is True

    def test_the_unplaced_row_is_given_a_real_place(self, agregarr):
        # The last seeded row has sortOrderHome 0 ("unplaced"), which agregarr sorts LAST. Four of
        # SFLIX's rows were in exactly this state.
        state, url = agregarr
        assert state.order_for(LIB)[-1] == f"row-{ROWS[-1]}"

        run_order_phase(make_ctx(url))

        assert state.order_for(LIB)[-1] != f"row-{ROWS[-1]}"

    def test_a_second_run_writes_nothing(self, agregarr):
        """The steady state. Without this the mirror would re-POST every run forever."""
        state, url = agregarr
        run_order_phase(make_ctx(url))
        assert len(state.writes) == 1

        report = run_order_phase(make_ctx(url))

        assert len(state.writes) == 1  # unchanged — no second write
        assert report.agregarr_mirrors[0]["changed"] is False


class TestItNeverBreaksTheRun:
    def test_a_dead_agregarr_does_not_fail_the_run(self, agregarr):
        state, url = agregarr
        ctx = make_ctx(url)
        ctx.agregarr = AgregarrClient("http://127.0.0.1:9", API_KEY)  # nothing listening

        report = run_order_phase(ctx)  # must not raise

        assert report.agregarr_mirrors[0]["ok"] is False
        assert state.writes == []

    def test_a_rejected_key_does_not_fail_the_run(self, agregarr):
        state, url = agregarr
        ctx = make_ctx(url)
        ctx.agregarr = AgregarrClient(url, "wrong-key")

        report = run_order_phase(ctx)  # must not raise

        assert report.agregarr_mirrors[0]["ok"] is False
        assert "rejected the API key" in report.agregarr_mirrors[0]["error"]
        assert state.writes == []

    def test_dry_run_touches_nothing(self, agregarr):
        state, url = agregarr
        ctx = make_ctx(url)
        ctx.config.dry_run = True

        report = run_order_phase(ctx)

        assert state.writes == []
        assert report.agregarr_mirrors[0]["dry_run"] is True

    def test_shelf_ordering_off_skips_agregarr_entirely(self, agregarr):
        state, url = agregarr
        ctx = make_ctx(url)
        ctx.config.manage_shelf_order = False

        run_order_phase(ctx)

        assert state.writes == []


class TestCostOfARun:
    def test_the_instance_is_read_once_per_run_not_once_per_library(self, agregarr):
        """Both endpoints return the WHOLE instance regardless of library, so reading them per
        library re-downloads all 213 configs each time — and, on a hung instance, pays the timeout
        once per library instead of once."""
        state, url = agregarr
        second = MagicMock(type="show", key="2", title="TV Shows")
        second.managedHubs.return_value = [MagicMock(identifier="tv.recentlyadded")]
        ctx = make_ctx(url)
        ctx.delivery_sections = [ctx.delivery_sections[0], second]

        run_order_phase(ctx)

        assert state.reads == {"preexisting": 1, "defaulthubs": 1}


class TestRealServerRefusals:
    def test_every_item_we_send_survives_agregarrs_type_guards(self, agregarr):
        """The guards are bare `'collectionRatingKey' in config` / `'hubIdentifier' in config`
        checks, and an item satisfying NEITHER is skipped while the request still returns 200. A
        payload trimmed past the join key would look like a clean write and change nothing."""
        state, url = agregarr

        run_order_phase(make_ctx(url))

        assert state.skipped == []

    def test_the_write_does_not_clobber_fields_we_never_modelled(self, agregarr):
        """Agregarr merges `{...stored, ...sent}`, so anything we echo back overwrites its storage.
        A poster path we read and returned would be written straight back over its own."""
        state, url = agregarr
        target = next(c for c in state.preexisting if c["id"] == "row-100")
        target["customPoster"] = "agregarr-owned.png"
        target["visibilityConfig"] = {"usersHome": True, "serverOwnerHome": False}

        run_order_phase(make_ctx(url))

        assert target["customPoster"] == "agregarr-owned.png"
        assert target["visibilityConfig"] == {"usersHome": True, "serverOwnerHome": False}
        assert target["sortOrderHome"] == 2  # but the order WAS applied

    def test_the_write_carries_position_because_agregarr_demands_it(self, agregarr):
        """The live 400. Agregarr serves configs WITHOUT `position` and requires it on the way back,
        so the client must stamp it — the fake rejects the request otherwise."""
        state, url = agregarr
        served = AgregarrClient(url, API_KEY).home_items(LIB)
        assert any("position" not in item for item in served), "fake must reproduce the missing field"

        run_order_phase(make_ctx(url))

        assert len(state.writes) == 1  # accepted

    def test_hub_configs_route_is_unusable_with_an_api_key(self, agregarr):
        """`/hubs/configs` is what the OpenAPI spec advertises, and it 307s an API key to /login.
        The client must read `/defaulthubs`; this pins why."""
        _state, url = agregarr

        items = AgregarrClient(url, API_KEY).home_items(LIB)

        assert any(i.get("configType") == "hub" for i in items), "built-in hubs must still be found"
