"""The PMS notification socket — assembling anonymous position updates into sessions.

The event Plex pushes is thin: a session key, a rating key, an offset, a state. No user, no runtime.
Everything here is about what has to be added to that, and the failure modes measured on a live
server before the code was written (see the module docstring for the capture).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
