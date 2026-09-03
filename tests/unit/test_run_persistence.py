from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import UserRunReport
from shortlist.server.db.models import Base
from shortlist.server.services.run_persistence import _cost_blob


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


class TestCostBlob:
    def test_seconds_become_integer_milliseconds(self):
        report = UserRunReport(username="alex", slug="alex")
        report.setup_s = 421.0
        report.row_timing = {"picked-for-you": {"duration_s": 12.04, "blocked_s": 0.31}}
        report.pool_costs = [
            {
                "label": "movie · tmdb, llm_web",
                "tokens": 15917,
                "exa_searches": 3,
                "duration_s": 398.0,
                "rows": ["picked-for-you"],
            }
        ]
        blob = _cost_blob(report)
        assert blob["setup_ms"] == 421000
        assert blob["rows"]["picked-for-you"] == {"duration_ms": 12040, "blocked_ms": 310}
        assert blob["pools"][0]["duration_ms"] == 398000
        assert blob["pools"][0]["tokens"] == 15917
        assert "duration_s" not in blob["pools"][0]

    def test_a_report_that_measured_nothing_persists_null(self):
        """Not `{}`: an empty blob would render as a real measurement of zero. A user who never
        reached the gather (no rows due) genuinely has nothing recorded."""
        assert _cost_blob(UserRunReport(username="alex", slug="alex")) is None


class TestARealFailureOutlivesAThresholdReason:
    """A queued title now always carries a reason, so the merge had to learn which one matters.

    Before: `row.detail = m.detail or row.detail` — so a title whose send genuinely errored
    ("Sonarr returned HTTP 503") had that overwritten the next night by "max_per_run (3) already
    filled", and the only record that Sonarr was broken was gone from the inbox and the trace.
    """

    def test_every_reason_the_engine_can_queue_is_classified_as_not_a_failure(self):
        """Drives the real engine through each blocking branch, so rewording a reason cannot silently
        reclassify it. Asserting substrings (the older tests do) would not catch that: "below
        auto_min_demand" still contains "auto_min_demand" while no longer matching the prefix."""
        from shortlist.engine.requests import QUEUE_REASON_PREFIXES
        from shortlist.server.services.run_persistence import _is_failure_detail

        for prefix in QUEUE_REASON_PREFIXES:
            assert _is_failure_detail(prefix) is False, prefix
            assert _is_failure_detail(f"{prefix} (3)") is False, prefix

    def test_a_threshold_reason_does_not_erase_a_recorded_failure(self):
        from shortlist.server.services.run_persistence import _is_failure_detail

        assert _is_failure_detail("Sonarr GET /lookup returned HTTP 503") is True
        assert _is_failure_detail("max_per_run (3) already filled") is False
        assert _is_failure_detail("rating below auto_min_rating (7.5)") is False
        assert _is_failure_detail("auto-send is off") is False
        assert _is_failure_detail("on an Arr exclusion list") is False
        assert _is_failure_detail("") is False
        assert _is_failure_detail(None) is False


class TestTheShelfEventsANightlyRunEmits:
    """`_emit_hub_ordering_events` — the RUN path, which `jobs._audit_hub_orderings` mirrors for the
    on-demand handlers.

    Tested separately from the jobs path because they are separate emitters with separate scope
    names, and `docs/guides.md` tells owners to read THIS one back after a nightly run
    (`/api/events/log?scope=run.hub_unplaced` — the change log has no screen yet). The jobs-path
    tests in `test_jobs.py` cannot see a regression here.
    """

    @staticmethod
    def _emit(entries: list[dict], *, dry_run: bool = False) -> list[tuple]:
        from types import SimpleNamespace
        from unittest.mock import patch

        from shortlist.server.services import run_persistence as rp

        seen: list[tuple] = []
        report = SimpleNamespace(hub_orderings=entries, dry_run=dry_run)
        with patch.object(rp, "add_audit", lambda session, scope, level, **f: seen.append((scope, level, f))):
            rp._emit_hub_ordering_events(None, 7, report)
        return seen

    def test_a_placement_that_could_not_be_applied_gets_its_own_scope_and_no_verified(self):
        """`verified` answers "we asked Plex and it stuck". Nothing was asked here, so answering it
        would be a fabrication — and the separate scope is what keeps `_shelf_contention`'s bounded
        window holding only the repeated moves it counts."""
        seen = self._emit([{"library": "Movies", "placed": False, "moved": [], "reason": "anchor not on the shelf"}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_unplaced", "warning")]
        fields = seen[0][2]
        assert fields["reason"] == "anchor not on the shelf" and fields["verified"] is None
        assert fields["library"] == "Movies" and fields["run_id"] == 7

    def test_a_move_still_uses_the_ordinary_scope(self):
        seen = self._emit([{"library": "Movies", "moved": ["Picked for You"], "verified": True}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_order", "info")]

    def test_an_unverified_move_is_a_warning(self):
        """A shelf we asked for and did not get — the SFLIX case the whole audit was rebuilt around."""
        seen = self._emit([{"library": "Movies", "moved": ["Picked for You"], "verified": False}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_order", "warning")]

    def test_a_dry_run_is_never_a_warning_on_either_scope(self):
        """A preview asked Plex for nothing, so neither kind is an alarm."""
        seen = self._emit(
            [
                {"library": "Movies", "placed": False, "moved": [], "reason": "anchor not found"},
                {"library": "TV", "moved": ["row"], "verified": False},
            ],
            dry_run=True,
        )

        assert [(a[0], a[1]) for a in seen] == [("run.hub_unplaced", "info"), ("run.hub_order", "info")]


class TestTheZeroRequestedEventSaysWhetherItWasReachable:
    """`_emit_request_events` — "0 requested" has two shapes and only one is about the owner's
    settings. `min_demand` counts DISTINCT wanters, so a run covering fewer people than the floor
    could never have filled the pool, whatever the settings were. Six such events on the
    maintainer's server (2026-09-03, every one a one-user manual run) raised "Nothing is being
    requested — loosen your floors" while the nightly 46-user run was requesting normally."""

    @staticmethod
    def _emit(*, users: int, demand_floor: int) -> dict:
        from types import SimpleNamespace
        from unittest.mock import patch

        from shortlist.engine.models import RequestReport
        from shortlist.server.services import run_persistence as rp

        seen: list[tuple] = []
        requests = RequestReport(wanted=650, pool_size=0, demand_floor=demand_floor)
        report = SimpleNamespace(
            requests=requests,
            dry_run=False,
            users=[UserRunReport(username=f"u{i}", slug=f"u{i}") for i in range(users)],
        )
        with patch.object(rp, "add_audit", lambda session, scope, level, **f: seen.append((scope, level, f))):
            rp._emit_request_events(None, 7, report)
        return next(f | {"_level": level} for scope, level, f in seen if scope == "requests.none_qualified")

    def test_a_run_smaller_than_its_own_demand_floor_is_info_and_flagged(self):
        fields = self._emit(users=1, demand_floor=2)

        assert fields["_level"] == "info", "arithmetically guaranteed, so not an alarm"
        assert fields["demand_unreachable"] is True
        assert (fields["users"], fields["demand_floor"]) == (1, 2)

    def test_a_full_roster_that_cleared_nothing_is_still_a_warning(self):
        """The shape the alert exists for: plenty of people, plenty missing, floors too tight."""
        fields = self._emit(users=46, demand_floor=2)

        assert fields["_level"] == "warning"
        assert fields["demand_unreachable"] is False

    def test_a_floor_of_one_is_never_unreachable(self):
        """The default. One person wanting a title is one wanter, so a single-user run clears it."""
        assert self._emit(users=1, demand_floor=1)["demand_unreachable"] is False


class TestPicksCarryTheBuiltAtStamp:
    """`built_at` has to survive the write as well as the read.

    The read back has a test (`test_previous_picks_carries_the_built_at_stamp`), but nothing
    exercised the WRITE: drop `built_at=pick.built_at` from `_persist_user_report` and every stamp is
    silently NULL, every carried row reads as "unknown", and the idle hold is inert on a real server
    with the whole suite green.
    """

    def test_a_persisted_pick_keeps_the_stamp_the_engine_put_on_it(self, sessions):
        from shortlist.engine.models import MediaType, Pick, UserRunReport
        from shortlist.server.db.models import PickRow, Run, User
        from shortlist.server.services.run_persistence import _persist_user_report

        built = datetime(2026, 8, 20, 3, 30, tzinfo=UTC)
        with sessions() as session:
            user = User(plex_account_id=1, username="sarah", slug="sarah", enabled=True)
            run = Run(trigger="manual", status="ok", dry_run=False, stats={})
            session.add_all([user, run])
            session.commit()
            report = UserRunReport(username="sarah", slug="sarah", status="ok")
            report.picks = [
                Pick(
                    tmdb_id=100,
                    rating_key=1,
                    title="T100",
                    rank=1,
                    reason="",
                    media_type=MediaType.MOVIE,
                    collection_slug="picked",
                    section_key="1",
                    built_at=built,
                )
            ]

            _persist_user_report(session, run.id, user, report, dry_run=False)
            session.commit()

            stored = session.query(PickRow).one()
            assert stored.built_at is not None, "the stamp was dropped on the way into the database"
            assert stored.built_at.replace(tzinfo=stored.built_at.tzinfo or UTC) == built
