"""File-backed engine stores used by the full-stack fake-Plex integration tests.

These were the CLI's on-disk snapshot store back when a nightly cron ran the engine directly. The
server persists snapshots in the DB (``restriction_snapshots``) instead, so this now lives with the
tests: ``test_engine_vs_fake`` drives the real engine against a fake PMS and needs a concrete
``SnapshotStore`` to exercise the snapshot-first restriction path end to end.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from shortlist.engine.models import FilterSnapshot


class FileSnapshotStore:
    """One JSON file per user under a directory — the uninstall/restore source of truth on disk."""

    def __init__(self, directory: Path):
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, plex_account_id: int) -> Path:
        return self._dir / f"{plex_account_id}.json"

    def get(self, plex_account_id: int) -> FilterSnapshot | None:
        path = self._path(plex_account_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return FilterSnapshot(
            plex_account_id=data["plex_account_id"],
            username=data["username"],
            taken_at=datetime.fromisoformat(data["taken_at"]),
            filters=data["filters"],
        )

    def save(self, snapshot: FilterSnapshot) -> None:
        payload = {**asdict(snapshot), "taken_at": snapshot.taken_at.isoformat()}
        self._path(snapshot.plex_account_id).write_text(json.dumps(payload, indent=2))

    def all(self) -> list[FilterSnapshot]:
        return [s for p in sorted(self._dir.glob("*.json")) if (s := self.get(int(p.stem)))]
