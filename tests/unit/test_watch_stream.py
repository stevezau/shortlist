"""The PMS notification socket — assembling anonymous position updates into sessions.

The event Plex pushes is thin: a session key, a rating key, an offset, a state. No user, no runtime.
Everything here is about what has to be added to that, and the failure modes measured on a live
server before the code was written (see the module docstring for the capture).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import shortlist.server.services.watch_stream as watch_stream
from shortlist.server.db.models import Base, WatchSession
from shortlist.server.services.watch_stream import MIN_START_SECONDS, WatchStream


@pytest.fixture
def sessions():
    # StaticPool, because the persistence path runs in a worker thread (`asyncio.to_thread`) and
    # SQLite's default pooling hands a new thread its OWN connection — which for `sqlite://` means its
    # own empty in-memory database. Without this the writes land somewhere nothing can read.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


@pytest.fixture
def ctx():
    c = MagicMock()
    c.plex.active_sessions.return_value = {
        "556": {
            "account_id": 99,
            "rating_key": 456294,
            "show_rating_key": None,
            "media_type": "movie",
            "duration_ms": 6_000_000,
            "state": "playing",
        }
    }
    return c


def playing(session_key: str = "556", *, offset: int, state: str = "playing") -> dict:
    return {"sessionKey": session_key, "ratingKey": 456294, "viewOffset": offset, "state": state}


def run(coro):
    return asyncio.run(coro)


class TestIdentity:
    def test_a_session_is_resolved_to_an_account_from_status_sessions(self, sessions, ctx):
        """The event carries no user at all — this read is the only thing that can say who it was."""
        stream = WatchStream(sessions, MagicMock())

        run(stream._on_playing(ctx, playing(offset=1000)))

        assert stream._live["556"].account_id == 99
        assert stream._live["556"].duration_ms == 6_000_000

    def test_a_session_that_cannot_be_resolved_is_dropped_not_guessed(self, sessions, ctx):
        ctx.plex.active_sessions.return_value = {}
        stream = WatchStream(sessions, MagicMock())

        run(stream._on_playing(ctx, playing(offset=1000)))

        assert stream._live == {}

    def test_a_session_first_seen_as_stopped_is_ignored(self, sessions, ctx):
        """Measured live: a session whose only observed state was `stopped` had already left
        `/status/sessions`, so there was nothing left to identify it against. We only ever needed the
        start, so dropping it is correct rather than a loss."""
        stream = WatchStream(sessions, MagicMock())

        run(stream._on_playing(ctx, playing(offset=1000, state="stopped")))

        assert stream._live == {}
        ctx.plex.active_sessions.assert_not_called()

    def test_status_sessions_is_read_once_for_a_burst_of_new_sessions(self, sessions, ctx):
        """One read answers every session on the server, so a burst costs one call rather than one
        each — at ~1 event/second server-wide that difference is the whole load."""
        ctx.plex.active_sessions.return_value |= {
            "557": {
                "account_id": 88,
                "rating_key": 12,
                "show_rating_key": None,
                "media_type": "movie",
                "duration_ms": 100,
                "state": "playing",
            }
        }
        stream = WatchStream(sessions, MagicMock())

        run(stream._on_playing(ctx, playing("556", offset=1)))
        run(stream._on_playing(ctx, playing("557", offset=1)))

        assert ctx.plex.active_sessions.call_count == 1


class TestProgress:
    def test_the_furthest_point_is_kept_not_the_latest(self, sessions, ctx):
        """Someone who scrubs backwards has still watched the further point. Taking the last offset
        would report them as less engaged than they were."""
        stream = WatchStream(sessions, MagicMock())

        run(stream._on_playing(ctx, playing(offset=1000)))
        run(stream._on_playing(ctx, playing(offset=900_000)))
        run(stream._on_playing(ctx, playing(offset=5000)))

        assert stream._live["556"].max_offset_ms == 900_000

    def test_progress_is_not_written_on_every_event(self, sessions, ctx):
        """Events arrive about every 10s per session, ~1/second across the server. A write per event
        would be a write per second to record that ten seconds passed."""
        stream = WatchStream(sessions, MagicMock())

        for offset in range(1000, 6000, 1000):
            run(stream._on_playing(ctx, playing(offset=offset)))

        with sessions() as s:
            assert s.query(WatchSession).count() == 0, "still in memory, nothing flushed yet"


class TestClosing:
    def test_a_stopped_session_is_persisted_and_closed(self, sessions, ctx):
        stream = WatchStream(sessions, MagicMock())
        run(stream._on_playing(ctx, playing(offset=1000)))
        stream._live["556"].started_at -= timedelta(minutes=30)

        run(stream._on_playing(ctx, playing(offset=3_000_000, state="stopped")))

        with sessions() as s:
            row = s.query(WatchSession).one()
        assert row.end_reason == "stopped"
        assert row.ended_at is not None
        assert row.max_offset_ms == 3_000_000
        assert row.session_key == "556", "the session key must survive to the row"
        assert stream._live == {}

    def test_a_session_that_goes_quiet_is_closed_as_a_timeout_not_a_stop(self, sessions, ctx):
        """A client that crashes or drops off the network never sends `stopped`. Tautulli schedules a
        force-stop for the same reason. `end_reason` records which of the two actually happened."""
        stream = WatchStream(sessions, MagicMock())
        run(stream._on_playing(ctx, playing(offset=1000)))
        live = stream._live["556"]
        live.started_at -= timedelta(minutes=40)
        live.last_seen_at -= timedelta(minutes=30)

        run(stream._housekeep(ctx))

        with sessions() as s:
            row = s.query(WatchSession).one()
        assert row.end_reason == "timeout"
        assert stream._live == {}

    def test_a_misfire_shorter_than_the_floor_is_discarded(self, sessions, ctx):
        """A mis-click, an autoplay preview, a client probing the file. Recording those as "they
        started it" would turn noise into a signal the report then reasons about."""
        stream = WatchStream(sessions, MagicMock())
        run(stream._on_playing(ctx, playing(offset=500)))

        run(stream._on_playing(ctx, playing(offset=900, state="stopped")))

        with sessions() as s:
            assert s.query(WatchSession).count() == 0

    def test_a_real_watch_past_the_floor_is_kept(self, sessions, ctx):
        stream = WatchStream(sessions, MagicMock())
        run(stream._on_playing(ctx, playing(offset=500)))
        stream._live["556"].started_at -= timedelta(seconds=MIN_START_SECONDS + 5)

        run(stream._on_playing(ctx, playing(offset=400_000, state="stopped")))

        with sessions() as s:
            assert s.query(WatchSession).count() == 1


class TestPercent:
    def test_percent_is_computed_from_offset_and_runtime(self, sessions):
        row = WatchSession(max_offset_ms=1_294_833, duration_ms=6_000_000)
        assert row.percent == 22

    def test_percent_is_none_when_the_runtime_is_unknown(self, sessions):
        """A percentage of an unknown runtime is worse than none — it would read as 0%, which is a
        claim we cannot make."""
        assert WatchSession(max_offset_ms=500, duration_ms=None).percent is None

    def test_an_offset_past_the_runtime_caps_at_100(self, sessions):
        """Observed live on an on-deck item reading 145% of its duration; a re-scan or a bulk mark can
        leave the offset past the end."""
        assert WatchSession(max_offset_ms=9_000_000, duration_ms=6_000_000).percent == 100


class TestMessageHandling:
    def test_non_playing_notifications_are_ignored(self, sessions, ctx):
        """The socket carries transcode, activity and progress traffic too — 13 transcode updates and
        13 activity events in the same 20 seconds as 19 playing events."""
        stream = WatchStream(sessions, MagicMock())

        run(stream._on_message(ctx, '{"NotificationContainer": {"type": "transcodeSession.update"}}'))

        assert stream._live == {}

    def test_malformed_frames_do_not_break_the_listener(self, sessions, ctx):
        stream = WatchStream(sessions, MagicMock())

        run(stream._on_message(ctx, "not json at all"))

        assert stream._live == {}


class TestOrphanedSessions:
    def test_sessions_left_open_by_a_restart_are_closed_at_startup(self, sessions):
        """Live sessions live in memory. A restart empties that and leaves their rows reading as
        "still playing" for ever, because the socket only ever reports sessions that are still going —
        nothing else would close them."""
        started = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
        last_seen = started + timedelta(minutes=40)
        with sessions() as s:
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=10,
                    media_type="movie",
                    started_at=started,
                    last_seen_at=last_seen,
                    max_offset_ms=1000,
                    duration_ms=6000,
                )
            )
            s.commit()

        assert WatchStream(sessions, MagicMock()).close_orphans() == 1

        with sessions() as s:
            row = s.query(WatchSession).one()
        assert row.end_reason == "timeout"
        # Compared naive: SQLite stores no offset, so everything read back is naive UTC. That is the
        # whole codebase's convention, not a quirk of this test.
        assert row.ended_at == last_seen.replace(tzinfo=None), "ended when we last SAW it, not when we noticed"

    def test_already_closed_sessions_are_left_alone(self, sessions):
        ended = datetime(2026, 8, 20, 21, 0, tzinfo=UTC)
        with sessions() as s:
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=10,
                    media_type="movie",
                    started_at=ended - timedelta(hours=1),
                    last_seen_at=ended,
                    ended_at=ended,
                    end_reason="stopped",
                    max_offset_ms=1000,
                    duration_ms=6000,
                )
            )
            s.commit()

        assert WatchStream(sessions, MagicMock()).close_orphans() == 0

        with sessions() as s:
            assert s.query(WatchSession).one().end_reason == "stopped"


class TestImplausibleOffsets:
    """An offset past the end of the item is a reading that belongs to something else.

    Found by cross-checking a night of real captures against Tautulli, which watched the same socket:
    47 of 50 (person, title) pairs agreed within 3 points, and all three that did not were ours
    reading too high — one at 119%, an offset eight minutes past the end of the episode, on the same
    `sessionKey` as a sane 89% reading. Plex and Tautulli agreed on the runtime, so it was the offset
    that was wrong: auto-play moves it to the next item before `ratingKey` catches up.
    """

    def test_an_offset_past_the_runtime_is_not_recorded(self, sessions, ctx):
        stream = WatchStream(sessions, MagicMock())
        run(stream._on_playing(ctx, playing(offset=3_000_000)))  # half of a 6,000,000ms film

        run(stream._on_playing(ctx, playing(offset=7_500_000)))  # 125% — impossible

        assert stream._live["556"].max_offset_ms == 3_000_000

    def test_a_small_overshoot_at_the_end_is_still_accepted(self, sessions, ctx):
        """Genuine end-of-file overshoot happens; the guard must not reject a real completion."""
        stream = WatchStream(sessions, MagicMock())
        run(stream._on_playing(ctx, playing(offset=1000)))

        run(stream._on_playing(ctx, playing(offset=6_100_000)))  # ~102%

        assert stream._live["556"].max_offset_ms == 6_100_000

    def test_a_bad_reading_is_dropped_rather_than_clamped_to_finished(self, sessions, ctx):
        """Clamping would turn the bad reading into the strongest claim the report can make."""
        stream = WatchStream(sessions, MagicMock())
        run(stream._on_playing(ctx, playing(offset=5_340_000)))  # 89%, the truth

        run(stream._on_playing(ctx, playing(offset=7_140_000)))  # 119%, the bad reading
        live = stream._live["556"]

        assert round(100 * live.max_offset_ms / live.duration_ms) == 89


class FakeSocket:
    """A stand-in for the websocket — the BOUNDARY, not one of our own helpers.

    Every defect in the connection lifecycle lived here and none of it was reachable: the whole suite
    called the handlers directly, so the reconnect loop, the backoff reset, the boot sweep and the
    shutdown path had no coverage at all. `test_malformed_frames_do_not_break_the_listener` was even
    named for a property it never tested — it called `_on_message`, and a bad frame did break the
    listener.
    """

    def __init__(self, frames=(), fail_after=None):
        self._frames = list(frames)
        self._fail_after = fail_after
        self.sent = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        if self._fail_after is not None and self.sent >= self._fail_after:
            raise ConnectionError("socket dropped")
        if self._frames:
            self.sent += 1
            return self._frames.pop(0)
        await asyncio.sleep(3600)  # idle, like a real quiet server


async def until(predicate, *, timeout: float = 5.0) -> None:
    """Wait for a CONDITION, never a duration.

    These scenarios used a flat `sleep(0.05)` to let `run()` get as far as connecting. That is a race
    the moment anything in the startup path gets slower — and it did: `close_orphans`, the context
    build and the health write all hop to the listener's single-thread pool, so under parallel test
    load fifty milliseconds stopped being enough and `test_connecting_resets_the_backoff` failed about
    two runs in three. Polling the condition is both faster in the common case and immune to it.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


def connect_returning(*sockets):
    """Monkeypatch target for `websockets.connect`, handing out each socket in turn."""
    queue = list(sockets)
    calls = {"n": 0}

    def _connect(*_a, **_k):
        calls["n"] += 1
        return queue.pop(0) if queue else FakeSocket()

    _connect.calls = calls
    return _connect


class TestConnectionLifecycle:
    def test_a_failing_orphan_sweep_does_not_kill_the_listener(self, sessions, monkeypatch):
        """The one unguarded write at boot. A restart normally leaves orphans to close, so it commits
        — and a commit at boot races the other startup writers for SQLite's single writer slot. It
        used to exit `run()` before the loop was ever entered: no reconnect, no retry, no log line."""
        stream = WatchStream(sessions, lambda **kw: MagicMock())
        monkeypatch.setattr(stream, "close_orphans", MagicMock(side_effect=OSError("database is locked")))
        socket = FakeSocket()
        connect = connect_returning(socket)
        monkeypatch.setattr(watch_stream.websockets, "connect", connect)

        async def scenario():
            task = asyncio.ensure_future(stream.run())
            await until(lambda: connect.calls["n"] > 0)
            stream.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())  # must not raise

    def test_a_dropped_socket_reconnects_rather_than_ending_the_listener(self, sessions, monkeypatch):
        stream = WatchStream(sessions, lambda **kw: MagicMock())
        connect = connect_returning(FakeSocket(fail_after=0), FakeSocket())
        monkeypatch.setattr(watch_stream.websockets, "connect", connect)
        monkeypatch.setattr(watch_stream, "logger", MagicMock())

        # The real backoff waits 5s before retrying, which is correct in production and useless in a
        # test — patched so the reconnect itself is what is being observed, not the delay.
        async def instant(_seconds):
            await asyncio.sleep(0)

        monkeypatch.setattr(stream, "_sleep", instant)

        async def scenario():
            task = asyncio.ensure_future(stream.run())
            await asyncio.sleep(0.2)
            stream.stop()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())

        assert connect.calls["n"] >= 2, "it reconnected instead of giving up"

    def test_stop_returns_promptly_on_an_idle_socket(self, sessions, monkeypatch):
        """`recv()` is raced against the stop event, not waited on with a timeout. Parked in a 30s
        read on a quiet server — which is exactly when nobody is playing and a restart is likely —
        `run()` took up to 30s to notice, while the lifespan allows 5, so the graceful close never
        happened and every restart left orphans behind."""
        stream = WatchStream(sessions, lambda **kw: MagicMock())
        connect = connect_returning(FakeSocket())
        monkeypatch.setattr(watch_stream.websockets, "connect", connect)

        async def scenario():
            task = asyncio.ensure_future(stream.run())
            await until(lambda: connect.calls["n"] > 0)
            stream.stop()
            await asyncio.wait_for(task, timeout=2)  # 30s recv would blow this

        asyncio.run(scenario())

    def test_connecting_resets_the_backoff(self, sessions, monkeypatch):
        """`delay` used to be reset where `_listen` RETURNS, which only happens at shutdown — so every
        real drop doubled it, for the life of the process, up to two minutes per blip."""
        stream = WatchStream(sessions, lambda **kw: MagicMock())
        stream._delay = 60
        monkeypatch.setattr(watch_stream.websockets, "connect", connect_returning(FakeSocket()))

        async def scenario():
            task = asyncio.ensure_future(stream.run())
            await until(lambda: stream._delay == 5)
            stream.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

        assert stream._delay == 5

    def test_a_malformed_frame_really_does_not_break_the_listener(self, sessions, monkeypatch, ctx):
        """Through the socket this time. `int(event["ratingKey"])` on a path-shaped value raised out
        to `run()`, which treated it as a dropped socket, reconnected, and closed every live session
        as `replaced` — one client's odd frame fragmenting everybody's watch."""
        bad = json.dumps(
            {
                "NotificationContainer": {
                    "type": "playing",
                    "PlaySessionStateNotification": [
                        {"sessionKey": "1", "ratingKey": "/library/metadata/1", "viewOffset": 5, "state": "playing"}
                    ],
                }
            }
        )
        stream = WatchStream(sessions, lambda **kw: ctx)
        socket = FakeSocket([bad])
        connect = connect_returning(socket)
        monkeypatch.setattr(watch_stream.websockets, "connect", connect)

        async def scenario():
            task = asyncio.ensure_future(stream.run())
            await asyncio.sleep(0.15)
            stream.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

        assert connect.calls["n"] == 1, "a bad frame must not cause a reconnect"


class TestCloseAllIsAllOrNothingInMemory:
    def test_one_failing_write_does_not_leave_other_sessions_tracked(self, sessions, ctx, monkeypatch):
        """A busy database is the condition most likely to coincide with a reconnect, so an early
        abort here defeated the guard exactly when it mattered — survivors then absorb a new play's
        offsets under a reused key."""
        stream = WatchStream(sessions, MagicMock())
        ctx.plex.active_sessions.return_value |= {
            "557": {
                "account_id": 88,
                "rating_key": 12,
                "show_rating_key": None,
                "media_type": "movie",
                "duration_ms": 100,
                "state": "playing",
            }
        }
        run(stream._on_playing(ctx, playing("556", offset=1)))
        run(stream._on_playing(ctx, playing("557", offset=1)))
        for live in stream._live.values():
            live.started_at -= timedelta(minutes=5)
        monkeypatch.setattr(stream, "_flush", MagicMock(side_effect=OSError("database is locked")))

        stream._close_all("replaced")

        assert stream._live == {}, "memory state must be correct even when persistence is not"


class TestTheOwnerIsToldWhenTrackingIsOffline:
    """A dropped socket is normal and self-healing, so nothing escalates a blip. But a listener that
    is quietly dead stops the ONE signal Plex cannot give us — partial watches — while every number on
    the dashboard still looks plausible. "Nobody abandoned anything this week" reads as a healthy week.
    """

    def _store(self, sessions):
        from shortlist.server.settings_store import SettingsStore

        return sessions, SettingsStore

    def test_a_healthy_listener_raises_no_alert(self, sessions):
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.settings_store import SettingsStore

        with sessions() as s:
            assert _playback_listener_down(SettingsStore(s)) is None

    def test_a_brief_outage_raises_no_alert(self, sessions):
        """A container recreate takes seconds and a Plex restart a minute or two. Alerting on those
        would train the owner to ignore the bell."""
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY
        from shortlist.server.settings_store import SettingsStore

        with sessions() as s:
            SettingsStore(s).set(STREAM_DOWN_SINCE_KEY, (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
            s.commit()
        with sessions() as s:
            assert _playback_listener_down(SettingsStore(s)) is None

    def test_a_long_outage_raises_a_warning_that_cannot_be_dismissed(self, sessions):
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY
        from shortlist.server.settings_store import SettingsStore

        with sessions() as s:
            SettingsStore(s).set(STREAM_DOWN_SINCE_KEY, (datetime.now(UTC) - timedelta(hours=6)).isoformat())
            s.commit()
        # Through `build_notifications`, the real entry point — asserting on the builder alone passes
        # even when it is never wired into the list the bell renders.
        import shortlist
        from shortlist.server.notifications import build_notifications

        with sessions() as s:
            store = SettingsStore(s)
            alert = next(
                (n for n in build_notifications(s, store, shortlist.__version__) if "playback" in n["id"]), None
            )
        assert alert is not None, "the bell must actually carry it"
        assert alert["severity"] == "warning"
        assert alert["dismissable"] is False, "silencing it would hide a feature that has stopped"
        assert "6 hours" in alert["body"]
        assert _playback_listener_down(SettingsStore(s)) is not None

    def test_connecting_clears_the_outage(self, sessions):
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        with sessions() as s:
            SettingsStore(s).set(STREAM_DOWN_SINCE_KEY, (datetime.now(UTC) - timedelta(hours=6)).isoformat())
            s.commit()

        asyncio.run(WatchStream(sessions, lambda **_: None)._mark_connected())

        import shortlist
        from shortlist.server.notifications import build_notifications

        with sessions() as s:
            assert _playback_listener_down(SettingsStore(s)) is None
            assert not [
                n for n in build_notifications(s, SettingsStore(s), shortlist.__version__) if "playback" in n["id"]
            ]

    def test_the_outage_is_dated_from_its_START_not_the_latest_retry(self, sessions):
        """The retry loop fires every few seconds. Re-stamping on each one would reset the age and the
        alert could never cross its threshold, however long the outage ran."""
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        stream = WatchStream(sessions, lambda **_: None)
        stream._write_health(connected=False)
        with sessions() as s:
            first = SettingsStore(s).get(STREAM_DOWN_SINCE_KEY)
        for _ in range(3):
            stream._write_health(connected=False)

        with sessions() as s:
            assert SettingsStore(s).get(STREAM_DOWN_SINCE_KEY) == first

    def test_health_reporting_cannot_take_down_the_listener(self, sessions):
        """It runs inside the reconnect loop. A failure here must never be what stops the retries."""
        from shortlist.server.services.watch_stream import WatchStream

        def boom():
            raise RuntimeError("db is gone")

        stream = WatchStream(boom, lambda **_: None)
        stream._write_health(connected=False)  # must not raise
        asyncio.run(stream._mark_connected())  # must not raise


class TestAnUnreachablePlexIsAnOutageToo:
    """`build_plex_only` reaches the PMS (`plex.machine_id`), so a CONFIGURED-but-unreachable server
    raises in the same place as a never-configured one. Treated alike, the single case the alert
    exists for was the one case that never alerted."""

    def test_a_configured_but_unreachable_plex_marks_the_listener_down(self, sessions):
        # Written as raw rows, the way a box-less caller must: `plex.token` is a SECRET key and
        # `SettingsStore` without a `SecretBox` raises on it rather than returning a value. That is
        # exactly why `_plex_is_configured` asks whether the rows EXIST instead of reading them — an
        # earlier version went through the store, threw on every call, and silently answered False.
        from shortlist.server.db.models import Setting
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        with sessions() as s:
            s.add(Setting(key="plex.url", value={"v": "http://plex.local:32400"}))
            s.add(Setting(key="plex.token", value={"v": "encrypted"}))
            s.commit()

        stream = WatchStream(sessions, lambda **_: None)
        assert stream._plex_is_configured() is True
        stream._write_health(connected=False)

        with sessions() as s:
            assert SettingsStore(s).get(STREAM_DOWN_SINCE_KEY), "a reachable-server outage must be recorded"

    def test_an_install_that_never_finished_setup_is_not_an_outage(self, sessions):
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        assert stream._plex_is_configured() is False, "no url/token — nothing to be down"

    def test_a_half_finished_setup_is_not_an_outage(self, sessions):
        from shortlist.server.db.models import Setting
        from shortlist.server.services.watch_stream import WatchStream

        with sessions() as s:
            s.add(Setting(key="plex.url", value={"v": "http://plex.local:32400"}))  # token missing
            s.commit()
        assert WatchStream(sessions, lambda **_: None)._plex_is_configured() is False

    def test_a_database_failure_does_not_raise_an_alert(self, sessions):
        """Cannot tell — stay quiet rather than alert on a database blip."""
        from shortlist.server.services.watch_stream import WatchStream

        def boom():
            raise RuntimeError("db gone")

        assert WatchStream(boom, lambda **_: None)._plex_is_configured() is False


class TestTheReconnectLoopActuallyRecordsTheOutage:
    """Driving `run()`, not `_write_health` directly. Asserting on the writer alone passes even when
    the loop never calls it — which is the shape of half the fake tests found in this feature already.
    """

    def _configured(self, sessions):
        from shortlist.server.db.models import Setting

        with sessions() as s:
            s.add(Setting(key="plex.url", value={"v": "http://plex.local:32400"}))
            s.add(Setting(key="plex.token", value={"v": "encrypted"}))
            s.commit()

    def _run_one_pass(self, stream):
        """One trip round the loop, then stop — `run()` otherwise reconnects for ever."""

        async def stop_instead_of_sleeping(_seconds):
            stream._stop.set()

        stream._sleep = stop_instead_of_sleeping
        asyncio.run(stream.run())

    def test_a_context_failure_on_a_configured_server_is_recorded(self, sessions):
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        self._configured(sessions)

        def unreachable(**_):
            raise OSError("connection refused")

        self._run_one_pass(WatchStream(sessions, unreachable))

        with sessions() as s:
            assert SettingsStore(s).get(STREAM_DOWN_SINCE_KEY), "the loop must record what it could not do"

    def test_a_context_failure_before_setup_is_not_recorded(self, sessions):
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        def not_set_up(**_):
            raise RuntimeError("Plex connection is not configured yet — finish setup first")

        self._run_one_pass(WatchStream(sessions, not_set_up))

        with sessions() as s:
            assert not SettingsStore(s).get(STREAM_DOWN_SINCE_KEY), "nothing to be down before setup"


class TestHealthWritesStayOffTheEventLoop:
    """This listener has its own single-thread pool so that a DB write can never park the socket
    reader. A SQLite write here can wait up to five seconds on `busy_timeout` — doing that inline
    would make the health reporting a cause of the missed playback it reports on."""

    def test_the_connect_write_runs_on_the_pool_thread(self, sessions):
        import threading

        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        seen: dict[str, str] = {}
        real = stream._write_health

        def record(**kw):
            seen["thread"] = threading.current_thread().name
            return real(**kw)

        stream._write_health = record

        async def drive():
            seen["loop"] = threading.current_thread().name
            await stream._mark_connected()

        asyncio.run(drive())

        assert seen["thread"].startswith("watch-stream"), f"ran on {seen['thread']}"
        assert seen["thread"] != seen["loop"], "the write must not be on the loop's thread"

    def test_the_backoff_reset_is_immediate_and_not_deferred(self, sessions):
        """`_delay` is a field assignment and must take effect before the next drop, so it stays
        inline even though the write beside it does not."""
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        stream._delay = 120
        asyncio.run(stream._mark_connected())
        assert stream._delay == 5


class TestAFlappingSocketStillRaisesTheAlarm:
    """The health signal is "the handshake succeeded", not "frames are arriving", and those come
    apart. A PMS under memory pressure — or a proxy with a short idle timeout — ACCEPTS the socket and
    drops it seconds later. Every such cycle cleared `down_since` and re-stamped it at now, and since
    the backoff caps at 120s every cycle is shorter than the 45-minute threshold: the clock could
    never run out, and a server flapping all night looked perfectly healthy."""

    def test_repeated_connect_then_immediate_drop_keeps_the_original_outage(self, sessions):
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        stream = WatchStream(sessions, lambda **_: None)
        stream._write_health(connected=False)  # the outage begins
        with sessions() as s:
            began = SettingsStore(s).get(STREAM_DOWN_SINCE_KEY)

        for _ in range(4):  # connect ... drop ... connect ... drop
            asyncio.run(stream._mark_connected())
            asyncio.run(stream._mark_dropped())

        with sessions() as s:
            assert SettingsStore(s).get(STREAM_DOWN_SINCE_KEY) == began, "a flap is not a recovery"

    def test_a_connection_that_survives_is_a_real_recovery(self, sessions):
        from shortlist.server.services.watch_stream import STABLE_AFTER_S, STREAM_DOWN_SINCE_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        stream = WatchStream(sessions, lambda **_: None)
        stream._write_health(connected=False)
        asyncio.run(stream._mark_connected())
        # It has been up comfortably longer than the stability window.
        stream._up_since = datetime.now(UTC) - timedelta(seconds=STABLE_AFTER_S + 30)
        asyncio.run(stream._mark_dropped())

        with sessions() as s:
            down = SettingsStore(s).get(STREAM_DOWN_SINCE_KEY)
        assert down, "the drop still starts an outage"
        began = datetime.fromisoformat(str(down))
        assert (datetime.now(UTC) - began).total_seconds() < 5, "but a NEW one, dated now"


class TestTheAlertSaysHowLongInPlainEnglish:
    """Copy the owner reads. The unit was picked from the raw value and the number rounded
    independently, so 60-89 minutes rendered "1 hours" and 59.6 rendered "60 minutes"."""

    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [
            (45, "45 minutes"),
            (59.6, "an hour"),
            (60, "an hour"),
            (89, "an hour"),
            (90, "2 hours"),
            (1440, "a day"),
            (4300, "3 days"),
        ],
    )
    def test_it_reads_as_english(self, minutes, expected):
        from shortlist.server.notifications import _spell_duration

        assert _spell_duration(minutes) == expected

    def test_the_alert_body_carries_it(self, sessions):
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY
        from shortlist.server.settings_store import SettingsStore

        with sessions() as s:
            SettingsStore(s).set(STREAM_DOWN_SINCE_KEY, (datetime.now(UTC) - timedelta(minutes=70)).isoformat())
            s.commit()
        with sessions() as s:
            assert "for an hour." in _playback_listener_down(SettingsStore(s))["body"]


class TestTheAlertSurvivesBadStoredValues:
    """It runs on every bell poll. A parse error here would take out every OTHER notification too."""

    def test_a_garbage_timestamp_raises_nothing(self, sessions):
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY
        from shortlist.server.settings_store import SettingsStore

        with sessions() as s:
            SettingsStore(s).set(STREAM_DOWN_SINCE_KEY, "not a timestamp")
            s.commit()
        with sessions() as s:
            assert _playback_listener_down(SettingsStore(s)) is None

    def test_a_naive_timestamp_is_read_as_utc(self, sessions):
        """Every timestamp in this database is UTC; one written without a tzinfo must not be compared
        as though it were local, which on a +10 server would read six hours as sixteen."""
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.services.watch_stream import STREAM_DOWN_SINCE_KEY
        from shortlist.server.settings_store import SettingsStore

        naive = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None)
        with sessions() as s:
            SettingsStore(s).set(STREAM_DOWN_SINCE_KEY, naive.isoformat())
            s.commit()
        with sessions() as s:
            assert "2 hours" in _playback_listener_down(SettingsStore(s))["body"]

    def test_the_threshold_boundary(self, sessions):
        from shortlist.server.notifications import _playback_listener_down
        from shortlist.server.services.watch_stream import STREAM_DOWN_ALERT_MINUTES, STREAM_DOWN_SINCE_KEY
        from shortlist.server.settings_store import SettingsStore

        for minutes, fires in ((STREAM_DOWN_ALERT_MINUTES - 1, False), (STREAM_DOWN_ALERT_MINUTES + 1, True)):
            with sessions() as s:
                SettingsStore(s).set(
                    STREAM_DOWN_SINCE_KEY, (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
                )
                s.commit()
            with sessions() as s:
                assert (_playback_listener_down(SettingsStore(s)) is not None) is fires, f"at {minutes} minutes"


class TestTheDiagnosticBreadcrumb:
    def test_connecting_records_when_it_last_worked(self, sessions):
        """`watch.stream_connected_at` answers "when did this last work" from the settings table once
        the log has rotated past the answer. It is the only record of that, so it is kept — and a key
        that is written but never asserted is a key that can be deleted without anything noticing."""
        from shortlist.server.services.watch_stream import STREAM_CONNECTED_KEY, WatchStream
        from shortlist.server.settings_store import SettingsStore

        asyncio.run(WatchStream(sessions, lambda **_: None)._mark_connected())

        with sessions() as s:
            recorded = SettingsStore(s).get(STREAM_CONNECTED_KEY)
        assert recorded, "nothing else records when the socket last came up"
        assert (datetime.now(UTC) - datetime.fromisoformat(str(recorded))).total_seconds() < 5

    def test_it_is_not_writable_through_the_settings_api(self, sessions):
        """It raises a NON-dismissable alert, so a settings write that could clear it would be a way
        to silence exactly the warning that must not be silenceable."""
        from shortlist.server.api.settings import KNOWN_KEYS

        assert "watch.stream_down_since" not in KNOWN_KEYS
        assert "watch.stream_connected_at" not in KNOWN_KEYS


class TestBlankPlexFieldsAreNotConfigured:
    """Clearing the Plex fields in Settings leaves `{"v": ""}` rows behind. An EXISTENCE test called
    that configured while `build_plex_only` — which tests the values — raises "not configured yet", so
    the listener stamped an outage every 60s and 45 minutes later raised an undismissable warning that
    could never clear, telling the owner to check a server that was not broken."""

    def test_a_blank_url_is_not_configured(self, sessions):
        from shortlist.server.db.models import Setting
        from shortlist.server.services.watch_stream import WatchStream

        with sessions() as s:
            s.add(Setting(key="plex.url", value={"v": ""}))
            s.add(Setting(key="plex.token", value={"v": "encrypted"}))
            s.commit()
        assert WatchStream(sessions, lambda **_: None)._plex_is_configured() is False

    def test_a_blank_token_is_not_configured(self, sessions):
        from shortlist.server.db.models import Setting
        from shortlist.server.services.watch_stream import WatchStream

        with sessions() as s:
            s.add(Setting(key="plex.url", value={"v": "http://plex.local:32400"}))
            s.add(Setting(key="plex.token", value={"v": ""}))
            s.commit()
        assert WatchStream(sessions, lambda **_: None)._plex_is_configured() is False

    def test_a_null_value_row_is_not_configured(self, sessions):
        from shortlist.server.db.models import Setting
        from shortlist.server.services.watch_stream import WatchStream

        with sessions() as s:
            s.add(Setting(key="plex.url", value={"v": None}))
            s.add(Setting(key="plex.token", value={"v": None}))
            s.commit()
        assert WatchStream(sessions, lambda **_: None)._plex_is_configured() is False


class TestStoppingPlaybackAsksForTheCreditNow:
    """Without this the fact sat in `watch_sessions` until the nightly sync hours later — so someone
    who watched a pick and looked at the dashboard saw nothing, and concluded tracking was broken."""

    def _live(self, stream, *, row_id):
        from shortlist.server.services.watch_stream import _Live

        live = _Live(
            session_key="1",
            account_id=99,
            rating_key=10,
            show_rating_key=None,
            media_type="movie",
            duration_ms=6_000_000,
            offset_ms=1_800_000,
            now=datetime.now(UTC) - timedelta(minutes=20),
        )
        live.last_seen_at = datetime.now(UTC)
        live.row_id = row_id
        live.flushed_at = datetime.now(UTC)
        return live

    def _row(self, sessions):
        from shortlist.server.db.models import WatchSession as WS

        with sessions() as s:
            row = WS(
                plex_account_id=99,
                session_key="1",
                rating_key=10,
                media_type="movie",
                started_at=datetime.now(UTC) - timedelta(minutes=20),
                last_seen_at=datetime.now(UTC),
                max_offset_ms=1_800_000,
                duration_ms=6_000_000,
            )
            s.add(row)
            s.commit()
            return row.id

    def test_closing_a_session_queues_the_credit_pass(self, sessions):
        from shortlist.server.db.models import Job
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        stream._close("1", self._live(stream, row_id=self._row(sessions)), "stopped")

        with sessions() as s:
            queued = s.query(Job).filter_by(kind="watch.reconcile").all()
        assert len(queued) == 1

    def test_four_streams_stopping_at_once_queue_one_pass(self, sessions):
        """A household stopping four streams wants one credit pass, not four identical ones."""
        from shortlist.server.db.models import Job
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        for _ in range(4):
            stream._close("1", self._live(stream, row_id=self._row(sessions)), "stopped")

        with sessions() as s:
            assert s.query(Job).filter_by(kind="watch.reconcile").count() == 1

    def test_a_queue_failure_never_reaches_the_listener(self, sessions):
        """A credit that does not happen now happens at the nightly sync. A listener that dies because
        a queue insert failed loses every partial watch until the process restarts."""
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)

        def boom():
            raise RuntimeError("db gone")

        stream._sessions = boom
        stream._queue_reconcile()  # must not raise

    def test_a_session_too_short_to_persist_queues_nothing(self, sessions):
        """A mis-click is not a watch, and it does not deserve a pass over the whole event log."""
        from shortlist.server.db.models import Job
        from shortlist.server.services.watch_stream import MIN_START_SECONDS, WatchStream, _Live

        stream = WatchStream(sessions, lambda **_: None)
        now = datetime.now(UTC)
        brief = _Live(
            session_key="1",
            account_id=99,
            rating_key=10,
            show_rating_key=None,
            media_type="movie",
            duration_ms=6_000_000,
            offset_ms=1000,
            now=now,
        )
        brief.last_seen_at = now + timedelta(seconds=MIN_START_SECONDS - 1)
        stream._close("1", brief, "stopped")

        with sessions() as s:
            assert s.query(Job).filter_by(kind="watch.reconcile").count() == 0


class TestCoalescingDoesNotSwallowTheNewestSession:
    """A pass that is already RUNNING has read `watch_sessions` — so skipping because of it drops
    this session's final offset with nothing left to re-queue it. The household case that motivates
    coalescing at all is two people stopping seconds apart, which is exactly when that bites."""

    def _closed(self, sessions, stream):
        from shortlist.server.db.models import WatchSession as WS
        from shortlist.server.services.watch_stream import _Live

        now = datetime.now(UTC)
        with sessions() as s:
            row = WS(
                plex_account_id=99,
                session_key="1",
                rating_key=10,
                media_type="movie",
                started_at=now - timedelta(minutes=20),
                last_seen_at=now,
                max_offset_ms=1_800_000,
                duration_ms=6_000_000,
            )
            s.add(row)
            s.commit()
            row_id = row.id
        live = _Live(
            session_key="1",
            account_id=99,
            rating_key=10,
            show_rating_key=None,
            media_type="movie",
            duration_ms=6_000_000,
            offset_ms=1_800_000,
            now=now - timedelta(minutes=20),
        )
        live.last_seen_at = now
        live.row_id = row_id
        live.flushed_at = now
        stream._close("1", live, "stopped")

    def test_a_running_pass_does_not_block_the_next_one(self, sessions):
        from shortlist.server.db.models import Job
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        self._closed(sessions, stream)
        with sessions() as s:
            s.query(Job).filter_by(kind="watch.reconcile").one().status = "running"
            s.commit()

        self._closed(sessions, stream)

        with sessions() as s:
            assert s.query(Job).filter_by(kind="watch.reconcile").count() == 2, (
                "the running pass already read the table — this session needs its own"
            )

    def test_a_queued_pass_still_coalesces(self, sessions):
        """A queued job has not read anything yet, so it will pick this session up."""
        from shortlist.server.db.models import Job
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        self._closed(sessions, stream)
        self._closed(sessions, stream)

        with sessions() as s:
            assert s.query(Job).filter_by(kind="watch.reconcile").count() == 1


class TestAPartialWatchIsNotLostToOneSlowWrite:
    """`_persist` swallows the `OperationalError` a >5s writer lock produces, so a slow database can
    never take the listener down. But if the FIRST flush is the one that fails, no `watch_sessions`
    row exists at all and `_close` returned — losing the session outright.

    Every other gap in this feature self-heals off the play log. This one cannot: the log records
    completions only, so a partial watch exists nowhere else."""

    def _live(self):
        from shortlist.server.services.watch_stream import _Live

        now = datetime.now(UTC)
        live = _Live(
            session_key="1",
            account_id=99,
            rating_key=10,
            show_rating_key=None,
            media_type="movie",
            duration_ms=6_000_000,
            offset_ms=1_800_000,
            now=now - timedelta(minutes=20),
        )
        live.last_seen_at = now
        return live

    def test_a_first_flush_that_fails_once_is_retried(self, sessions):
        from shortlist.server.db.models import WatchSession as WS
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        calls = {"n": 0}
        real = stream._flush

        def flaky(live):
            calls["n"] += 1
            if calls["n"] == 1:
                return  # the write was swallowed; `row_id` stays None
            real(live)

        stream._flush = flaky
        stream._close("1", self._live(), "stopped")

        assert calls["n"] == 2, "it must try again before giving up"
        with sessions() as s:
            row = s.query(WS).one()
            assert row.max_offset_ms == 1_800_000
            assert row.end_reason == "stopped"

    def test_two_failures_give_up_loudly_rather_than_silently(self, sessions):
        from shortlist.server.db.models import WatchSession as WS
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        stream._flush = lambda live: None  # every write swallowed
        stream._close("1", self._live(), "stopped")  # must not raise

        with sessions() as s:
            assert s.query(WS).count() == 0

    def test_a_normal_close_still_writes_once(self, sessions):
        """The retry must not double-write the happy path."""
        from shortlist.server.db.models import WatchSession as WS
        from shortlist.server.services.watch_stream import WatchStream

        stream = WatchStream(sessions, lambda **_: None)
        calls = {"n": 0}
        real = stream._flush

        def counted(live):
            calls["n"] += 1
            real(live)

        stream._flush = counted
        stream._close("1", self._live(), "stopped")

        assert calls["n"] == 1
        with sessions() as s:
            assert s.query(WS).count() == 1
