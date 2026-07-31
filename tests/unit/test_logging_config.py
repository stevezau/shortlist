"""Unit tests for the shared loguru configuration + the log-level setting."""

from __future__ import annotations

import shortlist.logging_config as lc
from shortlist.logging_config import LOG_LEVELS, configure_logging, normalize_level


class TestNormalizeLevel:
    def test_accepts_every_known_level_case_insensitively(self):
        for level in LOG_LEVELS:
            assert normalize_level(level.lower()) == level

    def test_unknown_or_blank_falls_back_to_info(self):
        assert normalize_level("verbose") == "INFO"
        assert normalize_level("") == "INFO"
        assert normalize_level(None) == "INFO"

    def test_strips_and_uppercases(self):
        assert normalize_level("  debug ") == "DEBUG"


class TestConfigureLogging:
    def test_reconfigure_without_a_path_keeps_the_prior_file_sink(self, tmp_path):
        # A live level change (settings PUT) calls configure_logging(level) with no path — the boot
        # file sink must survive so the on-disk debug log isn't silently dropped.
        log_file = tmp_path / "shortlist.log"
        try:
            configure_logging("INFO", log_file=str(log_file))
            assert lc._log_file == str(log_file)
            configure_logging("DEBUG")  # no path — simulates a live level change
            assert lc._log_file == str(log_file)
        finally:
            # Leave loguru clean (stderr only) so a later test never writes to the tmp file sink.
            lc._log_file = None
            configure_logging("INFO")


def test_apscheduler_per_firing_chatter_is_silenced():
    """APScheduler logs TWO INFO lines for every firing of every job. The job-queue drain ticks on an
    interval, so at INFO that alone buried every real event in the operator's log — the maintainer hit
    exactly this in production. Real failures still surface: APScheduler logs a raising job at ERROR,
    and each Shortlist job records its own outcome to the `jobs` table."""
    import logging

    from shortlist.logging_config import configure_logging

    configure_logging("INFO")

    for name in ("apscheduler.executors.default", "apscheduler.scheduler"):
        assert logging.getLogger(name).level >= logging.WARNING, name
        # ERROR must still get through — silencing a failing job would be worse than the noise.
        assert logging.getLogger(name).isEnabledFor(logging.ERROR), name
