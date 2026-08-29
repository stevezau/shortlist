from shortlist.engine.models import UserRunReport
from shortlist.server.services.run_persistence import _cost_blob


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
    names, and `docs/guides.md` sends owners looking for THIS one (`run.hub_unplaced`) after a
    nightly run. The jobs-path tests in `test_jobs.py` cannot see a regression here.
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
