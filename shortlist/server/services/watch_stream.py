"""The PMS notification socket — the only place a PARTIAL watch exists.

Plex publishes no partial-play API. `unwatched=0` gives a binary flag that does not move until a film
is ~90% played, and the server's own history log records completions only (probed: an episode at 73%
with no `viewCount` had no entry). So someone who watches twenty minutes and gives up is invisible to
every read Shortlist makes — and that is the difference between "nobody opened this pick" and "the row
got them to press play and the title lost them", which is a fact about the recommendation.

`/:/websockets/notifications` pushes `PlaySessionStateNotification`, and it is deliberately thin:

    {"sessionKey": "556", "ratingKey": "456294", "viewOffset": 1294833, "state": "paused", ...}

No user. No runtime. Both are assembled here — identity by resolving `sessionKey` against
`/status/sessions`, runtime from the same read — which is exactly why Tautulli keeps its own database
rather than querying Plex for this.

Measured against a live server before any of this was written (240s, 234 events, 10 sessions):

* events arrive per session about every **10 seconds** (median; max 15), roughly one a second across
  the whole server — so state is held in memory and flushed on a throttle. A write per event would be
  a write per second to record that ten seconds passed.
* `viewOffset` advances **1:1 with wall clock** (+231,978 ms over 232 s), so it is a real progress
  measure, not an estimate.
* a `stopped` state DOES arrive on a clean end (2 of 2 observed, both gone from `/status/sessions`
  afterwards) — but a client that crashes or drops off the network never sends one, which is why
  there is a timeout as well.
* **identity must be resolved on the first PLAYING event, never on a stop.** 8 of 10 sessions resolved
  immediately; one of the two that did not had `stopped` as the only state we ever saw, and by then
  the session had already left `/status/sessions` with nothing left to identify it.

Nothing here writes to Plex. It reads two endpoints and writes rows to our own database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta

import websockets
from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.server.db.models import WatchSession

#: How long a session may go unheard-from before we call it over. Comfortably above the ~10s cadence
#: and the 15s worst case measured live, so a slow client is not repeatedly closed and reopened.
SESSION_TIMEOUT = timedelta(minutes=5)
#: How often in-memory progress is written back. Tautulli uses the same 60s idea for the same reason.
FLUSH_EVERY = timedelta(seconds=60)
#: `/status/sessions` is re-read at most this often. One read answers every session, so a burst of new
#: sessions costs one call, not one each.
SESSION_CACHE_TTL = timedelta(seconds=5)
#: Below this, a session is a misfire rather than a start — a mis-click, an autoplay preview, a client
#: probing the file. Tautulli calls the same idea its "ignore interval".
MIN_START_SECONDS = 60


class _Live:
    """One playback session as we are watching it happen, before it is worth a write."""

    __slots__ = (
        "account_id",
        "duration_ms",
        "flushed_at",
        "last_seen_at",
        "max_offset_ms",
        "media_type",
        "rating_key",
        "row_id",
        "session_key",
        "show_rating_key",
        "started_at",
    )

    def __init__(
        self,
        session_key: str,
        account_id: int,
        rating_key: int,
        show_rating_key: int | None,
        media_type: str,
        duration_ms: int | None,
        offset_ms: int,
        now: datetime,
    ) -> None:
        self.session_key = session_key
        self.account_id = account_id
        self.rating_key = rating_key
        self.show_rating_key = show_rating_key
        self.media_type = media_type
        self.started_at = now
        self.last_seen_at = now
        self.max_offset_ms = offset_ms
        self.duration_ms = duration_ms
        self.row_id: int | None = None
        self.flushed_at = now

    @property
    def seconds(self) -> float:
        return (self.last_seen_at - self.started_at).total_seconds()


class WatchStream:
    """Holds the socket open, keeps live sessions in memory, and persists them as they settle."""

    def __init__(self, session_factory: sessionmaker[Session], build_context) -> None:
        self._sessions = session_factory
        self._build_context = build_context
        self._live: dict[str, _Live] = {}
        self._snapshot: dict[str, dict] = {}
        self._snapshot_at: datetime | None = None
        self._stop = asyncio.Event()
        self._delay = 5

    # -- lifecycle -----------------------------------------------------------------------

    def close_orphans(self) -> int:
        """Close sessions this process cannot possibly still be watching.

        Live sessions exist in memory; a restart empties that and leaves their rows with
        `ended_at IS NULL` for ever, reading as "still playing" long after the person went to bed —
        and nothing else would ever close them, because the socket only reports sessions that are
        still going. Called once at startup. `last_seen_at` is used as the end, not now(): that is the
        last moment we actually observed, and inventing a later one would overstate the watch.
        """
        with self._sessions() as session:
            rows = session.query(WatchSession).filter(WatchSession.ended_at.is_(None)).all()
            for row in rows:
                row.ended_at = row.last_seen_at
                row.end_reason = "timeout"
            if rows:
                session.commit()
                logger.info("watch-stream: closed {} session(s) left open by a restart", len(rows))
            return len(rows)

    async def run(self) -> None:
        """Connect, and keep reconnecting until asked to stop.

        A dropped socket is a normal event, not an error: every gap it leaves is covered by the play
        log on the next sweep, which is server-side and reaches back years. So this logs at INFO and
        tries again rather than escalating.
        """
        await asyncio.to_thread(self.close_orphans)
        while not self._stop.is_set():
            try:
                ctx = await asyncio.to_thread(self._build_context, dry_run=True, plex_only=True)
            except Exception as e:  # Plex not configured yet — nothing to listen to
                logger.info("watch-stream: no Plex context ({}), retrying", type(e).__name__)
                await self._sleep(60)
                continue
            try:
                # `_reset_delay` is handed in so the backoff clears the moment the socket CONNECTS,
                # not when `_listen` returns — which only happens on shutdown. Every real drop goes
                # through the `except` below, so the delay used to double monotonically for the life
                # of the process and settle at two minutes of unobserved playback per blip.
                await self._listen(ctx, on_connected=lambda: setattr(self, "_delay", 5))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info("watch-stream: socket closed ({}), reconnecting in {}s", type(e).__name__, self._delay)
                await self._sleep(self._delay)
                self._delay = min(self._delay * 2, 120)
        await asyncio.to_thread(self._close_all, "stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _persist(self, fn, *args) -> None:
        """Run a write off the event loop, and never let it take the connection down with it.

        A flush landing while a run holds the SQLite writer for longer than `busy_timeout` raises —
        and with no guard that propagated all the way to `run()`, which logged "socket closed
        (OperationalError)" and started backing off. Losing one progress write is not a reason to
        stop listening, and the log should not blame the network for the database.
        """
        try:
            await asyncio.to_thread(fn, *args)
        except Exception as e:
            logger.warning("watch-stream: could not persist session state ({})", type(e).__name__)

    async def _listen(self, ctx, *, on_connected=None) -> None:
        url = ctx.plex.notification_socket_url()
        # Token in the HEADER, never the query string (rule 9) — this URL reaches logs and exceptions.
        async with websockets.connect(
            url, additional_headers={"X-Plex-Token": ctx.plex.token}, ping_interval=30, ping_timeout=15
        ) as socket:
            logger.info("watch-stream: listening for playback events")
            if on_connected is not None:
                on_connected()
            # A fresh connection means the PMS may have restarted, which is both what drops the
            # socket AND what restarts `sessionKey` numbering at 1. Anything still tracked belongs to
            # the old numbering and would silently absorb a new play's offsets.
            await self._persist(self._close_all, "replaced")
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=30)
                except TimeoutError:
                    await self._housekeep(ctx)
                    continue
                await self._on_message(ctx, raw)
                await self._housekeep(ctx)

    # -- events --------------------------------------------------------------------------

    async def _on_message(self, ctx, raw) -> None:
        try:
            container = json.loads(raw).get("NotificationContainer", {})
        except (ValueError, AttributeError):
            return
        if container.get("type") != "playing":
            return
        for event in container.get("PlaySessionStateNotification", []) or []:
            await self._on_playing(ctx, event)

    async def _on_playing(self, ctx, event: dict) -> None:
        session_key = str(event.get("sessionKey") or "")
        if not session_key:
            return
        state = event.get("state") or ""
        offset = int(event.get("viewOffset") or 0)
        now = datetime.now(UTC)

        live = self._live.get(session_key)
        if live is not None and int(event.get("ratingKey") or 0) not in (live.rating_key, 0):
            # Plex reuses `sessionKey` — it is unique only while a session is live. A different title
            # under a key we are tracking means the old session ended without telling us and this is
            # a new one, so close it rather than merging two people's playback into one row.
            await self._persist(self._close, session_key, live, "replaced")
            live = None
        if live is None:
            # A session whose FIRST event is a stop cannot be identified: it has already left
            # `/status/sessions` (observed live), so there is nothing to resolve it against. Dropping
            # it is correct — we only ever needed the start.
            if state == "stopped":
                return
            live = await self._open(ctx, session_key, event, offset, now)
            if live is None:
                return

        live.last_seen_at = now
        live.max_offset_ms = max(live.max_offset_ms, offset)

        if state == "stopped":
            await self._persist(self._close, session_key, live, "stopped")
            return
        if now - live.flushed_at >= FLUSH_EVERY:
            await self._persist(self._flush, live)

    async def _open(self, ctx, session_key: str, event: dict, offset: int, now: datetime) -> _Live | None:
        """Start tracking a session — resolving who it is and how long the item runs."""
        snapshot = await self._current_sessions(ctx)
        found = snapshot.get(session_key)
        if not found or not found.get("account_id"):
            # Unresolvable: no user, so no attribution is possible and a guess would be worse than
            # nothing. The play log still records the completion later if there is one.
            return None
        rating_key = int(event.get("ratingKey") or found.get("rating_key") or 0)
        if not rating_key:
            return None
        live = _Live(
            session_key=session_key,
            account_id=int(found["account_id"]),
            rating_key=rating_key,
            show_rating_key=found.get("show_rating_key"),
            media_type=found.get("media_type") or "",
            duration_ms=found.get("duration_ms"),
            offset_ms=offset,
            now=now,
        )
        self._live[session_key] = live
        return live

    async def _current_sessions(self, ctx) -> dict[str, dict]:
        """`/status/sessions`, cached briefly — one read answers every session on the server."""
        now = datetime.now(UTC)
        if self._snapshot_at is not None and now - self._snapshot_at < SESSION_CACHE_TTL:
            return self._snapshot
        try:
            self._snapshot = await asyncio.to_thread(ctx.plex.active_sessions)
            self._snapshot_at = now
        except Exception as e:
            logger.debug("watch-stream: could not read active sessions ({})", type(e).__name__)
        return self._snapshot

    async def _housekeep(self, ctx) -> None:
        """Close sessions that stopped talking to us.

        A `stopped` state does arrive on a clean end, but a client that crashes or loses the network
        never sends one — Tautulli schedules a force-stop for exactly this reason rather than waiting.
        `end_reason` records which of the two happened instead of dressing one up as the other.
        """
        cutoff = datetime.now(UTC) - SESSION_TIMEOUT
        stale = [key for key, live in self._live.items() if live.last_seen_at < cutoff]
        for key in stale:
            await self._persist(self._close, key, self._live[key], "timeout")

    # -- persistence ---------------------------------------------------------------------

    def _flush(self, live: _Live) -> None:
        """Write progress back, inserting the row on first flush."""
        with self._sessions() as session:
            if live.row_id is None:
                row = WatchSession(
                    plex_account_id=live.account_id,
                    session_key=live.session_key,
                    rating_key=live.rating_key,
                    show_rating_key=live.show_rating_key,
                    media_type=live.media_type,
                    started_at=live.started_at,
                    last_seen_at=live.last_seen_at,
                    max_offset_ms=live.max_offset_ms,
                    duration_ms=live.duration_ms,
                )
                session.add(row)
                session.flush()
                live.row_id = row.id
            else:
                row = session.get(WatchSession, live.row_id)
                if row is None:
                    return
                row.last_seen_at = live.last_seen_at
                row.max_offset_ms = max(row.max_offset_ms, live.max_offset_ms)
            session.commit()
        live.flushed_at = datetime.now(UTC)

    def _close(self, session_key: str, live: _Live, reason: str) -> None:
        """Finish a session, or discard it if it was too short to mean anything."""
        self._live.pop(session_key, None)
        if live.seconds < MIN_START_SECONDS and live.row_id is None:
            # Never persisted and barely played: a mis-click, not a start.
            return
        self._flush(live)
        if live.row_id is None:
            return
        with self._sessions() as session:
            row = session.get(WatchSession, live.row_id)
            if row is not None:
                row.ended_at = live.last_seen_at
                row.end_reason = reason
                session.commit()

    def _close_all(self, reason: str) -> None:
        for key, live in list(self._live.items()):
            self._close(key, live, reason)
