"""The run's agregarr step — `_mirror_shelf_to_agregarr`.

These pin the contract that lets a third-party, unversioned API sit at the end of a run at all: it
never fails the run, it never writes when agregarr already agrees, and it does not write at all in
dry-run or when the owner has handed the shelf to another tool.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.agregarr import AgregarrError
from shortlist.engine.context import EngineContext
from shortlist.engine.models import RunReport
from shortlist.engine.pipeline import _mirror_shelf_to_agregarr, _order_phase

LIB = "1"


def hub(identifier: str) -> MagicMock:
    return MagicMock(identifier=identifier)


def section() -> MagicMock:
    """Movies, with our row at the top and one foreign collection below it — the live layout."""
    sec = MagicMock(type="movie", key=LIB, title="Movies")
    sec.managedHubs.return_value = [
        hub("movie.recentlyadded"),
        hub(f"custom.collection.{LIB}.100"),  # ours
        hub(f"custom.collection.{LIB}.900"),  # agregarr's own
    ]
    return sec


def agregarr_items(row_sort_order: int) -> list[dict]:
    """Agregarr's stored order. `row_sort_order=40` is the scattered case — the foreign collection
    holds key 2 and so sorts ABOVE our row, which is the fight this feature exists to end."""
    return [
        {
            "id": "1:movie.recentlyadded",
            "hubIdentifier": "movie.recentlyadded",
            "libraryId": LIB,
            "sortOrderHome": 1,
            "configType": "hub",
        },
        {
            "id": "cfg-100",
            "collectionRatingKey": "100",
            "libraryId": LIB,
            "sortOrderHome": row_sort_order,
            "configType": "preExisting",
        },
        {
            "id": "cfg-900",
            "collectionRatingKey": "900",
            "libraryId": LIB,
            "sortOrderHome": 2,
            "configType": "preExisting",
        },
    ]


@pytest.fixture
def ctx(engine_config, mock_plextv, mock_tmdb, mock_curator, snapshot_store) -> EngineContext:
    """A context with only what the shelf phases touch — this step runs after every other phase."""
    return EngineContext(
        config=engine_config,
        plex=MagicMock(),
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=MagicMock(),
        curator=mock_curator,
        snapshots=snapshot_store,
    )


@pytest.fixture
def wired(ctx):
    """A ctx with one library on the shelf and a connected agregarr whose order is SCATTERED."""
    ctx.delivery_sections = [section()]
    ctx.config.dry_run = False
    ctx.agregarr = MagicMock()
    ctx.agregarr.home_items_by_library.return_value = {LIB: agregarr_items(row_sort_order=40)}
    return ctx


class TestSkips:
    def test_does_nothing_when_no_agregarr_is_connected(self, ctx):
        # Most servers have no agregarr, so this must be free for them — not one extra PMS read
        # per library per night to discover there is nothing to do.
        ctx.delivery_sections = [section()]
        ctx.agregarr = None
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(ctx, report)

        assert report.agregarr_mirrors == []
        ctx.delivery_sections[0].managedHubs.assert_not_called()

    def test_order_phase_skips_the_mirror_when_shelf_ordering_is_off(self, ctx):
        # The owner deferred the shelf to a co-managing tool; writing our order into agregarr would
        # override the very tool they deferred to.
        ctx.delivery_sections = [section()]
        ctx.config.manage_shelf_order = False
        ctx.agregarr = MagicMock()
        report = RunReport(started_at=datetime.now(UTC))

        _order_phase(ctx, report)

        ctx.agregarr.home_items_by_library.assert_not_called()
        ctx.agregarr.apply_home_order.assert_not_called()

    def test_writes_nothing_when_agregarr_already_agrees(self, ctx):
        ctx.delivery_sections = [section()]
        ctx.config.dry_run = False
        ctx.agregarr = MagicMock()
        # Stored order already reproduces the live shelf: ours at 2, the foreign collection at 3.
        items = agregarr_items(row_sort_order=2)
        items[2]["sortOrderHome"] = 3
        ctx.agregarr.home_items_by_library.return_value = {LIB: items}
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(ctx, report)

        ctx.agregarr.apply_home_order.assert_not_called()
        assert report.agregarr_mirrors[0]["changed"] is False
        assert report.agregarr_mirrors[0]["ok"] is True

    def test_dry_run_reports_the_plan_without_writing(self, wired):
        wired.config.dry_run = True
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(wired, report)

        wired.agregarr.apply_home_order.assert_not_called()
        assert report.agregarr_mirrors[0]["dry_run"] is True
        assert report.agregarr_mirrors[0]["changed"] is True


class TestWrites:
    def test_sends_the_live_shelf_order_for_the_library(self, wired):
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(wired, report)

        call = wired.agregarr.apply_home_order.call_args
        assert call.args[0] == LIB
        # The ORDER is the payload — assert it, not just that a write happened. Our row must land
        # above the foreign collection that agregarr had sorting first.
        assert [i["id"] for i in call.args[1]] == ["1:movie.recentlyadded", "cfg-100", "cfg-900"]
        assert report.agregarr_mirrors[0]["changed"] is True


class TestFailSoft:
    def test_a_failed_read_never_raises_and_is_recorded_once(self, wired):
        # One fetch serves every library, so an unreachable agregarr costs ONE timeout and ONE
        # record — not one per library, which is what re-reading per library used to mean.
        second = MagicMock(type="show", key="2", title="TV Shows")
        second.managedHubs.return_value = [hub("tv.recentlyadded")]
        wired.delivery_sections = [wired.delivery_sections[0], second]
        wired.agregarr.home_items_by_library.side_effect = AgregarrError("Agregarr unreachable (ConnectError)")
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(wired, report)  # must not raise

        assert len(report.agregarr_mirrors) == 1
        assert report.agregarr_mirrors[0]["ok"] is False
        assert "AgregarrError" in report.agregarr_mirrors[0]["error"]
        wired.agregarr.apply_home_order.assert_not_called()

    def test_a_failed_write_never_raises_and_is_recorded(self, wired):
        wired.agregarr.apply_home_order.side_effect = AgregarrError("Agregarr rejected the shelf order with HTTP 500")
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(wired, report)  # must not raise

        assert report.agregarr_mirrors[0]["ok"] is False
        assert "AgregarrError" in report.agregarr_mirrors[0]["error"]

    def test_a_plexapi_error_never_puts_a_plex_token_in_the_audit(self, wired):
        # `managedHubs()` is inside the guarded block, and plexapi error text embeds the whole
        # request URL — token and all. That string goes straight into an `events` row, so it has to
        # be redacted on the way (plex-safety rule 9).
        wired.delivery_sections[0].managedHubs.side_effect = RuntimeError(
            "BadRequest: http://plex:32400/hubs/sections/1/manage?X-Plex-Token=SUPERSECRETTOKEN"
        )
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(wired, report)

        error = report.agregarr_mirrors[0]["error"]
        assert "SUPERSECRETTOKEN" not in error
        assert "REDACTED" in error

    def test_one_library_failing_does_not_stop_the_next(self, wired):
        # A per-library failure (an unreadable Plex shelf) must not cost the other libraries their
        # mirror — only the shared read is all-or-nothing.
        second = MagicMock(type="show", key="2", title="TV Shows")
        second.managedHubs.return_value = [hub("tv.recentlyadded")]
        wired.delivery_sections[0].managedHubs.side_effect = RuntimeError("PMS said no")
        wired.delivery_sections = [wired.delivery_sections[0], second]
        wired.agregarr.home_items_by_library.return_value = {
            LIB: agregarr_items(row_sort_order=40),
            "2": [
                {
                    "id": "2:tv.recentlyadded",
                    "hubIdentifier": "tv.recentlyadded",
                    "libraryId": "2",
                    "sortOrderHome": 9,
                    "configType": "hub",
                }
            ],
        }
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(wired, report)

        assert [e["library"] for e in report.agregarr_mirrors] == ["Movies", "TV Shows"]
        assert report.agregarr_mirrors[0]["ok"] is False
        assert report.agregarr_mirrors[1]["ok"] is True

    def test_a_changed_library_records_the_order_before_and_after(self, wired):
        report = RunReport(started_at=datetime.now(UTC))

        _mirror_shelf_to_agregarr(wired, report)

        entry = report.agregarr_mirrors[0]
        # The prior order is agregarr's own — foreign row at key 2 sorting above our row at 40.
        assert entry["order_before"] == ["1:movie.recentlyadded", "cfg-900", "cfg-100"]
        assert entry["order_after"] == ["1:movie.recentlyadded", "cfg-100", "cfg-900"]
