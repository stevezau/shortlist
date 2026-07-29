"""Run service — the server's run orchestrator.

Executes runs in a worker thread (the engine is sync), persists
runs/run_users/picks/events rows, and emits SSE progress. A `runs` row is inserted BEFORE
execution so a container restart can see and abort orphaned runs. Context assembly (clients,
config, profiles) lives in ``context_builder.ContextBuilder``; this module is only orchestration.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.models import SHARED_SLUG_PREFIX
from shortlist.engine.pipeline import EngineContext
from shortlist.engine.pipeline import run as engine_run
from shortlist.server.db.models import (
    Delivery,
    Event,
    PickRow,
    RequestCandidate,
    Run,
    RunLogLine,
    RunUser,
    User,
    iso_utc,
)
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.services import jobs
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.sse import EventBus
from shortlist.server.settings_store import SettingsStore

HIT_WINDOW_DAYS = 30  # a pick counts as a hit if it is watched within 30 days of being recommended


def _parse_ts(value: str | None) -> datetime:
    """The progress hook stamps `iso_utc()` strings; turn one back into a datetime for the DB.
    A malformed or missing stamp falls back to now rather than dropping the line."""
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)


def _record_deliveries(session: Session, user_slug: str, breakdown: list[dict]) -> None:
    """Upsert this user's delivery-ledger rows from a run's per-(row, library) breakdown.

    The ledger is what lets a later reconcile find a collection it cannot re-derive a title for (a
    `{top_seed}` row renders differently every run). It is written here, on the persist path, so it
    stays in step with what the run actually delivered.

    Entries with no `rating_key` are skipped: legacy breakdowns predate the field, and a dry run never
    reaches this function at all. A row without a key is simply not in the ledger, which leaves the
    reconciles exactly where they were before it existed — the fallback still applies.
    """
    for entry in breakdown or []:
        rating_key = int(entry.get("rating_key") or 0)
        slug, library_key = entry.get("row_slug") or "", str(entry.get("library_key") or "")
        if not rating_key or not slug or not library_key:
            continue
        row = session.get(Delivery, (slug, user_slug, library_key))
        if row is None:
            row = Delivery(collection_slug=slug, user_slug=user_slug, library_key=library_key)
            session.add(row)
        row.rating_key = rating_key
        row.title = entry.get("row_title") or ""
        row.updated_at = datetime.now(UTC)


def _why_json(why) -> list[dict]:
    """Serialize a missing title's provenance for storage + the API: [{user, row, seed, source}]."""
    return [{"user": w.user, "row": w.row, "seed": w.seed, "source": w.source} for w in why]


def _candidate_row(m, run_id: int, *, status: str) -> RequestCandidate:
    """One inbox row for a missing title, in whichever state the run left it."""
    return RequestCandidate(
        tmdb_id=m.tmdb_id,
        media_type=m.media_type.value,
        title=m.title,
        year=m.year,
        imdb_id=m.imdb_id,
        poster_path=m.poster_path,
        rating=m.rating,
        vote_count=m.vote_count,
        demand=m.demand,
        tags=sorted(m.tags),
        wanters=sorted(m.wanters),
        why=_why_json(m.why),
        status=status,
        detail=m.detail,  # a failed auto-send carries WHY it didn't land, shown as "Last attempt: …"
        excluded=m.excluded,  # on a Sonarr/Radarr exclusion list — flagged in the inbox
        arr_slug=m.arr_slug,  # set for auto-sent titles, so the inbox deep-links to the arr page
        first_seen_run_id=run_id,
    )


class RunService:
    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus, config_dir: Path, secret_box):
        self._sessions = session_factory
        self._bus = bus
        self._config_dir = config_dir
        self._secrets = secret_box
        self._ctx = ContextBuilder(session_factory, secret_box, bus)
        self._lock = asyncio.Lock()  # one run at a time; nightly + manual runs must not overlap
        # SQLite is single-writer, but the engine finishes users on a thread POOL — so the live
        # per-user persist (below) must serialize its commits across those worker threads.
        self._persist_lock = threading.Lock()
        # run_id -> cancel flag for the one in-flight run, so the /cancel endpoint can ask the engine
        # to stop. The engine checks it before each user (cooperative), so an in-flight user finishes.
        self._cancels: dict[int, threading.Event] = {}
        self._tasks: set[asyncio.Task] = set()  # strong refs so in-flight runs aren't GC'd
        # run_id -> the run's stage activity log, in memory as the LIVE tail: the SSE stream and a
        # reload during the run read from here. The durable copy lands in `run_log_lines` (see
        # `_new_run_log`), which is what any later read uses.
        self._run_logs: OrderedDict[int, deque[dict]] = OrderedDict()
        self._run_log_runs = 10  # keep the in-memory tail for this many most-recent runs
        self._log_seq: dict[int, int] = {}  # run_id -> next seq
        self._log_buffer: dict[int, list[dict]] = {}  # run_id -> lines not yet flushed
        self._log_lock = threading.Lock()

    #: Flush the narration in batches. A chatty run emits thousands of lines; one INSERT each would
    #: put a write on the hot path of every stage transition for no benefit — nothing reads the
    #: durable copy while the run is live (the SSE tail serves that).
    _LOG_FLUSH_EVERY = 50

    def _new_run_log(self, run_id: int) -> Callable[[dict], None]:
        """Start (or reset) a run's activity buffer and return an append sink for the progress hook.

        The sink stamps a monotonic `seq` (a timestamp is not unique enough to dedupe on — several
        lines land in the same millisecond), appends to the live tail, and buffers for the DB.
        """
        log: deque[dict] = deque(maxlen=2000)
        self._run_logs[run_id] = log
        self._run_logs.move_to_end(run_id)
        while len(self._run_logs) > self._run_log_runs:
            evicted, _ = self._run_logs.popitem(last=False)
            self._log_seq.pop(evicted, None)
            self._log_buffer.pop(evicted, None)
        self._log_seq[run_id] = 0
        self._log_buffer[run_id] = []

        def sink(entry: dict) -> None:
            with self._log_lock:
                seq = self._log_seq.get(run_id, 0)
                self._log_seq[run_id] = seq + 1
                entry["seq"] = seq
                log.append(entry)
                buffered = self._log_buffer.setdefault(run_id, [])
                buffered.append(entry)
                due = len(buffered) >= self._LOG_FLUSH_EVERY
            if due:
                self.flush_run_log(run_id)

        return sink

    def flush_run_log(self, run_id: int) -> None:
        """Write buffered narration to `run_log_lines`. Best-effort: losing a log line must never
        fail a run that has already written to Plex."""
        with self._log_lock:
            pending = self._log_buffer.get(run_id) or []
            self._log_buffer[run_id] = []
        if not pending:
            return
        try:
            with self._sessions() as session:
                session.add_all(
                    [
                        RunLogLine(
                            run_id=run_id,
                            seq=entry["seq"],
                            ts=_parse_ts(entry.get("ts")),
                            user_slug=entry.get("user") or "",
                            stage=entry.get("stage") or "",
                            counts=entry.get("counts") or {},
                            reason=(entry.get("reason") or "")[:1024],
                            level=entry.get("level") or "info",
                        )
                        for entry in pending
                    ]
                )
                session.commit()
        except Exception:
            logger.exception("could not persist {} run-log line(s) for run {}", len(pending), run_id)

    def run_log(self, run_id: int, after_seq: int | None = None) -> list[dict]:
        """A run's activity log, newest-last.

        Serves the live in-memory tail while the run is in flight, and falls back to the durable
        `run_log_lines` rows for any run this process didn't handle — which, before those rows
        existed, was every run after a restart.
        """
        live = list(self._run_logs.get(run_id, ()))
        if live:
            entries = live
        else:
            with self._sessions() as session:
                query = session.query(RunLogLine).filter(RunLogLine.run_id == run_id)
                rows = query.order_by(RunLogLine.seq).all()
            entries = [
                {
                    "seq": row.seq,
                    "ts": iso_utc(row.ts),
                    "run_id": run_id,
                    "user": row.user_slug,
                    "stage": row.stage,
                    "counts": row.counts or {},
                    **({"reason": row.reason} if row.reason else {}),
                }
                for row in rows
            ]
        if after_seq is not None:
            entries = [e for e in entries if e.get("seq", -1) > after_seq]
        return entries

    # -- context assembly (delegated to ContextBuilder) ----------------------------------

    def build_context(
        self,
        *,
        dry_run: bool,
        loop: asyncio.AbstractEventLoop | None = None,
        run_id: int | None = None,
        log_sink: Callable[[dict], None] | None = None,
        collection_ids: list[int] | None = None,
    ) -> EngineContext:
        # Safe-mode chokepoint: EVERY Plex-touching path (runs AND the manual row
        # delete/rename/poster-reset/disable-user reconciles) gets its context here, so forcing
        # dry-run on the built context makes SHORTLIST_DRY_RUN cover all of them. Callers read the
        # effective value back off `ctx.config.dry_run`.
        dry_run = force_dry_run() or dry_run
        return self._ctx.build(
            dry_run=dry_run, loop=loop, run_id=run_id, log_sink=log_sink, collection_ids=collection_ids
        )

    def build_requests_context(self):
        """Requests config + TMDB client for the approval inbox's manual send — no Plex/LLM I/O."""
        return self._ctx.build_requests_only()

    def enabled_profiles(self, session: Session, user_ids: list[int] | None = None):
        return self._ctx.enabled_profiles(session, user_ids)

    def user_history(self, user_id: int, *, limit: int = 25) -> list[dict] | None:
        return self._ctx.user_history(user_id, limit=limit)

    def _full_resync_due(self, store) -> bool:
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

    def refresh_watched(self, ctx, profile, *, incremental: bool = True, force_full: bool = False) -> list:
        """This person's watched set, read as cheaply as is safe, and cached.

        The complete read is the fallback, not the exception: anything that leaves the cache unable
        to answer — incremental turned off, no cursor, a section never read, the weekly reconcile
        falling due — takes it. Incremental is only ever an optimisation on top.

        Returns the full cached set (not just what this read fetched), so callers see the same thing
        a complete read would have given them.
        """
        from shortlist.engine.models import MediaType

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

    def _prefill_history(self, ctx, profiles, run_id: int | None = None) -> None:
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
        wanted = [p for p in profiles if self._has_a_row_in_scope(ctx, p)]
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
    def _has_a_row_in_scope(ctx, profile) -> bool:
        """Will this run build anything for this person?

        Mirrors the engine's own gate in `rows._run_user` — a row this person is in the audience for,
        not muted, and in this run's scope — rather than inventing a second rule. If it is empty the
        engine returns "skipped" BEFORE reading any history, so pre-filling theirs is a complete
        per-user PMS read spent on someone the run then skips. Every row carries its own cron, so a
        scheduled run is always scoped and this is the common case, not the rare one.

        Fails OPEN: any context that does not expose these (a test double, a future refactor) is
        treated as in-scope, so the worst case is exactly the behaviour before this narrowing.
        """
        from shortlist.engine.rows import _in_audience, _is_muted

        config = getattr(ctx, "config", None)
        per_person = getattr(config, "per_person_rows", None)
        should_build = getattr(config, "should_build", None)
        if per_person is None or should_build is None:
            return True
        try:
            return any(
                _in_audience(profile, spec) and not _is_muted(profile, spec) and should_build(spec)
                for spec in per_person()
            )
        except Exception:
            return True

    def _watch_cache(self):
        """The cache, honouring `sync.watch_full_days`.

        The setting has to reach `WatchCache` itself, not just the global `force_full` decision:
        `needs_full` independently re-reads a section whose `last_full_at` is older than this, so
        leaving the constructor on its 7-day default silently capped the setting at 7.
        """
        from shortlist.server.services.watch_cache import DEFAULT_FULL_EVERY, WatchCache

        with self._sessions() as session:
            days = SettingsStore(session).get("sync.watch_full_days")
        every = timedelta(days=days) if isinstance(days, int) and days > 0 else DEFAULT_FULL_EVERY
        return WatchCache(self._sessions, full_every=every)

    def sync_watched_background(self) -> None:
        """Fire sync_watched as a tracked background task (the reference is kept so it isn't GC'd) —
        for the dashboard's manual 'Sync now'."""
        task = asyncio.create_task(self.sync_watched())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def sync_watched(self) -> None:
        """Refresh every enabled user's ``watched_at`` from their current watch history WITHOUT
        rebuilding rows or writing to Plex — a read-only reconcile so the effectiveness report stays
        fresh daily even when a row's own cron is weekly (or a user has no scheduled row at all).

        Skips quietly if Plex isn't configured (build_context raises), and a per-user history-fetch
        failure is logged and skipped rather than aborting the sweep. Serialized against runs by the
        same lock, so it never overlaps a live run's per-user writes.

        Streams ``sync.progress`` per user (done/total) and a final ``sync.finished`` over the SSE bus
        so the Tools page can show a live bar. Harmless on the nightly schedule (no subscribers)."""
        loop = asyncio.get_running_loop()

        def emit(event: str, data: dict) -> None:
            # work() runs in an executor thread; publish must hop back to the loop (see system.py).
            loop.call_soon_threadsafe(self._bus.publish, event, {"kind": "watched", **data})

        def work() -> int:
            ctx = self.build_context(dry_run=True)  # dry: builds clients, writes nothing to Plex
            with self._sessions() as session:
                profiles = self.enabled_profiles(session)
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
            self._reconcile_watched(profiles)
            with self._sessions() as session:
                store = SettingsStore(session)
                # Stamp the sync so the dashboard can show "watch status synced N ago".
                store.set("report.watch_synced_at", datetime.now(UTC).isoformat())
                if force_full:
                    store.set("report.watch_full_at", datetime.now(UTC).isoformat())
            return total

        async with self._lock:
            try:
                count = await loop.run_in_executor(None, work)
                logger.info("watch-sync: refreshed watch status for {} user(s)", count)
                self._bus.publish("sync.finished", {"kind": "watched", "ok": True, "count": count})
            except Exception as e:  # e.g. Plex not configured yet — never crash the scheduler
                logger.info("watch-sync skipped: {}", type(e).__name__)
                self._bus.publish("sync.finished", {"kind": "watched", "ok": False, "error": type(e).__name__})

    # -- execution -----------------------------------------------------------------------

    async def start_run(
        self,
        *,
        trigger: str,
        dry_run: bool,
        user_ids: list[int] | None = None,
        collection_ids: list[int] | None = None,
    ) -> int:
        """Insert the runs row and launch execution as a background task; returns run id.

        ``collection_ids`` scopes the run to specific rows (a per-row scheduled run builds only its
        own rows); ``None`` builds every enabled row. The leak-safe privacy sync always covers every
        account regardless, so rows not built this run stay hidden.
        """
        if force_dry_run() and not dry_run:
            logger.warning("SHORTLIST_DRY_RUN is set — forcing this {} run to dry-run (no Plex writes)", trigger)
            dry_run = True
        with self._sessions() as session:
            run = Run(trigger=trigger, dry_run=dry_run, status="queued", stats={})
            session.add(run)
            session.commit()
            run_id = run.id
        self._cancels[run_id] = threading.Event()  # armed here so /cancel works the instant it's queued
        task = asyncio.create_task(self._execute(run_id, dry_run, user_ids, collection_ids))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _execute(
        self, run_id: int, dry_run: bool, user_ids: list[int] | None, collection_ids: list[int] | None = None
    ) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._bus.publish("run.progress", {"run_id": run_id, "status": "running"})
            cancel = self._cancels.get(run_id)
            try:
                # Inside the try so a failure here (e.g. reading users) still marks the run errored
                # AND runs the finally that frees the cancel Event — never leaves a run stuck "running".
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    run.status = "running"
                    profiles = self.enabled_profiles(session, user_ids)
                    run.stats = {
                        **(run.stats or {}),
                        "expected_users": [
                            {"slug": p.slug, "username": p.username, "display_name": p.nickname or p.username}
                            for p in profiles
                        ],
                    }
                    session.commit()
                ctx = self.build_context(
                    dry_run=dry_run,
                    loop=loop,
                    run_id=run_id,
                    log_sink=self._new_run_log(run_id),
                    collection_ids=collection_ids,
                )
                # Cooperative cancel: the engine checks this before each user and skips the rest. An
                # in-flight user still finishes (per-user transactional), and the privacy merge +
                # promote still run for who was delivered, so a cancel leaves a consistent server.
                if cancel is not None:
                    ctx.cancelled = cancel.is_set
                # "Run now" for one person hands the engine a subset of the roster. Tell it so, or it
                # reads that subset as the whole server and builds/judges shared rows against it.
                ctx.config.users_scoped = user_ids is not None
                # Persist each user's results the moment they finish, so the run page fills in person by
                # person instead of staying empty until the whole run ends (the end-of-run persist below
                # is the backstop + reconciler).
                ctx.on_user_done = lambda profile, user_report: self._persist_user_live(
                    run_id, profile, user_report, dry_run
                )
                # Fill each person's history from the cache BEFORE the engine runs. The run used to
                # do its own complete per-user read — the same read the nightly sync had already
                # done hours earlier — which was half the total cost of a night.
                await loop.run_in_executor(None, self._prefill_history, ctx, profiles, run_id)  # scoped inside
                # The ONE-WRITER lock, held for the whole engine run (plex-safety rule 3 + design
                # doc §2 principle 6). Jobs deferring to runs via `_plex_busy` is only half of it:
                # without this, a writer job already mid-flight kept merging share filters straight
                # through the start of a run, and the second of two read-modify-write merges drops
                # the first's `label!=shortlist_<slug>` excludes with nothing left to catch it.
                async with jobs.plex_writer_lock():
                    report = await loop.run_in_executor(None, engine_run, ctx, profiles)
                aborted = cancel is not None and cancel.is_set()
                self._persist_report(run_id, report, status="aborted" if aborted else None)
                # The engine filled each profile's history in place, so this is the one moment we hold
                # both "what we recommended" and "what they have since watched". A dry run is a
                # preview and mutates nothing, matching the rest of persistence.
                if not dry_run:
                    self._reconcile_watched(profiles)
                status = "aborted" if aborted else ("ok" if report.ok else "error")
            except Exception as e:
                logger.exception("run {} failed", run_id)
                self._mark_run_error(run_id, {"error": f"{type(e).__name__}: {e}"})
                self._bus.publish(
                    "run.finished", {"run_id": run_id, "status": "error", "error": f"{type(e).__name__}: {e}"}
                )
                return
            finally:
                self._cancels.pop(run_id, None)
                # In the `finally` so a crashed run still leaves its narration behind — that is
                # exactly the run whose log someone will want to read.
                self.flush_run_log(run_id)
            # Carry the reason on failure so the UI (e.g. the wizard's first run) can show it inline
            # rather than only pointing at the Runs page.
            self._bus.publish(
                "run.finished",
                {"run_id": run_id, "status": status, "error": None if report.ok else report.error},
            )

    def is_running(self) -> bool:
        """Is an engine run executing in THIS process right now?

        Read by the job queue: runs and jobs are separate systems but share one Plex and one
        throttled plex.tv, and interleaving their writes risks a job merging a share filter from a
        roster snapshot a run is halfway through changing. `_cancels` holds exactly the runs this
        process is executing — entries are added when a run starts and removed when it finishes —
        so it is the authoritative in-flight set, unlike the DB status (which a crashed process
        leaves stale until the boot reap).

        A CANCELLED run still counts. Cancellation is cooperative: the engine stops taking new users
        but then falls through to the privacy merge and promote for everyone already delivered.
        Treating "cancel requested" as "finished" would open the job queue precisely inside the
        merge→promote window that rule 1 exists to protect, letting a second share-filter writer
        clobber an exclude the run had just added.
        """
        return bool(self._cancels)

    def cancel_run(self, run_id: int) -> bool:
        """Ask the in-flight run to stop. Returns True if a running run was signalled, False if that
        run isn't currently executing here (already finished, never ran, or a stale id).

        Cooperative: the engine stops before the next user, so a user already being built finishes and
        the privacy merge + promote still run for everyone delivered — the server stays consistent.
        """
        event = self._cancels.get(run_id)
        if event is None or event.is_set():
            return False
        event.set()
        self._bus.publish("run.progress", {"run_id": run_id, "status": "cancelling"})
        logger.info("run {} cancel requested", run_id)
        return True

    def _mark_run_error(self, run_id: int, stats: dict) -> None:
        """Force a run to a finished error state with the given stats (a build failure)."""
        with self._sessions() as session:
            run = session.get(Run, run_id)
            run.status = "error"
            run.finished_at = datetime.now(UTC)
            run.stats = stats
            session.commit()

    def _merge_run_stats(self, run_id: int, extra: dict) -> None:
        with self._sessions() as session:
            run = session.get(Run, run_id)
            run.stats = {**(run.stats or {}), **extra}
            session.commit()

    # -- persistence ---------------------------------------------------------------------

    def _reconcile_watched(self, profiles) -> None:
        """Mark the picks a person actually watched — the hit rate, and the whole point of the app.

        `picks.watched_at` was declared, migrated and read by the hit-rate query, but never WRITTEN:
        every user's hit rate was structurally 0%, while the docs promised "expect 20-40%".

        A pick counts as a hit only when the watch happened AFTER we recommended it (the run that
        produced it) and within 30 days — recommending something they had already seen isn't a hit,
        and neither is a watch a year later. `history_depth` is refreshed here too; it was likewise
        surfaced in the UI and written nowhere, so every user read "0 titles watched".
        """
        with self._sessions() as session:
            for profile in profiles:
                user = session.query(User).filter_by(slug=profile.slug).first()
                if user is None:
                    continue
                user.prefs = {**(user.prefs or {}), "history_depth": len(profile.history)}

                latest_watch: dict[tuple[int, str], datetime] = {}
                for item in profile.history:
                    if item.tmdb_id is None:
                        continue
                    key = (item.tmdb_id, str(item.media_type))
                    when = item.watched_at if item.watched_at.tzinfo else item.watched_at.replace(tzinfo=UTC)
                    if key not in latest_watch or when > latest_watch[key]:
                        latest_watch[key] = when
                if not latest_watch:
                    continue

                # Only picks recent enough to still be creditable: a pick older than the window can
                # never become a hit, so scanning every unwatched pick ever recorded is dead work
                # that grows without bound. Uses the pick's own created_at (when it was delivered),
                # not the run's started_at — so picks that outlive their run (after clear/prune) are
                # still creditable.
                cutoff = datetime.now(UTC) - timedelta(days=HIT_WINDOW_DAYS)
                unwatched = (
                    session.query(PickRow)
                    .filter(
                        PickRow.user_id == user.id,
                        PickRow.watched_at.is_(None),
                        PickRow.created_at >= cutoff,
                    )
                    .all()
                )
                for pick in unwatched:
                    watched = latest_watch.get((pick.tmdb_id, pick.media_type))
                    if watched is None:
                        continue
                    since = pick.created_at if pick.created_at.tzinfo else pick.created_at.replace(tzinfo=UTC)
                    if since <= watched <= since + timedelta(days=HIT_WINDOW_DAYS):
                        pick.watched_at = watched
            session.commit()

    def _persist_user_live(self, run_id: int, profile, user_report, dry_run: bool) -> None:
        """Persist ONE user's results as they finish (called from the engine's worker threads), so the
        run page shows each person on completion rather than the whole roster only at run's end. Its
        commits are serialized (SQLite single-writer) and it never re-writes a user already stored, so
        the end-of-run `_persist_report` stays a safe backstop + reconciler. A shared-row/unknown slug
        has no user row here and is handled only at run end."""
        with self._persist_lock, self._sessions() as session:
            user = session.query(User).filter_by(slug=profile.slug).first()
            if user is None:
                return
            if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is not None:
                return
            self._persist_user_report(session, run_id, user, user_report, dry_run)
            session.commit()

    def _persist_report(self, run_id: int, report, *, status: str | None = None, error: str | None = None) -> None:
        """Persist a run's outcome. `status`/`error` override what the report says — the gated
        path uses them so a refused run is never even momentarily written as a success."""
        with self._sessions() as session:
            run = session.get(Run, run_id)
            users_by_slug = {u.slug: u for u in session.query(User).all()}
            # Skipped is its OWN outcome, not a success: a skipped user built nothing, and folding
            # them into `ok` made a run where every single person was skipped report "3 succeeded ·
            # all succeeded" above three rows badged "Skipped".
            ok = errors = skipped = 0
            for user_report in report.users:
                user = users_by_slug.get(user_report.slug)
                if user is None:
                    # A SHARED row files its report under `shared_<slug>`, which is nobody's user
                    # slug — so this `continue` silently dropped it: a real Plex collection was
                    # created, labelled and promoted with no run record and NO AUDIT EVENT at all
                    # (plex-safety rule 10), and a failed shared row produced an errored run with
                    # nothing to show for it.
                    if user_report.slug.startswith(f"{SHARED_SLUG_PREFIX}_"):
                        if user_report.status == "error":
                            errors += 1
                        elif user_report.status == "skipped":
                            skipped += 1
                        self._emit_shared_row_event(session, run_id, user_report, report.dry_run)
                    continue
                if user_report.status == "error":
                    errors += 1
                elif user_report.status == "skipped":
                    skipped += 1
                else:
                    ok += 1
                # Skip anyone already written by the live per-user persist — still counted above for
                # the finalize stats. This backstops users the live path missed (e.g. it errored).
                if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is None:
                    self._persist_user_report(session, run_id, user, user_report, report.dry_run)
            self._emit_sweep_event(session, run_id, report)
            self._emit_privacy_sync_events(session, run_id, report)
            self._emit_hub_ordering_events(session, run_id, report)
            self._emit_request_events(session, run_id, report)
            self._persist_request_queue(session, run_id, report)
            if report.error:
                self._add_event(session, "run", "error", run_id, error=report.error)
            self._finalize_run(run, report, status, error, ok, errors, skipped)
            store = SettingsStore(session)
            months = int(store.get("runs.retention"))
            # Legacy DBs store the old count-based "100" — values beyond the new 24-month max are
            # treated as 0 (keep forever) until the owner visits Settings and sets a real month value.
            self._prune_runs(session, months if 0 < months <= 24 else 0)
            event_months = int(store.get("events.retention") or 0)
            self._prune_events(session, event_months if 0 < event_months <= 24 else 0)
            session.commit()

    @staticmethod
    def _prune_runs(session: Session, retention_months: int) -> None:
        """Delete runs older than `retention_months`. 0 = keep everything forever.

        What is pruned: the run row, its per-user results/traces, and its activity log — the storage
        hog (~100 KB per user per run in trace blobs).

        What is NOT, and must never be:

        * **picks** — the impact ledger. Their `run_id` is nulled, not the row deleted, so the
          dashboard's history survives a run being pruned.
        * **deliveries** — the record of which Plex collection is which row for which person. It is
          keyed by SLUG rather than by foreign key precisely so it outlives runs and rows; deleting
          it would strand real collections on real users' servers with nothing left that knows to
          clean them up.
        """
        if retention_months <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=retention_months * 30)
        old = session.query(Run.id).filter(Run.started_at < cutoff).all()
        old_ids = [rid for (rid,) in old]
        if not old_ids:
            return
        session.query(PickRow).filter(PickRow.run_id.in_(old_ids)).update(
            {PickRow.run_id: None}, synchronize_session=False
        )
        session.query(RunUser).filter(RunUser.run_id.in_(old_ids)).delete(synchronize_session=False)
        # Explicit, not left to the FK's ON DELETE CASCADE: SQLite only enforces foreign keys when
        # `PRAGMA foreign_keys` is on, and a bulk ORM delete does not cascade in Python either.
        session.query(RunLogLine).filter(RunLogLine.run_id.in_(old_ids)).delete(synchronize_session=False)
        session.query(Run).filter(Run.id.in_(old_ids)).delete(synchronize_session=False)

    @staticmethod
    def _prune_events(session: Session, retention_months: int) -> None:
        """Trim the audit trail. 0 = keep everything forever, which is the default.

        Separate from run retention on purpose: `events` is the answer to "what changed on whose
        share at 03:31" (plex-safety rule 10), so it is the one thing an operator may want to keep
        far longer than the run detail around it.
        """
        if retention_months <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=retention_months * 30)
        session.query(Event).filter(Event.ts < cutoff).delete(synchronize_session=False)

    @staticmethod
    def _add_event(session: Session, scope: str, level: str, run_id: int, *, dry_run: bool | None = None, **fields):
        """Append one audit Event, injecting the run_id (and dry_run, where relevant) that every
        emitter shares (plex-safety rule 10). Callers pass only their distinctive message fields."""
        message: dict = {"run_id": run_id}
        if dry_run is not None:
            message["dry_run"] = dry_run
        message.update(fields)
        session.add(Event(scope=scope, level=level, message=message))

    @classmethod
    def _emit_shared_row_event(cls, session: Session, run_id: int, user_report, dry_run: bool) -> None:
        """The audit record for a shared row — it has no user, so it gets no RunUser row.

        Rule 10: every write, real or dry-run, leaves a structured event with its diff. "What changed
        on the shared row at 03:31" must be answerable from the UI.
        """
        cls._add_event(
            session,
            "run.shared",
            "error" if user_report.status == "error" else "info",
            run_id,
            dry_run=dry_run,
            row=user_report.slug,
            status=user_report.status,
            picks=len(user_report.picks),
            error=user_report.error,
            # A shared row gets no RunUser row, so this event is the ONLY place its outcome is
            # recorded — without the reason, "why did my shared row build nothing" is answerable
            # from the container log and nowhere else (issue #3).
            reason=user_report.reason,
            diff=user_report.diff.__dict__ if user_report.diff else {},
        )

    @staticmethod
    def _persist_user_report(session: Session, run_id: int, user: User, user_report, dry_run: bool) -> None:
        """One user's RunUser row, their picks (non-dry-run only), and their run.user audit event."""
        user.cold_start = user_report.status == "cold_start"
        session.add(
            RunUser(
                run_id=run_id,
                user_id=user.id,
                status=user_report.status,
                error=user_report.error,
                reason=user_report.reason,
                duration_ms=int(user_report.duration_s * 1000),
                llm_tokens=user_report.llm_tokens,
                llm_tokens_by_step=dict(user_report.llm_tokens_by_step),
                exa_searches=user_report.exa_searches,
                diff=user_report.diff.__dict__ if user_report.diff else {},
                breakdown=user_report.breakdown,
                trace=user_report.trace,
            )
        )
        if not dry_run:
            _record_deliveries(session, user.slug, user_report.breakdown)
            for pick in user_report.picks:
                session.add(
                    PickRow(
                        run_id=run_id,
                        user_id=user.id,
                        tmdb_id=pick.tmdb_id,
                        media_type=pick.media_type.value,
                        rating_key=pick.rating_key,
                        rank=pick.rank,
                        collection_slug=pick.collection_slug,
                        section_key=pick.section_key,
                        library=pick.library,
                        title=pick.title,
                        reason=pick.reason,
                        sources=",".join(pick.sources),
                        affinity=pick.affinity,
                        seed_tmdb_id=pick.seed_tmdb_id,
                        seed_title=pick.seed_title,
                    )
                )
        session.add(
            Event(
                scope="run.user",
                level="error" if user_report.status == "error" else "info",
                message={
                    "run_id": run_id,
                    "user": user_report.slug,
                    "status": user_report.status,
                    "dry_run": dry_run,
                    "diff": user_report.diff.__dict__ if user_report.diff else {},
                    "privacy_synced": user_report.privacy_synced,
                    "llm_tokens": user_report.llm_tokens,
                    "exa_searches": user_report.exa_searches,
                    "error": user_report.error,
                },
            )
        )

    @classmethod
    def _emit_sweep_event(cls, session: Session, run_id: int, report) -> None:
        # Rows deleted because Plex could not hide them. This is a SERVER-wide sweep, so it
        # can touch users who were not in this run at all (paused, disabled) — those have no
        # RunUser row to carry the audit, and deleting someone's row is the most destructive
        # thing a run does. It gets its own event (plex-safety rule 10).
        if not report.swept_rows:
            return
        cls._add_event(
            session,
            "run.sweep",
            "warning",
            run_id,
            dry_run=report.dry_run,
            reason="row was broken beyond repair-in-place — no share filter could hide it (wrong "
            "type for its library, or no shortlist label at all — an orphan from an interrupted "
            "run), or it shared a collection tag with other users' rows and held their picks",
            deleted=report.swept_rows,
        )

    @classmethod
    def _emit_privacy_sync_events(cls, session: Session, run_id: int, report) -> None:
        # Share-filter writes. Most of these accounts are NOT in this run's user list — they
        # are simply people the server is shared with — so they have no RunUser row to carry
        # the audit. Changing someone's Plex share permissions is the most sensitive thing
        # Shortlist does; "what changed on whose share at 03:31" has to be answerable for every
        # one of them (plex-safety rule 10).
        for account_id, write in report.filter_writes.items():
            cls._add_event(
                session,
                "run.privacy_sync",
                "info",
                run_id,
                dry_run=report.dry_run,
                plex_account_id=account_id,
                username=write["username"],
                fields={
                    field: {"before": before, "after": after} for field, (before, after) in write["fields"].items()
                },
            )

    @classmethod
    def _emit_hub_ordering_events(cls, session: Session, run_id: int, report) -> None:
        # Recommended-shelf reorders. Moving a managed hub shifts every collection's position on a
        # server-wide shelf that a co-managing tool (Kometa) also cares about, so each library we
        # actually moved rows in is audited — "what changed on the shelf at 03:31" (plex-safety rule 10).
        for entry in report.hub_orderings:
            cls._add_event(
                session,
                "run.hub_order",
                "info",
                run_id,
                dry_run=report.dry_run,
                library=entry.get("library"),
                anchor=entry.get("anchor"),
                moved=entry.get("moved", []),
            )

    @classmethod
    def _emit_request_events(cls, session: Session, run_id: int, report) -> None:
        # Sonarr/Radarr requests. Adding a title to a download app is a real outward-facing
        # write (it consumes disk and bandwidth), so every request — and every skip — is audited
        # with the app's own outcome message, dry-run included (plex-safety rule 10 spirit).
        # A separate, always-checked signal (independent of whether any title was sent): MDBList ran
        # out of quota mid-run, so ratings fell back to TMDB. Drives the owner's quota notification.
        if report.requests is not None and report.requests.warnings:
            for msg in report.requests.warnings:
                cls._add_event(
                    session, "requests.incomplete_config", "warning", run_id, dry_run=report.dry_run, detail=msg
                )
        if report.requests is not None and report.requests.ratings_rate_limited:
            cls._add_event(session, "requests.rate_limited", "warning", run_id, dry_run=report.dry_run)
        if report.requests is None or not report.requests.outcomes:
            return
        cls._add_event(
            session,
            "run.requests",
            "info",
            run_id,
            dry_run=report.dry_run,
            considered=report.requests.considered,
            outcomes=[
                {
                    "tmdb_id": o.tmdb_id,
                    "title": o.title,
                    "media_type": o.media_type.value,
                    "status": o.status,
                    "detail": o.detail,
                }
                for o in report.requests.outcomes
            ],
        )

    @staticmethod
    def _persist_request_queue(session: Session, run_id: int, report) -> None:
        """Save the titles a run wanted but did not auto-send, for the owner to approve by hand.

        Real runs only — a dry run is a preview and must not mutate the inbox. One row per
        (tmdb_id, media_type): a re-surfaced title refreshes the live facts of a still-pending row;
        a title already sent or rejected is left alone, so a download-in-progress isn't re-queued and
        a dismissed suggestion can't reappear every night.

        A pending title that has since ARRIVED in the library (grabbed elsewhere) is dropped, so the
        inbox never lingers on titles the owner already has. Same for one an ARR now tracks (added
        by hand, by another tool, or before the sent-ledger existed): while it downloads — or
        forever, if unaired — it's absent from Plex, so only the arr-presence prune can catch it.
        """
        if report.requests is None or report.dry_run:
            return
        existing = {(r.tmdb_id, r.media_type): r for r in session.query(RequestCandidate).all()}
        # Drop pending candidates the library now holds; leave sent/rejected alone (owner-actioned).
        present = {(tid, mt.value) for tid, mt in report.library_present}
        present |= report.requests.arr_present  # best-effort; empty when a check was skipped/failed
        for key in [k for k, r in existing.items() if r.status == "pending" and k in present]:
            session.delete(existing.pop(key))
        for m in report.requests.queued:
            row = existing.get((m.tmdb_id, m.media_type.value))
            if row is None:
                session.add(_candidate_row(m, run_id, status="pending"))
            elif row.status == "pending":
                (
                    row.title,
                    row.year,
                    row.imdb_id,
                    row.poster_path,
                    row.rating,
                    row.vote_count,
                    row.demand,
                    row.tags,
                    row.wanters,
                    row.why,
                    row.detail,
                    row.excluded,
                ) = (
                    m.title,
                    m.year,
                    m.imdb_id or row.imdb_id,  # keep a known id if a later run couldn't re-fetch it
                    # Same rule as imdb_id, and it is also what backfills rows queued before 0044:
                    # the first run to re-surface the title fills the artwork in.
                    m.poster_path or row.poster_path,
                    m.rating,
                    m.vote_count,
                    m.demand,
                    sorted(m.tags),
                    sorted(m.wanters),
                    _why_json(m.why),
                    m.detail or row.detail,  # keep the last failure reason if this pass didn't set one
                    m.excluded,  # refresh the exclusion flag each run (a removed exclusion clears it)
                )

        # The titles this run AUTO-SENT are filed as `sent` too. Without this the ledger only knew
        # about titles the owner sent by hand, so an auto-sent title still downloading was "missing"
        # again tomorrow: it out-ranked everything by demand, re-consumed one of `max_per_run` every
        # single night, and the queue starved on the same few titles forever.
        # The Arr's answer per auto-sent title, so the sent log records the outcome ("requested",
        # "already in Radarr", …), not just that it went.
        auto_outcomes = {(o.tmdb_id, o.media_type.value): o for o in report.requests.outcomes}
        for m in report.requests.sent:
            row = existing.get((m.tmdb_id, m.media_type.value))
            outcome = auto_outcomes.get((m.tmdb_id, m.media_type.value))
            if row is None:
                new_row = _candidate_row(m, run_id, status="sent")
                if outcome is not None:
                    new_row.detail = outcome.detail
                session.add(new_row)
            else:
                row.status = "sent"
                if outcome is not None:
                    row.detail = outcome.detail
                if m.arr_slug:  # keep an existing slug if this pass somehow didn't resolve one
                    row.arr_slug = m.arr_slug

    @staticmethod
    def _finalize_run(
        run: Run, report, status: str | None, error: str | None, ok: int, errors: int, skipped: int = 0
    ) -> None:
        # `report.ok` — not `errors == 0`. A run-level failure (the sweep could not run, so we
        # refused to write) has no per-user error to count, and must never report success.
        run.status = status or ("ok" if report.ok else "error")
        run.finished_at = datetime.now(UTC)
        # Run-total AI cost, summed from every user (real + shared). by_step merges each user's
        # {llm_web: n} so the run header can show WHERE the tokens went. (Since the curate step was
        # removed, llm_web — web-search title discovery — is the only paid AI path left.)
        tokens_by_step: dict[str, int] = {}
        for user_report in report.users:
            for step, n in user_report.llm_tokens_by_step.items():
                tokens_by_step[step] = tokens_by_step.get(step, 0) + n
        # Titles added to / rotated out of everyone's rows this run (summed across users' diffs), so
        # the runs list can show at a glance how much actually changed on Plex.
        titles_added = sum(len(u.diff.added) for u in report.users if u.diff)
        titles_removed = sum(len(u.diff.removed) for u in report.users if u.diff)
        run.stats = {
            "users_ok": ok,
            "users_error": errors,
            # Built nothing, but nothing went wrong — see RunUser.reason for which case it was.
            "users_skipped": skipped,
            "dry_run": report.dry_run,
            "rows_swept": sum(len(titles) for titles in report.swept_rows.values()),
            "shares_updated": len(report.filter_writes),
            "titles_added": titles_added,
            "titles_removed": titles_removed,
            "titles_requested": report.requests.requested if report.requests else 0,
            "requests_warnings": report.requests.warnings if report.requests else [],
            "llm_tokens": sum(u.llm_tokens for u in report.users),
            "llm_tokens_by_step": tokens_by_step,
            "exa_searches": sum(u.exa_searches for u in report.users),
            # Cache hits served from the shared 14-day web-search cache. Reported so the UI can read
            # "1 searched · N from cache" — without it a fully-cached run shows a bare exa_searches:1
            # and looks like the source did nothing (it didn't: the cache did the work).
            "exa_cache_hits": sum(u.exa_cache_hits for u in report.users),
            "error": error or report.error,
            # Every account whose share filter Plex refused this run. These are the reason nothing
            # was promoted, so the UI can say so instead of leaving "Failed" unexplained (issue #1).
            "promotion_blockers": list(report.promotion_blockers),
        }
