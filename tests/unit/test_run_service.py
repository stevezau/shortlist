"""RunService: DB-backed cache/snapshots, run execution persistence, error handling."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import shortlist.server.services.run_service as run_service_mod
from shortlist.engine.models import (
    CollectionDiff,
    EngineConfig,
    FilterSnapshot,
    MediaType,
    Pick,
    RunReport,
    StageCounts,
    UserRunReport,
)
from shortlist.server.db.adapters import DbCache, DbSnapshotStore
from shortlist.server.db.models import Event, PickRow, Run, RunUser, User
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.run_service import RunService
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.settings_store import SettingsStore


def _fake_ctx() -> SimpleNamespace:
    """Stands in for an EngineContext in tests that stub out the engine entirely.

    It carries the attributes `_execute` SETS on a real context (`config`, `cancelled`,
    `on_user_done`) — a bare SimpleNamespace() silently diverged from the real shape and turned a
    new assignment into an AttributeError swallowed by the run's error handling.
    """
    return SimpleNamespace(config=EngineConfig())


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(User(plex_account_id=555000100, username="sarah", slug="sarah", enabled=True))
        session.add(User(plex_account_id=555000200, username="mike", slug="mike", enabled=True))
        session.commit()
    yield factory
    engine.dispose()


class TestDbCache:
    def test_set_get_and_expiry(self, sessions):
        cache = DbCache(sessions)
        cache.set("k", json.dumps({"a": 1}), ttl_s=60)
        assert json.loads(cache.get("k")) == {"a": 1}
        cache.set("k", json.dumps({"a": 2}), ttl_s=-1)  # already expired
        assert cache.get("k") is None

    def test_concurrent_set_of_the_same_key_does_not_raise(self, sessions):
        # Parallel runs (Stage 3) fetch candidates for two users who share a seed at once — both
        # cold-miss and write the same (kind, key). The atomic upsert must let the second writer win
        # instead of raising IntegrityError (which would fail that user's pool).
        import threading
        from concurrent.futures import ThreadPoolExecutor

        cache = DbCache(sessions)
        barrier = threading.Barrier(6)

        def write(i: int) -> None:
            barrier.wait()  # maximize the collision window
            cache.set("shared-seed", json.dumps({"n": i}), ttl_s=60)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(6)))  # raises here if any thread hit IntegrityError

        assert cache.get("shared-seed") is not None  # one writer won, value is present


class TestDbSnapshotStore:
    def test_save_then_get_initial_snapshot(self, sessions):
        store = DbSnapshotStore(sessions)
        assert store.get(555000100) is None
        snapshot = FilterSnapshot(
            plex_account_id=555000100,
            username="sarah",
            taken_at=datetime(2026, 7, 12, tzinfo=UTC),
            filters={"filterMovies": "contentRating!=R"},
        )
        store.save(snapshot)
        loaded = store.get(555000100)
        assert loaded.filters == {"filterMovies": "contentRating!=R"}
        assert store.get(999999) is None


def fake_report(dry_run: bool = False) -> RunReport:
    users = []
    for slug, status in (("sarah", "ok"), ("mike", "error")):
        users.append(
            UserRunReport(
                username=slug,
                slug=slug,
                status=status,
                picks=[Pick(tmdb_id=1, rating_key=10, title="Movie", rank=1, reason="r", media_type=MediaType.MOVIE)]
                if status == "ok"
                else [],
                counts=StageCounts(picks=1 if status == "ok" else 0),
                diff=CollectionDiff(added=["Movie"]),
                error=None if status == "ok" else "boom",
                duration_s=1.5,
                privacy_synced=status == "ok",
            )
        )
    return RunReport(started_at=datetime.now(UTC), finished_at=datetime.now(UTC), dry_run=dry_run, users=users)


async def _wait_for_run(sessions, run_id: int, timeout_s: float = 3.0) -> Run:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with sessions() as session:
            run = session.get(Run, run_id)
            if run and run.status in ("ok", "error"):
                session.expunge(run)
                return run
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish in time")


class TestRunExecution:
    def test_cancel_run_signals_an_armed_run_and_ignores_others(self, sessions, tmp_path):
        import threading

        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        assert service.cancel_run(999) is False  # nothing in-flight with that id
        service._cancels[7] = threading.Event()  # simulate a run currently executing
        assert service.cancel_run(7) is True
        assert service._cancels[7].is_set()  # the engine's cooperative cancel flag is now set
        assert service.cancel_run(7) is False  # already cancelling — not signalled again

    def test_a_skipped_user_is_counted_as_skipped_not_as_a_success(self, sessions, tmp_path, monkeypatch):
        """A skipped person built nothing. Folding them into `users_ok` is what made a run where
        EVERY person was skipped report "3 succeeded · all succeeded" over three "Skipped" rows —
        the summary contradicting the rows right beneath it (issue #3 follow-up)."""
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())
        report = RunReport(
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            dry_run=False,
            users=[
                UserRunReport(username="sarah", slug="sarah", status="skipped", reason="No per-person rows."),
                UserRunReport(username="mike", slug="mike", status="ok", diff=CollectionDiff(added=["Movie"])),
            ],
        )
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: report)

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())

        assert run.stats["users_skipped"] == 1
        assert run.stats["users_ok"] == 1, "the skipped user must not be counted as a success"
        assert run.stats["users_error"] == 0
        assert run.status == "ok"  # a skip is not a failure — the run itself is still fine
        with sessions() as session:
            rows = {r.user.username: (r.status, r.reason) for r in session.query(RunUser).all()}
        assert rows["sarah"] == ("skipped", "No per-person rows.")

    def test_run_persists_report_picks_and_events(self, sessions, tmp_path, monkeypatch):
        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: fake_report())

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())
        assert run.status == "error"  # one user errored -> run status error
        assert run.stats == {
            "users_ok": 1,
            "users_error": 1,
            "users_skipped": 0,
            "dry_run": False,
            "rows_swept": 0,
            "shares_updated": 0,
            "titles_added": 2,  # the fake_report's ok user has a 2-title diff.added
            "titles_removed": 0,
            "titles_requested": 0,
            "llm_tokens": 0,
            "llm_tokens_by_step": {},
            "exa_searches": 0,
            "exa_cache_hits": 0,
            "error": None,
            "promotion_blockers": [],
        }
        with sessions() as session:
            run_users = session.query(RunUser).filter_by(run_id=run.id).all()
            assert {r.status for r in run_users} == {"ok", "error"}
            picks = session.query(PickRow).all()
            assert len(picks) == 1
            assert picks[0].title == "Movie"
            events = session.query(Event).filter_by(scope="run.user").all()
            assert len(events) == 2
            assert any(e.level == "error" for e in events)

    def test_shortlist_dry_run_env_forces_dry_run(self, sessions, tmp_path, monkeypatch):
        """SHORTLIST_DRY_RUN forces even a non-dry 'Run now' to dry-run — the safety a demo/test
        instance pointed at a real server relies on (it can never write to Plex)."""
        monkeypatch.setenv("SHORTLIST_DRY_RUN", "1")
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        captured: dict = {}

        def fake_build_context(**kw):
            captured["dry_run"] = kw.get("dry_run")
            return _fake_ctx()

        monkeypatch.setattr(service, "build_context", fake_build_context)
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: fake_report(dry_run=True))

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)  # caller asked for a REAL run
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())
        assert captured["dry_run"] is True  # engine built in dry-run despite the caller's dry_run=False
        assert run.dry_run is True  # and the persisted run is marked dry
        assert run.stats["dry_run"] is True

    def test_build_context_forces_dry_run_under_safe_mode(self, sessions, tmp_path, monkeypatch):
        """Safe-mode chokepoint: build_context is where EVERY Plex-touching path (runs + the manual
        row delete/rename/poster/disable reconciles) gets its context, so forcing dry-run here covers
        all of them. Assert a caller's dry_run=False is overridden when the env is set."""
        monkeypatch.setenv("SHORTLIST_DRY_RUN", "1")
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        captured: dict = {}
        monkeypatch.setattr(service._ctx, "build", lambda **kw: captured.update(kw) or SimpleNamespace())

        service.build_context(dry_run=False)

        assert captured["dry_run"] is True  # forced on despite the caller asking for a live context

    def _one_user_report(self, slug: str) -> UserRunReport:
        return UserRunReport(
            username=slug,
            slug=slug,
            status="ok",
            picks=[Pick(tmdb_id=1, rating_key=10, title="Movie", rank=1, reason="r", media_type=MediaType.MOVIE)],
            counts=StageCounts(picks=1),
            diff=CollectionDiff(added=["Movie"]),
            error=None,
            duration_s=1.0,
            privacy_synced=True,
        )

    def _new_run(self, sessions) -> int:
        with sessions() as session:  # users 'sarah'/'mike' are already seeded by the fixture
            run = Run(trigger="manual", status="running", stats={})
            session.add(run)
            session.commit()
            return run.id

    def _report(self, *reports: UserRunReport) -> RunReport:
        return RunReport(started_at=datetime.now(UTC), finished_at=datetime.now(UTC), users=list(reports))

    def test_live_persist_then_end_persist_writes_each_user_exactly_once(self, sessions, tmp_path):
        """The live per-user persist writes a user; the end-of-run persist must NOT write them again —
        exactly one RunUser + its picks + one run.user event, not two."""
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        run_id = self._new_run(sessions)
        report = self._one_user_report("sarah")

        service._persist_user_live(run_id, SimpleNamespace(slug="sarah"), report, dry_run=False)
        with sessions() as s:
            assert s.query(RunUser).filter_by(run_id=run_id).count() == 1
            assert s.query(PickRow).filter_by(run_id=run_id).count() == 1
            assert s.query(Event).filter_by(scope="run.user").count() == 1

        # End-of-run persist over the same user must be a no-op for their rows (dedup guard).
        service._persist_report(run_id, self._report(report))
        with sessions() as s:
            assert s.query(RunUser).filter_by(run_id=run_id).count() == 1
            assert s.query(PickRow).filter_by(run_id=run_id).count() == 1
            assert s.query(Event).filter_by(scope="run.user").count() == 1
            assert s.get(Run, run_id).stats["users_ok"] == 1  # still counted for finalize stats

    def test_end_persist_backstops_a_user_the_live_path_missed(self, sessions, tmp_path):
        """If the live persist never ran for a user (hook raised / unwired), the end-of-run persist
        still writes them exactly once."""
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        run_id = self._new_run(sessions)

        service._persist_report(run_id, self._report(self._one_user_report("mike")))
        with sessions() as s:
            assert s.query(RunUser).filter_by(run_id=run_id).count() == 1
            assert s.query(PickRow).filter_by(run_id=run_id).count() == 1

    def test_a_shared_rows_write_is_audited(self, sessions, tmp_path, monkeypatch):
        """A shared row files its report under `shared_<slug>`, which is nobody's user slug — so
        _persist_report's `if user is None: continue` dropped it whole. A real Plex collection was
        created, labelled and promoted with NO audit event at all (plex-safety rule 10), and a failed
        shared row produced an errored run with nothing to show for it."""
        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())

        shared = UserRunReport(
            username="Popular on this server",
            slug="shared_popular",
            status="ok",
            picks=[Pick(tmdb_id=7, rating_key=70, title="Dune", rank=1, reason="r", media_type=MediaType.MOVIE)],
            counts=StageCounts(picks=1),
            diff=CollectionDiff(added=["Dune"]),
            duration_s=0.5,
        )
        report = RunReport(started_at=datetime.now(UTC), finished_at=datetime.now(UTC), users=[shared])
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: report)

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())

        with sessions() as session:
            events = session.query(Event).filter_by(scope="run.shared").all()
            assert len(events) == 1, "a shared row's Plex write left no audit trail"
            assert events[0].message["row"] == "shared_popular"
            assert events[0].message["diff"]["added"] == ["Dune"]
            assert events[0].message["picks"] == 1
        assert run.status == "ok"

    def test_a_skipped_shared_row_is_counted_as_skipped_and_still_audited(self, sessions, tmp_path, monkeypatch):
        """A shared row has no RunUser row, so this event is the only record of its outcome (rule 10)
        — and a row that built nothing must not inflate the run's success count."""
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())
        shared = UserRunReport(
            username="Shared · popular",
            slug="shared_popular",
            status="skipped",
            reason="A shared row needs at least 2 people with overlapping viewing.",
            counts=StageCounts(),
        )
        report = RunReport(started_at=datetime.now(UTC), finished_at=datetime.now(UTC), users=[shared])
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: report)

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())

        assert run.stats["users_skipped"] == 1
        assert run.stats["users_ok"] == 0 and run.stats["users_error"] == 0
        with sessions() as session:
            events = session.query(Event).filter_by(scope="run.shared").all()
            assert len(events) == 1, "a shared row's outcome must still be audited"
            assert "at least 2 people" in events[0].message["reason"]

    def test_a_failed_shared_row_makes_the_run_an_error(self, sessions, tmp_path, monkeypatch):
        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())

        shared = UserRunReport(
            username="Popular",
            slug="shared_popular",
            status="error",
            error="plex timed out",
            counts=StageCounts(),
            duration_s=0.1,
        )
        report = RunReport(started_at=datetime.now(UTC), finished_at=datetime.now(UTC), users=[shared])
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: report)

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())

        assert run.stats["users_error"] == 1  # it used to be 0 — an errored run naming nobody
        with sessions() as session:
            event = session.query(Event).filter_by(scope="run.shared").one()
            assert event.level == "error"
            assert event.message["error"] == "plex timed out"

    def test_hit_rate_marks_the_picks_a_person_actually_watched(self, sessions, tmp_path, monkeypatch):
        """`picks.watched_at` was declared, migrated and READ by the hit-rate query — and written by
        nothing. Every hit rate was structurally 0%, while the docs promised "expect 20-40%"."""
        from datetime import timedelta

        from shortlist.engine.models import UserProfile, UserType, WatchedItem
        from shortlist.server.db.models import User

        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())

        # The `sessions` fixture already seeds sarah.
        # We recommended tmdb 1 to sarah in this run; she then watched it. Title 2 she never watched,
        # and title 3 she watched a YEAR later — too late to count as a hit.
        now = datetime.now(UTC)
        report = RunReport(
            started_at=now,
            finished_at=now,
            users=[
                UserRunReport(
                    username="sarah",
                    slug="sarah",
                    status="ok",
                    picks=[
                        Pick(tmdb_id=1, rating_key=10, title="Watched", rank=1, reason="r", media_type=MediaType.MOVIE),
                        Pick(tmdb_id=2, rating_key=20, title="Ignored", rank=2, reason="r", media_type=MediaType.MOVIE),
                    ],
                    counts=StageCounts(picks=2),
                    duration_s=0.1,
                )
            ],
        )
        profile = UserProfile(
            username="sarah",
            plex_account_id=100,
            user_type=UserType.SHARED,
            slug="sarah",
            history=[
                WatchedItem(title="Watched", media_type=MediaType.MOVIE, watched_at=now + timedelta(days=2), tmdb_id=1),
            ],
        )
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: report)
        monkeypatch.setattr(service, "enabled_profiles", lambda session, user_ids=None: [profile])

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        asyncio.run(scenario())

        with sessions() as session:
            picks = {p.tmdb_id: p for p in session.query(PickRow).all()}
            assert picks[1].watched_at is not None, "a watched pick was never credited to the hit rate"
            assert picks[2].watched_at is None  # never watched
            user = session.query(User).filter_by(slug="sarah").one()
            assert user.prefs["history_depth"] == 1  # also written by nothing before

    def test_an_auto_sent_title_is_filed_and_never_re_requested(self, sessions, tmp_path, monkeypatch):
        """The starvation bug, end to end. An auto-sent title used to leave NO ledger row — only
        titles the owner sent by hand did — so tomorrow it was 'missing' again, out-ranked everything
        by demand, re-consumed one of max_per_run, and the queue starved on the same few titles."""
        from shortlist.engine.models import MissingTitle, RequestOutcome, RequestReport, RequestWhy
        from shortlist.server.db.models import RequestCandidate

        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())

        why = RequestWhy(user="Sarah", row="Sarah's Picks", seed="Blade Runner", source="tmdb_similar")
        sent = MissingTitle(42, "Dune", MediaType.MOVIE, 2021, rating=8.5, vote_count=900, demand=4, why=[why])
        report = RunReport(
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            users=[],
            requests=RequestReport(
                considered=1,
                outcomes=[RequestOutcome(42, "Dune", MediaType.MOVIE, "requested", detail="added to Radarr")],
                sent=[sent],
            ),
        )
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: report)

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        asyncio.run(scenario())

        with sessions() as session:
            row = session.query(RequestCandidate).filter_by(tmdb_id=42).one()
            assert row.status == "sent", "an auto-sent title left no ledger row, so it would be re-sent"
            # The send log needs the Arr's own answer, not just "sent" — assert the outcome landed.
            assert row.detail == "added to Radarr"
            # ...and the provenance persisted, so the log can say which row/person wanted it and why.
            assert row.why == [
                {"user": "Sarah", "row": "Sarah's Picks", "seed": "Blade Runner", "source": "tmdb_similar"}
            ]
            # ...and the next run's engine context therefore excludes it.
            handled = ContextBuilder._handled_requests(session)
            assert (42, "movie") in handled

    def test_dry_run_persists_no_picks(self, sessions, tmp_path, monkeypatch):
        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: _fake_ctx())
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: fake_report(dry_run=True))

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=True)
            return await _wait_for_run(sessions, run_id)

        asyncio.run(scenario())
        with sessions() as session:
            assert session.query(PickRow).count() == 0

    def test_context_build_failure_marks_run_error(self, sessions, tmp_path, monkeypatch):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))

        def boom(**kw):
            raise RuntimeError("Plex connection is not configured yet")

        monkeypatch.setattr(service, "build_context", boom)

        async def scenario():
            run_id = await service.start_run(trigger="schedule", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())
        assert run.status == "error"
        assert "not configured" in run.stats["error"]

    def test_user_ids_narrows_but_never_widens_past_enabled(self, sessions, tmp_path):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        with sessions() as session:
            mike = session.query(User).filter_by(slug="mike").one()
            mike.enabled = False
            session.commit()
            mike_id, sarah_id = mike.id, session.query(User).filter_by(slug="sarah").one().id
            # Asking for a disabled user must NOT run them.
            assert [p.slug for p in service.enabled_profiles(session, [mike_id])] == []
            assert [p.slug for p in service.enabled_profiles(session, [mike_id, sarah_id])] == ["sarah"]
            # Empty list means "no users", not "everyone".
            assert service.enabled_profiles(session, []) == []

    def test_enabled_profiles_skips_paused_and_maps_prefs(self, sessions, tmp_path):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        with sessions() as session:
            mike = session.query(User).filter_by(slug="mike").one()
            mike.prefs = {"paused": True}
            sarah = session.query(User).filter_by(slug="sarah").one()
            sarah.prefs = {"excluded_genres": ["Horror"]}
            session.commit()
            profiles = service.enabled_profiles(session)
        assert [p.slug for p in profiles] == ["sarah"]
        assert profiles[0].excluded_genres == {"Horror"}


class TestPauseAll:
    """The Danger Zone switch was a no-op: the key wasn't storable and nothing read it."""

    def test_paused_all_stops_every_run_without_disabling_users(self, sessions, tmp_path):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        with sessions() as session:
            assert {p.slug for p in service.enabled_profiles(session)} == {"sarah", "mike"}
            SettingsStore(session, service._secrets).set("paused_all", True)
            assert service.enabled_profiles(session) == []
            # The users are still enabled — unpausing restores them, no re-enabling needed.
            assert session.query(User).filter_by(enabled=True).count() == 2
            SettingsStore(session, service._secrets).set("paused_all", False)
            assert {p.slug for p in service.enabled_profiles(session)} == {"sarah", "mike"}


class TestSnapshotsForAccountsShortlistDoesNotKnow:
    """The server must be able to write share filters for accounts that aren't in its users table.

    A row is visible to anyone whose filter doesn't exclude it, so every account sharing the
    server needs the excludes — including someone the owner invited to Plex ten minutes ago, who
    has never appeared on the Users page. Rule 2 forbids writing a filter without snapshotting it
    first, so if the snapshot store cannot record a stranger, that account's filter is never
    written and they go on seeing other people's rows, forever, with the run reporting green.
    """

    def test_snapshotting_a_stranger_records_them_so_uninstall_can_restore_them(self, sessions):
        from shortlist.server.db.adapters import DbSnapshotStore

        store = DbSnapshotStore(sessions)
        snapshot = FilterSnapshot(
            plex_account_id=987654,
            username="brand.new",
            taken_at=datetime.now(UTC),
            filters={"filterMovies": "contentRating!=R", "filterTelevision": ""},
        )

        store.save(snapshot)

        # Round-trips: uninstall reads snapshots back through the users table.
        restored = store.get(987654)
        assert restored is not None
        assert restored.filters["filterMovies"] == "contentRating!=R"

        with sessions() as session:
            user = session.query(User).filter_by(plex_account_id=987654).one()
            assert user.username == "brand.new"
            assert user.enabled is False, "a stranger gets excludes, not a row"

    def test_two_display_names_that_slugify_alike_do_not_collide(self, sessions):
        """Plex display names are free text, and the slug column is UNIQUE. If two accounts
        slugified to the same string, the second one's snapshot would fail to save — and a
        snapshot that cannot be saved means a share filter that is never written, which means
        that account goes on seeing everyone else's rows."""
        from shortlist.server.db.adapters import DbSnapshotStore

        store = DbSnapshotStore(sessions)
        for account_id, username in ((111, "Bob Smith"), (222, "bob-smith")):
            store.save(
                FilterSnapshot(
                    plex_account_id=account_id,
                    username=username,
                    taken_at=datetime.now(UTC),
                    filters={"filterMovies": "", "filterTelevision": ""},
                )
            )

        with sessions() as session:
            slugs = {u.plex_account_id: u.slug for u in session.query(User).all()}
        assert slugs[111] != slugs[222], "two accounts must never share a slug — the label is built from it"
        assert store.get(111) is not None and store.get(222) is not None


class TestRunLogBuffer:
    """The in-memory run activity log: append via the progress sink, replay, and bounded eviction."""

    def test_appends_replays_and_evicts_old_runs(self, sessions, tmp_path):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        sink = service._new_run_log(1)
        sink({"stage": "history", "user": "sarah"})
        sink({"stage": "candidates", "user": "sarah"})
        assert [e["stage"] for e in service.run_log(1)] == ["history", "candidates"]

        # Only the most-recent runs' logs are kept in memory; older ones are evicted.
        for run_id in range(2, 2 + service._run_log_runs + 1):
            service._new_run_log(run_id)
        assert service.run_log(1) == [], "the oldest run's log is evicted once the cap is exceeded"
        assert service.run_log(999_999) == [], "a run that never ran this process has an empty log"
