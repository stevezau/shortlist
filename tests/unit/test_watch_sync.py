"""WatchSync's per-person fan-out: people are read in parallel, and each cache holds only their own history.

The fake PMS answers by TOKEN, the way a real one does, and the real `ShareTokenWatchSource` maps each
person to their token — so a read made with the wrong person's token shows up as the wrong titles in
somebody's cache, not as a count that happens to match.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from shortlist.engine.clients.plex_pms import WatchedRead
from shortlist.engine.history import ShareTokenWatchSource
from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.server.db.models import User, WatchedTitle
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_cache import WatchCache
from shortlist.server.services.watch_sync import WatchSync, _for_each_profile

SECTIONS = [
    SimpleNamespace(key="1", type="movie", title="Movies"),
    SimpleNamespace(key="2", type="show", title="TV Shows"),
]


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    yield make_session_factory(engine)
    engine.dispose()


def _people(sessions, count: int) -> list[UserProfile]:
    with sessions() as s:
        for i in range(1, count + 1):
            s.add(User(username=f"user{i}", slug=f"user{i}", plex_account_id=i, user_type="shared", enabled=True))
        s.commit()
    return [
        UserProfile(username=f"user{i}", plex_account_id=i, user_type=UserType.SHARED, slug=f"user{i}")
        for i in range(1, count + 1)
    ]


def _history_of(account_id: int, section_key: str) -> list[WatchedItem]:
    """Titles only this account watched in this library. Sizes differ per person, so a swapped set is
    also the wrong size, and no two (person, library) pairs share a rating key."""
    media = MediaType.MOVIE if section_key == "1" else MediaType.SHOW
    return [
        WatchedItem(
            title=f"acct{account_id}-lib{section_key}-{n}",
            media_type=media,
            watched_at=datetime(2026, 9, 1, tzinfo=UTC) - timedelta(days=n),
            tmdb_id=account_id * 10_000 + int(section_key) * 100 + n,
            rating_key=account_id * 10_000 + int(section_key) * 100 + n,
        )
        for n in range(1, account_id + 2)
    ]


def _expected(account_id: int) -> set[tuple[str, int, str]]:
    return {(key, item.rating_key, item.title) for key in ("1", "2") for item in _history_of(account_id, key)}


class FakePms:
    """Serves watched titles by the token they are read with, and records how reads overlapped.

    The FIRST read waits (up to `gate_timeout`) for a second read to start. Code that reads people
    one at a time never starts one, so it times out with `peak == 1`; code that reads in parallel
    releases it at once. That is what makes "they ran concurrently" observable without a flat sleep.
    """

    def __init__(self, accounts: list[int], *, gate_timeout: float = 5.0, jitter_seed: int | None = None) -> None:
        self.tokens = {account: f"token-{account}" for account in accounts}
        self._account_by_token = {token: account for account, token in self.tokens.items()}
        self._gate_timeout = gate_timeout
        self._rng = random.Random(jitter_seed) if jitter_seed is not None else None
        self._lock = threading.Lock()
        self._in_flight = 0
        self._first_taken = False
        self._overlapped = threading.Event()
        self.peak = 0
        self.started: list[tuple[int, str]] = []

    def sections(self) -> list[SimpleNamespace]:
        return list(SECTIONS)

    def watched_titles(self, section_key, media_type, token: str, *, since=None) -> WatchedRead:
        account = self._account_by_token[token]
        with self._lock:
            self.started.append((account, str(section_key)))
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
            if self._in_flight >= 2:
                self._overlapped.set()
            first, self._first_taken = not self._first_taken, True
            delay = self._rng.uniform(0, 0.02) if self._rng else 0.0
        try:
            if first:
                self._overlapped.wait(self._gate_timeout)
            if delay:
                time.sleep(delay)  # jitter only, so completions interleave; nothing waits on it
            return WatchedRead(items=_history_of(account, str(section_key)), covers_window=True)
        finally:
            with self._lock:
                self._in_flight -= 1


def _ctx(pms: FakePms, *, concurrency: int) -> SimpleNamespace:
    plextv = SimpleNamespace(shared_server_tokens=lambda: dict(pms.tokens))
    return SimpleNamespace(
        plex=pms,
        history_source=ShareTokenWatchSource(pms, plextv, owner_token="owner-token"),
        config=SimpleNamespace(min_completion=0.7),
        concurrency=concurrency,
    )


def _sync(
    watch_sync: WatchSync,
    ctx: SimpleNamespace,
    profiles: list[UserProfile],
    reconcile: Callable[[list], None] = lambda _profiles: None,
) -> None:
    async def go() -> None:
        await watch_sync.sync_watched(
            build_context=lambda **_kwargs: ctx,
            enabled_profiles=lambda _session: profiles,
            reconcile_watched=reconcile,
            run_lock=asyncio.Lock(),
        )

    asyncio.run(go())


def _cached(sessions) -> dict[int, set[tuple[str, int, str]]]:
    """Every cached watch row, grouped by the ACCOUNT whose user row it hangs off."""
    with sessions() as s:
        account_of = {u.id: u.plex_account_id for u in s.query(User)}
        cached: dict[int, set[tuple[str, int, str]]] = {}
        for row in s.query(WatchedTitle):
            cached.setdefault(account_of[row.user_id], set()).add((row.section_key, row.rating_key, row.title))
    return cached


def _held(profile: UserProfile) -> set[tuple[int, str]]:
    return {(item.rating_key, item.title) for item in profile.history}


def _own(account_id: int) -> set[tuple[int, str]]:
    return {(rating_key, title) for _key, rating_key, title in _expected(account_id)}


class TestParallelSync:
    def test_each_person_caches_exactly_their_own_history_when_reads_interleave(self, sessions):
        profiles = _people(sessions, 6)
        pms = FakePms([p.plex_account_id for p in profiles], jitter_seed=7)

        _sync(WatchSync(sessions, EventBus()), _ctx(pms, concurrency=4), profiles)

        assert pms.peak >= 2, "reads never overlapped, so this proved nothing about parallel reads"
        assert _cached(sessions) == {p.plex_account_id: _expected(p.plex_account_id) for p in profiles}
        for profile in profiles:
            assert _held(profile) == _own(profile.plex_account_id), profile.slug
            assert profile.history_complete is True

    def test_people_are_read_concurrently_up_to_the_run_concurrency(self, sessions):
        profiles = _people(sessions, 6)
        pms = FakePms([p.plex_account_id for p in profiles])

        _sync(WatchSync(sessions, EventBus()), _ctx(pms, concurrency=2), profiles)

        # Exactly 2: reaching it proves the reads overlapped, and not exceeding it proves the pool is
        # bounded by `run.concurrency` rather than one thread per person.
        assert pms.peak == 2

    def test_the_cache_write_step_is_never_entered_by_two_people_at_once(self, sessions, monkeypatch):
        """SQLite takes one writer at a time and makes the rest wait out `busy_timeout` (5s), which a
        cold-cache bulk insert can outlast. Reads may overlap; the step that writes must not."""
        profiles = _people(sessions, 4)
        pms = FakePms([p.plex_account_id for p in profiles])
        real_sync_section = WatchCache.sync_section
        lock = threading.Lock()
        state = {"in_flight": 0, "peak": 0, "first": True}
        second_entered = threading.Event()

        def watched_sync_section(self, *args, **kwargs):
            with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
                if state["in_flight"] >= 2:
                    second_entered.set()
                first, state["first"] = state["first"], False
            try:
                if first:
                    # Give an unserialized second writer every chance to arrive.
                    second_entered.wait(0.5)
                return real_sync_section(self, *args, **kwargs)
            finally:
                with lock:
                    state["in_flight"] -= 1

        monkeypatch.setattr(WatchCache, "sync_section", watched_sync_section)

        _sync(WatchSync(sessions, EventBus()), _ctx(pms, concurrency=4), profiles)

        assert pms.peak >= 2, "reads never overlapped, so the write step was never contended"
        assert state["peak"] == 1
        assert _cached(sessions) == {p.plex_account_id: _expected(p.plex_account_id) for p in profiles}

    def test_one_person_failing_does_not_stop_the_others_and_is_reported_as_before(self, sessions, monkeypatch):
        profiles = _people(sessions, 4)
        pms = FakePms([p.plex_account_id for p in profiles])
        bus = EventBus()
        published: list[tuple[str, dict]] = []
        monkeypatch.setattr(bus, "publish", lambda event, data: published.append((event, data)))
        watch_sync = WatchSync(sessions, bus)
        real_refresh = watch_sync.refresh_watched

        def refresh(ctx, profile, **kwargs):
            if profile.slug == "user1":
                raise RuntimeError("the PMS went away")
            return real_refresh(ctx, profile, **kwargs)

        monkeypatch.setattr(watch_sync, "refresh_watched", refresh)
        reconciled: list[list] = []
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(str(message).strip()), level="WARNING", format="{message}")
        try:
            _sync(watch_sync, _ctx(pms, concurrency=4), profiles, reconcile=reconciled.append)
        finally:
            logger.remove(sink)

        assert _cached(sessions) == {p.plex_account_id: _expected(p.plex_account_id) for p in profiles[1:]}
        assert profiles[0].history == []
        assert "watch-sync: history fetch failed for user1: RuntimeError" in lines
        # Everyone still reaches the reconcile, the failed person included, exactly as the serial loop did.
        assert reconciled == [profiles]
        progress = [data["done"] for event, data in published if event == "sync.progress"]
        assert progress == [0, 1, 2, 3, 4]
        assert ("sync.finished", {"kind": "watched", "ok": True, "count": 4}) in published

    def test_progress_counts_finished_people_in_order_while_they_run_in_parallel(self, sessions, monkeypatch):
        profiles = _people(sessions, 6)
        pms = FakePms([p.plex_account_id for p in profiles], jitter_seed=11)
        bus = EventBus()
        published: list[tuple[str, dict]] = []
        monkeypatch.setattr(bus, "publish", lambda event, data: published.append((event, data)))

        _sync(WatchSync(sessions, bus), _ctx(pms, concurrency=4), profiles)

        progress = [data for event, data in published if event == "sync.progress"]
        assert [data["done"] for data in progress] == [0, 1, 2, 3, 4, 5, 6]
        assert all(data == {"kind": "watched", "done": data["done"], "total": 6} for data in progress)

    def test_a_concurrency_of_one_reads_people_one_at_a_time_in_roster_order(self, sessions, monkeypatch):
        profiles = _people(sessions, 3)
        # Half a second for a wrongly-parallel loop to start a second read before the first finishes.
        pms = FakePms([p.plex_account_id for p in profiles], gate_timeout=0.5)
        bus = EventBus()
        published: list[tuple[str, dict]] = []
        monkeypatch.setattr(bus, "publish", lambda event, data: published.append((event, data)))

        _sync(WatchSync(sessions, bus), _ctx(pms, concurrency=1), profiles)

        assert pms.peak == 1
        assert pms.started == [(account, key) for account in (1, 2, 3) for key in ("1", "2")]
        assert [data["done"] for event, data in published if event == "sync.progress"] == [0, 1, 2, 3]
        assert _cached(sessions) == {p.plex_account_id: _expected(p.plex_account_id) for p in profiles}


class TestParallelPrefill:
    def test_a_run_prefills_people_concurrently_each_with_their_own_history(self, sessions):
        profiles = _people(sessions, 4)
        pms = FakePms([p.plex_account_id for p in profiles], jitter_seed=3)

        WatchSync(sessions, EventBus()).prefill_history(_ctx(pms, concurrency=2), profiles)

        assert pms.peak == 2
        for profile in profiles:
            assert _held(profile) == _own(profile.plex_account_id), profile.slug
            assert profile.history_complete is True


class TestSectionsAreReadOncePerSync:
    def test_sections_are_read_once_before_people_run_in_parallel(self):
        calls: list[str] = []
        seen_before_work: list[list[str]] = []
        ctx = SimpleNamespace(plex=SimpleNamespace(sections=lambda: calls.append("sections")), concurrency=4)

        _for_each_profile(ctx, [1, 2, 3], lambda _profile: seen_before_work.append(list(calls)))

        # Once, and before anyone runs: `PlexClient`'s sections cache is unlocked, so a pool that reached
        # it cold would fetch it once per person, and a dead-library sweep could see different answers.
        assert calls == ["sections"]
        assert seen_before_work == [["sections"]] * 3

    def test_a_failed_sections_pre_read_still_runs_everyone(self):
        def unreachable() -> None:
            raise RuntimeError("PMS down")

        ran: list[int] = []
        ctx = SimpleNamespace(plex=SimpleNamespace(sections=unreachable), concurrency=4)

        _for_each_profile(ctx, [1, 2], ran.append)

        assert sorted(ran) == [1, 2]


class TestACompletedSyncDropsTheCachedReport:
    def test_a_watch_sync_clears_the_cached_report(self, sessions):
        from shortlist.server.services import report_cache

        pms = FakePms([1])
        profiles = _people(sessions, 1)
        report_cache.store_report("30", {"stale": True})

        _sync(WatchSync(sessions, EventBus()), _ctx(pms, concurrency=1), profiles)

        assert report_cache.get_cached_report("30") is None
