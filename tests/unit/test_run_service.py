"""RunService: DB-backed cache/snapshots, run execution persistence, error handling."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import rowarr.server.services.run_service as run_service_mod
from rowarr.engine.models import CollectionDiff, FilterSnapshot, MediaType, Pick, RunReport, StageCounts, UserRunReport
from rowarr.server.db.adapters import DbCache, DbSnapshotStore
from rowarr.server.db.models import Event, PickRow, Run, RunUser, User
from rowarr.server.db.session import make_engine, make_session_factory, run_migrations
from rowarr.server.services.run_service import RunService
from rowarr.server.services.secrets import SecretBox
from rowarr.server.services.sse import EventBus
from rowarr.server.settings_store import SettingsStore


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
    def test_run_persists_report_picks_and_events(self, sessions, tmp_path, monkeypatch):
        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "_privacy_gate_error", lambda: None)  # gate has its own matrix tests
        monkeypatch.setattr(service, "build_context", lambda **kw: SimpleNamespace())
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: fake_report())

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=False)
            return await _wait_for_run(sessions, run_id)

        run = asyncio.run(scenario())
        assert run.status == "error"  # one user errored -> run status error
        assert run.stats == {
            "users_ok": 1,
            "users_error": 1,
            "dry_run": False,
            "rows_swept": 0,
            "shares_updated": 0,
            "titles_requested": 0,
            "error": None,
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

    def test_dry_run_persists_no_picks(self, sessions, tmp_path, monkeypatch):
        bus = EventBus()
        service = RunService(sessions, bus, tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "build_context", lambda **kw: SimpleNamespace())
        monkeypatch.setattr(run_service_mod, "engine_run", lambda ctx, profiles: fake_report(dry_run=True))

        async def scenario():
            run_id = await service.start_run(trigger="manual", dry_run=True)
            return await _wait_for_run(sessions, run_id)

        asyncio.run(scenario())
        with sessions() as session:
            assert session.query(PickRow).count() == 0

    def test_context_build_failure_marks_run_error(self, sessions, tmp_path, monkeypatch):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        monkeypatch.setattr(service, "_privacy_gate_error", lambda: None)

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
            sarah.prefs = {"row_size": 10, "excluded_genres": ["Horror"]}
            session.commit()
            profiles = service.enabled_profiles(session)
        assert [p.slug for p in profiles] == ["sarah"]
        assert profiles[0].row_size == 10
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


class TestSnapshotsForAccountsRowarrDoesNotKnow:
    """The server must be able to write share filters for accounts that aren't in its users table.

    A row is visible to anyone whose filter doesn't exclude it, so every account sharing the
    server needs the excludes — including someone the owner invited to Plex ten minutes ago, who
    has never appeared on the Users page. Rule 2 forbids writing a filter without snapshotting it
    first, so if the snapshot store cannot record a stranger, that account's filter is never
    written and they go on seeing other people's rows, forever, with the run reporting green.
    """

    def test_snapshotting_a_stranger_records_them_so_uninstall_can_restore_them(self, sessions):
        from rowarr.server.db.adapters import DbSnapshotStore

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
        from rowarr.server.db.adapters import DbSnapshotStore

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
