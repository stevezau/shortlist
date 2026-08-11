class TestARealFailureOutlivesAThresholdReason:
    """A queued title now always carries a reason, so the merge had to learn which one matters.

    Before: `row.detail = m.detail or row.detail` — so a title whose send genuinely errored
    ("Sonarr returned HTTP 503") had that overwritten the next night by "max_per_run (3) already
    filled", and the only record that Sonarr was broken was gone from the inbox and the trace.
    """

    def test_a_threshold_reason_does_not_erase_a_recorded_failure(self):
        from shortlist.server.services.run_persistence import _is_failure_detail

        assert _is_failure_detail("Sonarr GET /lookup returned HTTP 503") is True
        assert _is_failure_detail("max_per_run (3) already filled") is False
        assert _is_failure_detail("rating below auto_min_rating (7.5)") is False
        assert _is_failure_detail("auto-send is off") is False
        assert _is_failure_detail("on an Arr exclusion list") is False
        assert _is_failure_detail("") is False
        assert _is_failure_detail(None) is False
