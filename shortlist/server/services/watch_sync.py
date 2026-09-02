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

from shortlist.engine.clients.plex_pms import SectionNotShared
from shortlist.engine.models import MediaType, UserProfile, UserType
from shortlist.server.db.models import User
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_cache import DEFAULT_FULL_EVERY, WatchCache
from shortlist.server.services.watch_events import ingest_play_history
from shortlist.server.services.watching_account import stamp_true_dates
from shortlist.server.settings_store import SettingsStore


def _with_owner(session: Session, profiles: list) -> list:
    """Add the owner to a sweep that would otherwise skip them.

    `enabled` decides who gets a ROW, and the owner is created with it off — most never turn it on,
    because Plex shows them everyone's rows anyway. Their watched set still has a consumer: the
    watching-account transfer copies FROM it, and this sweep is the only thing that ever fills it.
    Without this an owner who never gave themselves a row had an empty cache for ever, and the
    wizard's offer to copy their history across silently copied nothing (#88).

    Read-only, like everything else this list drives — it decides whose history is re-read, never
    who gets a row or what is written to Plex.
    """
    # `.first()`, like every other owner lookup here (`notifications.py`, `api/watching_account.py`,
    # `context_builder.py`). This is a top-up, not a precondition: it runs outside any per-profile
    # try/except, and `sync_watched` swallows what escapes it at INFO — so raising on a duplicate
    # owner row would stop EVERY user's history being read and say only "watch-sync skipped".
    owner = session.query(User).filter_by(user_type=UserType.OWNER.value).first()
    if owner is None or any(p.plex_account_id == owner.plex_account_id for p in profiles):
        return profiles
    # Paused is the per-user half of the same switch the caller checks globally. `enabled_profiles`
    # drops a paused person; topping the owner back up regardless would undo that for the one
    # account whose pause nobody thinks to check.
    if (owner.prefs or {}).get("paused"):
        return profiles
    return [
        *profiles,
        UserProfile(
            username=owner.username,
            plex_account_id=owner.plex_account_id,
            user_type=UserType.OWNER,
            slug=owner.slug,
        ),
    ]


class WatchSync:
    """Keeps every profile's watched set fresh, as cheaply as is safe."""

    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus) -> None:
        self._sessions = session_factory
        self._bus = bus

    def _dead_sweep_due(self, store: SettingsStore) -> bool:
        """Is this the periodic pass?

        Not "is a complete read due" — every sync reads the whole library now (issue #108). Gates the
        TWO things that must stay rare, because each acts on ABSENCE from a single response and
        neither self-heals: the dead-library sweep, which believes one `/library/sections` answer for
        every user, and withdrawing pick credit, which edits `picks.watched_at`. Never having run
        counts as due.

        Dropping cached titles the read did not return used to be gated here too. It no longer is —
        that made un-watching take up to a week — and it does not need to be: it is guarded by proof
        the read saw the whole library, plus a confirming re-read before any large deletion, and a
        wrong drop of a single title comes back on the next sync.
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

    def refresh_watched(self, ctx, profile, *, force_full: bool = False, sweep_dead: bool = False) -> list:
        """This person's watched set, read from the PMS and cached.

        Returns the full cached set (not just what this read fetched), so callers see the same thing
        a direct complete read would have given them.

        There is no longer a switch to bypass the cache. `sync.watch_incremental=false` used to send
        this straight to the PMS instead — which also meant nothing refreshed `watched_titles`, so
        the user page's watched list silently went stale while the setting sounded like it was making
        reads MORE thorough. With every sync now reading each library in full (issue #108) the switch
        had nothing left to turn off, so it is gone rather than left as a trap.
        """
        user_id = getattr(profile, "db_id", None)
        if user_id is None:
            with self._sessions() as session:
                row = session.query(User).filter(User.slug == profile.slug).one_or_none()
                user_id = row.id if row else None
        if user_id is None:
            # A profile with no DB row — there is nothing to cache against.
            return ctx.history_source.fetch(profile, min_completion=ctx.config.min_completion)

        cache = self._watch_cache()
        token_source = ctx.history_source
        outcomes = []
        failed: list[str] = []
        # Read once and keep: this is also the authority for which libraries still EXIST, and asking
        # twice risks sweeping against a different answer than the one just synced against.
        sections = list(ctx.plex.sections())
        with self._sessions() as session:
            for section in sections:
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
                            # EVERY sync, not just the periodic pass. Confining deletion to that pass
                            # was right when a complete read could delete on no proof at all; it is
                            # wrong now that it cannot. The cost was un-watching: before #108 the
                            # nightly incremental read dropped a title someone un-watched inside its
                            # window, and moving deletion to the weekly pass made that take up to
                            # seven days — reported straight after the fix shipped.
                            #
                            # Safe because the two guards that made weekly deletion tolerable are
                            # what make frequent deletion safe: the read must PROVE it saw the whole
                            # library, and a pass that would drop more than half a library asks the
                            # server a second time first. What is left unguarded is the small
                            # deletion — one or two titles — which is exactly what an un-watch looks
                            # like, and which self-heals on the next sync if it was wrong.
                            reconcile=True,
                        )
                    )
                except SectionNotShared:
                    # NOT a failure, so it must not reach `failed`. `ctx.plex.sections()` is the
                    # OWNER's library list walked for every person, so any library someone isn't
                    # given 403s here on every sync — and counting that as unreadable threw away
                    # their whole cache and forced an uncached complete re-read of every library,
                    # hourly, for ever. Skipping also leaves the cursor alone: there is nothing to
                    # self-heal, and a forced full read would 403 exactly the same way.
                    logger.debug("watch cache: {} section {} not shared — skipped", profile.username, section.key)
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
            # A library REMOVED from the server is swept here and nowhere else: `sync_section` only
            # ever replaces sections it read, so rows for one that is gone would otherwise be counted
            # as watched for ever. A section that merely 403'd above is still in `sections`, so an
            # unshared library is untouched — that history is still true.
            #
            # On the weekly pass only, not every sync. The sweep believes a single `/library/sections`
            # response, and `PlexClient` caches that for the life of the client — so one short answer
            # would be applied to every user in the sync. A dead library lingering a few days matches
            # the latency the full read already has; running it hourly buys nothing and multiplies the
            # exposure to a bad response by ~168.
            if sweep_dead:
                cache.forget_dead_sections(session, user_id, {str(section.key) for section in sections})
            # A transferred account's rows are CREATED here, by reading back what the transfer wrote
            # to Plex — so the transfer itself had nothing to stamp, and every one of them arrives
            # dated today. Stamping after the read is what puts the real dates on them. Idempotent and
            # a no-op for the 99% of accounts that carry no transferred events.
            stamped = stamp_true_dates(session, user_id)
            if stamped:
                logger.info("watch cache: {} — restored the true watch dates on {} title(s)", profile.username, stamped)
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
                # force_full, like the sync job. Without it this fell through to `needs_full()`,
                # which is False as soon as a section has one proven complete read on record — so
                # from the second night onward a RUN topped up incrementally and walked straight past
                # a series whose show date lags its episodes. That is issue #108's own mechanism,
                # left live on the path an owner reaches by pressing "Run now". The measured cost of
                # reading complete is 27.4s against 27.3s, so there was nothing here to protect.
                profile.history = self.refresh_watched(ctx, profile, force_full=True)
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
        reconcile_watched: Callable[..., None],
        run_lock: asyncio.Lock,
    ) -> None:
        """Refresh every enabled user's ``watched_at`` (and the owner's — see ``_with_owner``) from
        their current watch history WITHOUT rebuilding rows or writing to Plex — a read-only
        reconcile so the effectiveness report stays fresh daily even when a row's own cron is weekly
        (or a user has no scheduled row at all).

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
                # The periodic reconcile: the dead-library sweep, dropping titles Plex no longer
                # reports, and credit withdrawal. The READ below is always complete regardless.
                sweep_dead = self._dead_sweep_due(store)
                # After the pause check, not before it: `enabled_profiles` returns nothing at all
                # while "pause all" is on, and topping the owner back up would quietly make the
                # switch stop meaning "everything".
                if not store.get("paused_all"):
                    profiles = _with_owner(session, profiles)
            total = len(profiles)
            emit("sync.progress", {"done": 0, "total": total})
            for i, profile in enumerate(profiles, start=1):
                try:
                    # ALWAYS a complete read. Measured on a live 47-user, 3-library server: 27.4s
                    # complete against 27.3s incremental, because every read fetches a 500-row page
                    # per library either way and only 7 of 93 (person, library) pairs hold more than
                    # one page. The incremental path bought 0.1s and cost correctness — Plex's own
                    # "mark as played" on a series leaves the show row with no `lastViewedAt`, which
                    # an incremental walk sorts behind its cutoff and drops, so a marked series was
                    # invisible until the weekly complete read (issue #108).
                    profile.history = self.refresh_watched(ctx, profile, force_full=True, sweep_dead=sweep_dead)
                except Exception as e:
                    logger.warning("watch-sync: history fetch failed for {}: {}", profile.slug, type(e).__name__)
                emit("sync.progress", {"done": i, "total": total})
            # The server's own play log, one admin call for every account. Deliberately AFTER the
            # per-user reads and inside the same lock: it is the source of exact play TIMES, which is
            # what lets the reconcile ask "was this in their row at the time" instead of "is it in
            # their row now". Its failure must not cost us the watched-state refresh above, which is
            # what the engine depends on — hence its own try.
            try:
                with self._sessions() as session:
                    added = ingest_play_history(session, ctx.plex, SettingsStore(session))
                    session.commit()
                if added:
                    emit("sync.progress", {"done": total, "total": total, "events": added})
            except Exception as e:
                logger.warning(
                    "watch-sync: play-history read failed ({}) — watched state is still fresh", type(e).__name__
                )
            # Left on the weekly cadence deliberately, even though every read is now complete.
            # Withdrawal removes pick credit, and moving it from weekly to every pass is a separate
            # behaviour change that deserves its own review — not a side effect of this one.
            reconcile_watched(profiles, full_resync=sweep_dead)
            with self._sessions() as session:
                store = SettingsStore(session)
                # Stamp the sync so the dashboard can show "watch status synced N ago".
                store.set("report.watch_synced_at", datetime.now(UTC).isoformat())
                if sweep_dead:
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
