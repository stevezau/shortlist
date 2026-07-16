"""Round-trip coverage for the file-backed snapshot store the fake-Plex integration tests use."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from shortlist.engine.models import FilterSnapshot
from tests.fakes.file_stores import FileSnapshotStore


class TestFileSnapshotStore:
    def test_round_trip(self, tmp_path: Path):
        store = FileSnapshotStore(tmp_path / "snaps")
        snapshot = FilterSnapshot(
            plex_account_id=100,
            username="sarah",
            taken_at=datetime(2026, 7, 12, tzinfo=UTC),
            filters={"filterMovies": "contentRating!=R"},
        )
        store.save(snapshot)
        loaded = store.get(100)
        assert loaded == snapshot
        assert store.get(999) is None
        assert [s.username for s in store.all()] == ["sarah"]
