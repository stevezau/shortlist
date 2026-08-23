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
from concurrent.futures import ThreadPoolExecutor
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
        # Its OWN single thread, not the loop's default executor. That pool is shared with
        # `engine_run` (minutes to hours), `prefill_history`, the job worker and ~20 API handlers, and
        # is `min(32, cpu_count + 4)` wide — six slots on a 2-CPU box, several held for the length of a
        # run. Starved of a thread, this listener's writes do not run slowly, they do not START, and
        # the message loop parks behind them. One worker also serialises our own writes, so two
        # flushes for the same session can never race.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-stream")

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
        # GUARDED, and it is the only statement here that writes at boot. Unprotected it was the one
        # way to kill the listener for the life of the process: a restart normally leaves orphan rows
        # to close, so this commits, and a commit at boot races `recover_stale` and the `privacy.sync`
        # enqueue for the single SQLite writer. One `OperationalError` and `run()` exited before the
        # loop was ever entered — no reconnect, no retry, and no log line saying the feature was gone.
        # A missed sweep costs stale `ended_at IS NULL` rows; an unguarded one costs everything.
        try:
            await self._in_pool(self.close_orphans)
        except Exception as e:
            logger.warning("watch-stream: could not close orphaned sessions ({}) — carrying on", type(e).__name__)
        while not self._stop.is_set():
            try:
                ctx = await self._in_pool(lambda: self._build_context(dry_run=True, plex_only=True))
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
        await self._persist(self._close_all, "stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _in_pool(self, fn, *args):
        """Run a blocking call on OUR thread, never the shared default executor."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: fn(*args))

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
            await self._in_pool(fn, *args)
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
            # The snapshot has to go with it. `_close_all` clears `_live` precisely because the key
            # numbering may have restarted — but a stale `/status/sessions` cache survives, and
            # `_current_sessions` RETURNS the stale copy when the re-read fails, which is exactly what
            # a still-booting PMS does. A new session reusing key 556 then resolved to the previous
            # session's account: the wrong person credited.
            self._snapshot, self._snapshot_at = {}, None
            while not self._stop.is_set():
                # Raced against the stop event rather than waited on with a timeout. Parked in a 30s
                # `recv()` with no frames arriving — an idle server, which is when nobody is playing
                # and shutdown is most likely — `run()` took up to 30s to notice `stop()`, while the
                # lifespan allows 5. So `_close_all("stopped")` was never reached and every restart
                # left orphans behind, which is what made the boot sweep above load-bearing.
                recv = asyncio.ensure_future(socket.recv())
                stopping = asyncio.ensure_future(self._stop.wait())
                done, _ = await asyncio.wait({recv, stopping}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    recv.cancel()
                    return
                stopping.cancel()
                if recv not in done:
                    recv.cancel()
                    await self._housekeep(ctx)
                    continue
                raw = recv.result()
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
            # PER EVENT, so one odd frame cannot end tracking for everybody. `viewOffset` and
            # `ratingKey` are parsed with `int()`, and a non-numeric value there used to raise all the
            # way out to `run()` — which treated it as a dropped socket, reconnected, and closed every
            # live session as `replaced`. One client sending one strange frame fragmented every watch
            # in progress. These payload shapes are not fixture-backed, so they are assumptions.
            try:
                await self._on_playing(ctx, event)
            except Exception as e:
                logger.debug("watch-stream: unusable playing event ({})", type(e).__name__)

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
        # An offset past the end of the item is not progress, it is a reading that belongs to
        # something else. Observed live: a Slow Horses session recorded 3,132,562 ms against a
        # 2,630,435 ms episode — eight minutes past the end — on the same `sessionKey` as a sane 89%
        # reading, while Plex and Tautulli agreed on the runtime. Auto-play to the next episode moves
        # the offset before the `ratingKey` catches up, so the two briefly describe different items.
        #
        # DROPPED rather than clamped. Clamping to 100% would turn a bad reading into the strongest
        # claim the report can make — "they finished it" — when the truth was 89%. A little tolerance
        # for genuine end-of-file overshoot, and anything beyond that is simply not recorded.
        if live.duration_ms and offset > live.duration_ms * 1.05:
            logger.debug(
                "watch-stream: ignoring offset {}ms past the {}ms runtime of {}",
                offset,
                live.duration_ms,
                live.rating_key,
            )
        else:
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
        # The snapshot must be describing THIS play. After a PMS restart the cache can still hold the
        # previous key numbering, and `_current_sessions` deliberately returns a stale copy when the
        # re-read fails — which is what a still-booting PMS does. A snapshot naming a different title
        # under this key is evidence about something else, and taking its `account_id` credits the
        # wrong person.
        if found.get("rating_key") and int(found["rating_key"]) != rating_key:
            logger.debug("watch-stream: session {} describes a different title than the snapshot", session_key)
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
            self._snapshot = await self._in_pool(ctx.plex.active_sessions)
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
            # Per session, so one failure does not stop the sweep for the rest of the round.
            live = self._live.get(key)
            if live is not None:
                await self._persist(self._close, key, live, "timeout")

    # -- persistence ---------------------------------------------------------------------

    def _flush(self, live: _Live) -> None:
        """Write progress back, inserting the row on first flush."""
        # Stamped on ATTEMPT, not on success. Set after the commit, a failed write left `flushed_at`
        # stale, so `now - flushed_at >= FLUSH_EVERY` stayed true and every subsequent event retried —
        # each one blocking the message loop for up to the 5s busy timeout. With several sessions past
        # their window and a run holding the writer, frames back up behind `max_queue=16`, the
        # transport pauses, pongs stop, `ping_timeout` tears the connection down, and the reconnect
        # closes every session as `replaced`. A slow database became lost watch data.
        live.flushed_at = datetime.now(UTC)
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
        """Close every tracked session, and let none of them stop the others.

        The memory state is cleared UP FRONT: this is called when the key numbering may have restarted,
        and a session left in `_live` because its write failed would go on absorbing a new play's
        offsets under a reused key. A busy database is also the condition most likely to coincide with
        a reconnect, so an early abort here defeated the guard exactly when it was needed.
        """
        pending, self._live = list(self._live.items()), {}
        for key, live in pending:
            with contextlib.suppress(Exception):
                self._close(key, live, reason)
