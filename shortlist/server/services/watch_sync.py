"""Watch-cache orchestration — who gets their watched set re-read, how completely, and when.

`watch_cache.WatchCache` owns the per-section mechanics (cursor, upsert, full re-read). This module
is the layer above it: it decides whether tonight is the weekly complete re-read, walks a person's
libraries, falls back to a direct complete read when the cache cannot answer, and drives the
server-wide nightly sweep that keeps the effectiveness report fresh between runs.

The pieces it needs from the run orchestrator (a built engine context, the enabled roster, the
pick/watch reconcile, the one-run-at-a-time lock) are passed in per call rather than held, so the
orchestrator stays the single owner of them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.models import MediaType
from shortlist.server.db.models import User
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_cache import DEFAULT_FULL_EVERY, WatchCache
from shortlist.server.settings_store import SettingsStore


class WatchSync:
    """Keeps every profile's watched set fresh, as cheaply as is safe."""

    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus) -> None:
        self._sessions = session_factory
        self._bus = bus

    def _full_resync_due(self, store: SettingsStore) -> bool:
        """Is tonight the weekly complete re-read?

        An incremental read cannot see an un-watch, a deleted title, or one whose `lastViewedAt`
        never moved, so a full read has to happen on a schedule regardless of how well the cursor is
        working. Never having done one counts as due.
        """
        stamp = store.get("report.watch_full_at")
        if not isinstance(stamp, str) or not stamp:
            return True
        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        days = store.get("sync.watch_full_days")
        every = days if isinstance(days, int) and days > 0 else 7
        return datetime.now(UTC) - last >= timedelta(days=every)

    def _watch_cache(self) -> WatchCache:
        """The cache, honouring `sync.watch_full_days`.

        The setting has to reach `WatchCache` itself, not just the global `force_full` decision:
        `needs_full` independently re-reads a section whose `last_full_at` is older than this, so
        leaving the constructor on its 7-day default silently capped the setting at 7.
        """
        with self._sessions() as session:
            days = SettingsStore(session).get("sync.watch_full_days")
        every = timedelta(days=days) if isinstance(days, int) and days > 0 else DEFAULT_FULL_EVERY
        return WatchCache(self._sessions, full_every=every)

    def refresh_watched(self, ctx, profile, *, incremental: bool = True, force_full: bool = False) -> list:
        """This person's watched set, read as cheaply as is safe, and cached.

        The complete read is the fallback, not the exception: anything that leaves the cache unable
        to answer — incremental turned off, no cursor, a section never read, the weekly reconcile
        falling due — takes it. Incremental is only ever an optimisation on top.

        Returns the full cached set (not just what this read fetched), so callers see the same thing
        a complete read would have given them.
        """
        user_id = getattr(profile, "db_id", None)
        if user_id is None:
            with self._sessions() as session:
                row = session.query(User).filter(User.slug == profile.slug).one_or_none()
                user_id = row.id if row else None
        if user_id is None or not incremental:
            # Nothing to cache against (a profile with no DB row), or caching is switched off.
            return ctx.history_source.fetch(profile, min_completion=ctx.config.min_completion)

        cache = self._watch_cache()
        token_source = ctx.history_source
        outcomes = []
        failed: list[str] = []
        with self._sessions() as session:
            for section in ctx.plex.sections():
                media_type = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW

                def read(since, _section=section, _media=media_type):
                    return token_source.fetch_section(profile, _section, _media, since=since)

                try:
                    outcomes.append(
                        cache.sync_section(
                            session,
                            profile,
                            user_id,
                            str(section.key),
                            media_type,
                            read,
                            force_full=force_full,
                        )
                    )
                except Exception as e:
                    failed.append(str(section.key))
                    logger.warning(
                        "watch cache: {} section {} failed ({})", profile.username, section.key, type(e).__name__
                    )
                    # Self-heal. The commonest way this fails is the PMS refusing the incremental
                    # `lastViewedAt>=` filter — and a full read sends no filter at all, so forcing
                    # one is the escape from a section that can never be topped up. Without this the
                    # cursor simply never advances and the cache silently goes stale for ever.
                    cache.force_full_next_time(session, user_id, str(section.key))
            session.commit()
            history = cache.watched_set(session, user_id)

        if failed:
            # A partial cache must NEVER be served as if it were complete: the watched set is what
            # stops an already-seen title being recommended again, so a stale one is a visible,
            # confusing regression. Fall back to the direct complete read — exactly the behaviour
            # before this cache existed, so it cannot be worse, only slower.
            logger.warning(
                "watch cache: {} — {} section(s) unreadable, falling back to a complete read",
                profile.username,
                len(failed),
            )
            return ctx.history_source.fetch(profile, min_completion=ctx.config.min_completion)

        fetched = sum(o.fetched for o in outcomes)
        full = any(o.full for o in outcomes)
        logger.info(
            "watch cache: {} {} -> {} fetched, {} cached",
            profile.username,
            "FULL" if full else "incremental",
            fetched,
            len(history),
        )
        return history

    def prefill_history(self, ctx, profiles, run_id: int | None = None) -> None:
        """Top up the cache and hand each profile its watched set, so the engine reads none itself.

        Narrated, not silent: on a cold cache this is a complete per-user PMS read, so without a
        progress line the run's first minutes look exactly like the wedge the rest of this work exists
        to remove.

        Best-effort per person: anyone whose top-up fails is left with an empty history, and the
        engine falls back to its own complete read for them — the behaviour before the cache existed.
        """
        with self._sessions() as session:
            store = SettingsStore(session)
            incremental = bool(store.get("sync.watch_incremental"))
        # Only people this run will actually build for. `_run_user` returns early — before its own
        # history read — for anyone with no row in scope, so pre-filling them is a complete per-user
        # PMS read spent on someone the run then skips.
        wanted = [p for p in profiles if self.has_a_row_in_scope(ctx, p)]
        # getattr, not attribute access: narration must never be the thing that fails a run, and a
        # caller may hand in a context that has no progress hook at all.
        progress = getattr(ctx, "progress", None)
        total = len(wanted)
        for position, profile in enumerate(wanted, start=1):
            if progress is not None:
                try:
                    progress("Shortlist", "reading_history", {"done": position, "total": total}, None)
                except Exception:  # a broken listener must never fail the run
                    logger.exception("progress callback failed during history pre-fill")
            try:
                profile.history = self.refresh_watched(ctx, profile, incremental=incremental)
            except Exception as e:
                logger.warning(
                    "run: could not pre-fill history for {} ({}) — the engine will read it directly",
                    profile.slug,
                    type(e).__name__,
                )

    @staticmethod
    def has_a_row_in_scope(ctx, profile) -> bool:
        """Will this run build anything for this person?

        Mirrors the engine's own gate in `rows._run_user` — a row this person is in the audience for,
        not muted, and in this run's scope — rather than inventing a second rule. If it is empty the
        engine returns "skipped" BEFORE reading any history, so pre-filling theirs is a complete
        per-user PMS read spent on someone the run then skips. Every row carries its own cron, so a
        scheduled run is always scoped and this is the common case, not the rare one.

        Asks the ENGINE, through its public `builds_anything_for`. The rule is the engine's — audience,
        mute and run scope — and reaching in for the private `_in_audience`/`_is_muted` to re-assemble
        it here meant the server owned a copy of a rule it does not define.

        Fails OPEN: any context that cannot answer (a test double, an engine that predates the
        export) is treated as in-scope, so the worst case is exactly the behaviour before this
        narrowing — a complete history read for somebody the run then skips, never a person silently
        missing their history.
        """
        config = getattr(ctx, "config", None)
        if config is None:
            return True
        try:
            from shortlist.engine.rows import builds_anything_for

            return builds_anything_for(profile, config)
        except Exception:
            return True

    async def sync_watched(
        self,
        *,
        build_context: Callable[..., object],
        enabled_profiles: Callable[[Session], list],
        reconcile_watched: Callable[[list], None],
        run_lock: asyncio.Lock,
    ) -> None:
        """Refresh every enabled user's ``watched_at`` from their current watch history WITHOUT
        rebuilding rows or writing to Plex — a read-only reconcile so the effectiveness report stays
        fresh daily even when a row's own cron is weekly (or a user has no scheduled row at all).

        Skips quietly if Plex isn't configured (build_context raises), and a per-user history-fetch
        failure is logged and skipped rather than aborting the sweep. Serialized against runs by
        ``run_lock`` — the run orchestrator's own lock — so it never overlaps a live run's per-user
        writes.

        Streams ``sync.progress`` per user (done/total) and a final ``sync.finished`` over the SSE bus
        so the Tools page can show a live bar. Harmless on the nightly schedule (no subscribers)."""
        loop = asyncio.get_running_loop()

        def emit(event: str, data: dict) -> None:
            # work() runs in an executor thread; publish must hop back to the loop (see system.py).
            loop.call_soon_threadsafe(self._bus.publish, event, {"kind": "watched", **data})

        def work() -> int:
            # Plex-only: this reads watch history and writes nothing anywhere. The full context also
            # builds TMDB, Trakt, Exa, MDBList and the LLM curator, so a nightly READ of watch history
            # failed when an LLM key was wrong — a failure with nothing to do with what it was doing.
            ctx = build_context(dry_run=True, plex_only=True)
            with self._sessions() as session:
                profiles = enabled_profiles(session)
                store = SettingsStore(session)
                incremental = bool(store.get("sync.watch_incremental"))
                force_full = self._full_resync_due(store)
            total = len(profiles)
            emit("sync.progress", {"done": 0, "total": total})
            for i, profile in enumerate(profiles, start=1):
                try:
                    profile.history = self.refresh_watched(ctx, profile, incremental=incremental, force_full=force_full)
                except Exception as e:
                    logger.warning("watch-sync: history fetch failed for {}: {}", profile.slug, type(e).__name__)
                emit("sync.progress", {"done": i, "total": total})
            reconcile_watched(profiles)
            with self._sessions() as session:
                store = SettingsStore(session)
                # Stamp the sync so the dashboard can show "watch status synced N ago".
                store.set("report.watch_synced_at", datetime.now(UTC).isoformat())
                if force_full:
                    store.set("report.watch_full_at", datetime.now(UTC).isoformat())
            return total

        async with run_lock:
            try:
                count = await loop.run_in_executor(None, work)
                logger.info("watch-sync: refreshed watch status for {} user(s)", count)
                self._bus.publish("sync.finished", {"kind": "watched", "ok": True, "count": count})
            except Exception as e:  # e.g. Plex not configured yet — never crash the scheduler
                logger.info("watch-sync skipped: {}", type(e).__name__)
                self._bus.publish("sync.finished", {"kind": "watched", "ok": False, "error": type(e).__name__})
