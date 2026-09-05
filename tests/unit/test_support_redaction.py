"""`shortlist/server/api/support.py`: no probe failure ever writes a credential to the log.

plex-safety rule 9 — tokens are "never logged, never in exception messages". The support module
already scrubs every failure it puts in a RESPONSE, through the single `_fail` choke point. The log
line beside it did not go through that choke point, and the rotating file sink under /config/logs is
always DEBUG, so a plexapi error (which embeds `X-Plex-Token` in its message — see
`pipeline.py`'s note) wrote a live Plex token to disk on every failed health probe.
"""

from __future__ import annotations

from loguru import logger

from shortlist.server.api import support

_TOKEN = "sxABC123secretplextoken"


class TestProbeFailuresAreScrubbedBeforeLogging:
    def _capture(self, fn) -> tuple[dict, str]:
        lines: list[str] = []
        sink = logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            result = support._check("plex", fn)
        finally:
            logger.remove(sink)
        return result, "\n".join(lines)

    def test_a_token_in_the_exception_never_reaches_the_log(self):
        def boom():
            raise RuntimeError(f"BadRequest: http://pms:32400/library?X-Plex-Token={_TOKEN}")

        result, logged = self._capture(boom)

        assert result["ok"] is False
        assert _TOKEN not in logged, "a live Plex token was written to the log"
        assert _TOKEN not in result["detail"], "a live Plex token was returned in the response"

    def test_the_failure_is_still_reported_usefully(self):
        """Scrubbing must not turn a diagnosable failure into a blank — the point of this page is to
        load and explain itself when something is broken."""

        def boom():
            raise RuntimeError(f"BadRequest: unauthorised X-Plex-Token={_TOKEN}")

        result, logged = self._capture(boom)

        assert "RuntimeError" in result["detail"]
        assert "plex" in logged, "the probe name is what makes the log line actionable"

    def test_a_probe_with_no_secret_logs_normally(self):
        def boom():
            raise ValueError("connection refused")

        result, logged = self._capture(boom)

        assert "connection refused" in result["detail"]
        assert "connection refused" in logged

    def test_a_passing_probe_is_unchanged(self):
        result, _logged = self._capture(lambda: (True, "PMS 1.43.3"))

        assert result == {"name": "plex", "ok": True, "detail": "PMS 1.43.3"}
