"""The product decisions behind watch tracking, written as literals.

Every behavioural test of a threshold in this suite expresses the boundary IN TERMS OF the constant
— `MIN_START_SECONDS + 5`, `STREAM_DOWN_ALERT_MINUTES - 1`, `if pct >= FINISHED_PERCENT`. That is
the right way to test the LOGIC: the rule holds wherever the line is drawn, and the assertion moves
with the line. It is also why a mutation audit (2026-08-24) could shift all sixteen of these by a
step in either direction with the entire suite green. Not one of them is a free variable — each is a
claim the dashboard then makes to the owner about their server — and nothing was making the change
deliberate.

So this file is the other half: the values themselves, as literals, with what each one MEANS. It
catches exactly one thing, which is the thing that was missing — a threshold moving by accident.
Changing one on purpose means changing it here too, and that edit is the deliberate act.

Deliberately NOT parametrised over a table of (name, value): the point is that each line states a
product decision in prose a reader can disagree with. A table would restore the very indirection
this file exists to remove.
"""

from __future__ import annotations

import inspect

from shortlist.server.services.report_service import BOUNCE_PERCENT
from shortlist.server.services.run_persistence import (
    FINISHED_PERCENT,
    HIT_WINDOW_DAYS,
    UNWATCH_WITHDRAW_DAYS,
    WATCH_RETENTION_MONTHS,
)
from shortlist.server.services.watch_events import BACKFILL_DAYS
from shortlist.server.services.watch_stream import (
    FLUSH_EVERY,
    MIN_START_SECONDS,
    OVERSHOOT_TOLERANCE,
    SESSION_CACHE_TTL,
    SESSION_TIMEOUT,
    STABLE_AFTER_S,
    STREAM_DOWN_ALERT_MINUTES,
)


class TestWhatCountsAsWatching:
    def test_a_film_played_this_far_is_finished(self):
        """90%. Films carry credits and end titles, so the last tenth is not the story. Lower it and
        abandonments start reporting as completions; raise it and real completions file under
        "gave up part-way", which is the reading the dashboard draws in red."""
        assert FINISHED_PERCENT == 90

    def test_below_this_they_never_really_started(self):
        """5%. Separates "opened it and closed it" from "gave it a real go" — two things Plex's
        watched flag cannot tell apart at all, and which say opposite things about the pick."""
        assert BOUNCE_PERCENT == 5

    def test_a_play_shorter_than_this_is_a_mis_click(self):
        """60 seconds. Below it nothing is persisted at all, so it is the floor on the entire
        feature: raise it and genuine short starts vanish with no trace anywhere."""
        assert MIN_START_SECONDS == 60


class TestHowLongAPickHasToLand:
    def test_a_pick_is_judged_over_this_window(self):
        """30 days. The matured cohort the landing rate is computed over — a pick delivered more
        recently has not had its chance yet, so it is in neither numerator nor denominator."""
        assert HIT_WINDOW_DAYS == 30

    def test_a_credit_older_than_this_is_settled_history(self):
        """30 days, deliberately the same number. Past it an un-watch in Plex no longer withdraws
        the credit: someone re-watching and clearing a flag months later is not evidence the
        recommendation failed. Kept equal to the judging window so 'still being judged' and 'still
        withdrawable' cannot drift apart."""
        assert UNWATCH_WITHDRAW_DAYS == HIT_WINDOW_DAYS == 30

    def test_the_first_read_reaches_back_this_far(self):
        """90 days. How much play history a fresh install pulls before the cursor takes over. Larger
        means a longer first sync against the PMS; smaller means the first dashboard is emptier than
        the server really is."""
        assert BACKFILL_DAYS == 90

    def test_play_history_is_kept_this_long(self):
        """6 months. Beyond it events and sessions are pruned. This is the real bound on every
        recomputation: a credit can only ever be re-derived from evidence still inside it."""
        assert WATCH_RETENTION_MONTHS == 6


class TestTheLiveListener:
    def test_a_session_with_no_word_for_this_long_is_over(self):
        """5 minutes. Plex sends no "stopped" for a client that vanishes — a phone going into a
        tunnel, a TV losing power — so silence is the only signal that a session ended."""
        assert SESSION_TIMEOUT.total_seconds() == 300  # 5 minutes

    def test_progress_is_written_at_most_this_often(self):
        """Once a minute. The trade is crash-loss against write volume: the app can lose up to this
        much of an in-flight session's progress if it is killed."""
        assert FLUSH_EVERY.total_seconds() == 60  # 1 minute

    def test_the_sessions_snapshot_is_reused_for_this_long(self):
        """5 seconds. Short, because it is what maps a session to a PERSON — serve it too stale and
        a recycled session key resolves to the previous viewer."""
        assert SESSION_CACHE_TTL.total_seconds() == 5  # 5 seconds

    def test_the_stream_must_be_down_this_long_before_anyone_is_told(self):
        """45 minutes. Long enough that a PMS restart or a brief network blip stays quiet, short
        enough that a genuinely dead listener is reported the same evening."""
        assert STREAM_DOWN_ALERT_MINUTES == 45

    def test_reconnect_backoff_never_grows_past_two_minutes(self):
        """120 seconds. The listener doubles its wait after each failed connect; the cap is what
        bounds how long a recovered PMS goes unnoticed."""
        from shortlist.server.services import watch_stream

        source = inspect.getsource(watch_stream)
        assert "min(self._delay * 2, 120)" in source, "the reconnect backoff cap moved"

    def test_an_offset_past_the_runtime_is_tolerated_by_five_percent(self):
        """1.05. Plex reports offsets slightly past a file's stated duration (container padding,
        inaccurate metadata). Tighter and real completions get discarded as nonsense; looser and a
        genuinely broken offset is recorded as progress.

        A real constant now, asserted directly. It was a source grep, because the value was inline in
        one branch of `_on_playing` — which is the same fact that let the OPENING offset skip the check
        entirely (see `_is_overshoot`). Naming it made both the bug and this assertion straightforward.
        """
        assert OVERSHOOT_TOLERANCE == 1.05

    def test_a_connection_counts_as_stable_after_this_long(self):
        """60 seconds. Below it a flapping connection — connect, drop, reconnect — would clear the
        outage clock on every cycle and an alert would never fire at all."""
        assert STABLE_AFTER_S == 60
