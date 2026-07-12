from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import rowarr.cli as cli_mod
from rowarr.cli import FileCache, FileSnapshotStore, select_users
from rowarr.engine.models import (
    CollectionDiff,
    EngineConfig,
    FilterSnapshot,
    Pick,
    RunReport,
    StageCounts,
    UserRunReport,
)
from tests.conftest import plextv_user


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


class TestFileCache:
    def test_set_get_and_expiry(self, tmp_path: Path, monkeypatch):
        cache = FileCache(tmp_path / "cache.json")
        now = {"t": 1000.0}
        monkeypatch.setattr(cli_mod.time, "time", lambda: now["t"])
        cache.set("k", "v", ttl_s=60)
        assert cache.get("k") == "v"
        now["t"] += 61
        assert cache.get("k") is None
        # persisted across instances
        cache2 = FileCache(tmp_path / "cache.json")
        assert cache2.get("missing") is None


class TestSelectUsers:
    def _ctx(self, users):
        plextv = MagicMock()
        plextv.list_users.return_value = users
        return SimpleNamespace(plextv=plextv)

    def test_all_users(self):
        ctx = self._ctx([plextv_user(100, "sarah"), plextv_user(200, "mike")])
        assert len(select_users(ctx, {"users": "all"}, None)) == 2

    def test_enabled_list_and_overrides(self):
        ctx = self._ctx([plextv_user(100, "sarah"), plextv_user(200, "mike")])
        raw = {"users": ["sarah"], "user_overrides": {"sarah": {"row_size": 10, "excluded_genres": ["Horror"]}}}
        users = select_users(ctx, raw, None)
        assert [u.username for u in users] == ["sarah"]
        assert users[0].row_size == 10
        assert users[0].excluded_genres == {"Horror"}

    def test_only_filter_unknown_user_errors(self):
        ctx = self._ctx([plextv_user(100, "sarah")])
        with pytest.raises(Exception, match="not found"):
            select_users(ctx, {"users": "all"}, "ghost")


def fake_report(status: str = "ok") -> RunReport:
    picks = [Pick(tmdb_id=1, rating_key=10, title="Movie", rank=1, reason="r")]
    return RunReport(
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        users=[
            UserRunReport(
                username="sarah",
                slug="sarah",
                status=status,
                picks=picks if status == "ok" else [],
                counts=StageCounts(picks=1 if status == "ok" else 0),
                diff=CollectionDiff(),
                error=None if status == "ok" else "boom",
            )
        ],
    )


def fake_ctx(config_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=EngineConfig(),
        recent_picks={},
        plextv=MagicMock(),
        plex=MagicMock(),
        snapshots=FileSnapshotStore(config_dir / "snapshots"),
    )


def write_gate(config_dir: Path, *, passed: bool = True, days_old: int = 0, version=(1, 43, 3, 10793)) -> None:
    from datetime import timedelta

    (config_dir / "privacy_check.json").write_text(
        json.dumps(
            {
                "ran_at": (datetime.now(UTC) - timedelta(days=days_old)).isoformat(),
                "passed": passed,
                "pms_version": list(version),
                "tiers": {"T1": passed},
            }
        )
    )


class TestPrivacyGate:
    """plex-safety rule 1: no real writes without a recorded passing Privacy Check."""

    def _invoke_run(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(cli_mod, "load_context", lambda *a, **k: (fake_ctx(tmp_path), {"users": "all"}))
        monkeypatch.setattr(cli_mod, "select_users", lambda ctx, raw, only: [MagicMock(slug="sarah")])
        monkeypatch.setattr(cli_mod, "engine_run", lambda ctx, users: fake_report())
        return CliRunner().invoke(cli_mod.main, ["--config-dir", str(tmp_path), "run"])

    def test_real_run_refused_without_recorded_check(self, tmp_path: Path, monkeypatch):
        result = self._invoke_run(tmp_path, monkeypatch)
        assert result.exit_code != 0
        assert "rowarr verify" in result.output

    def test_real_run_refused_when_check_failed(self, tmp_path: Path, monkeypatch):
        write_gate(tmp_path, passed=False)
        result = self._invoke_run(tmp_path, monkeypatch)
        assert result.exit_code != 0
        assert "FAILED" in result.output

    def test_real_run_refused_when_check_stale(self, tmp_path: Path, monkeypatch):
        write_gate(tmp_path, days_old=8)
        result = self._invoke_run(tmp_path, monkeypatch)
        assert result.exit_code != 0
        assert "days old" in result.output

    def test_real_run_refused_on_prefix_pms(self, tmp_path: Path, monkeypatch):
        write_gate(tmp_path, version=(1, 42, 0, 9000))
        result = self._invoke_run(tmp_path, monkeypatch)
        assert result.exit_code != 0
        assert "upgrade Plex" in result.output

    def test_dry_run_needs_no_gate(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(cli_mod, "load_context", lambda *a, **k: (fake_ctx(tmp_path), {"users": "all"}))
        monkeypatch.setattr(cli_mod, "select_users", lambda ctx, raw, only: [MagicMock(slug="sarah")])
        monkeypatch.setattr(cli_mod, "engine_run", lambda ctx, users: fake_report())
        result = CliRunner().invoke(cli_mod.main, ["--config-dir", str(tmp_path), "run", "--dry-run"])
        assert result.exit_code == 0, result.output


class TestRunCommand:
    def test_run_writes_state_and_exits_zero(self, tmp_path: Path, monkeypatch):
        write_gate(tmp_path)
        monkeypatch.setattr(cli_mod, "load_context", lambda *a, **k: (fake_ctx(tmp_path), {"users": "all"}))
        monkeypatch.setattr(cli_mod, "select_users", lambda ctx, raw, only: [MagicMock(slug="sarah")])
        monkeypatch.setattr(cli_mod, "engine_run", lambda ctx, users: fake_report())

        result = CliRunner().invoke(cli_mod.main, ["--config-dir", str(tmp_path), "run"])

        assert result.exit_code == 0, result.output
        assert "sarah" in result.output
        assert json.loads((tmp_path / "recent_picks.json").read_text()) == {"sarah": [1]}
        assert (tmp_path / "picks_history.jsonl").exists()

    def test_dry_run_persists_nothing_and_failures_exit_nonzero(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(cli_mod, "load_context", lambda *a, **k: (fake_ctx(tmp_path), {"users": "all"}))
        monkeypatch.setattr(cli_mod, "select_users", lambda ctx, raw, only: [MagicMock(slug="sarah")])
        monkeypatch.setattr(cli_mod, "engine_run", lambda ctx, users: fake_report(status="error"))

        result = CliRunner().invoke(cli_mod.main, ["--config-dir", str(tmp_path), "run", "--dry-run"])

        assert result.exit_code == 1
        assert not (tmp_path / "recent_picks.json").exists()


class TestVerifyCommand:
    def test_t1_pass_records_gate_and_exits_zero(self, tmp_path: Path, monkeypatch):
        from rowarr.engine.models import PrivacyCheckResult

        ctx = fake_ctx(tmp_path)
        ctx.plex.owned_collections.return_value = {"sarah": ("Rowarr_sarah", 1)}
        ctx.plex.version = "1.43.3.10793-cd55560bb"
        monkeypatch.setattr(cli_mod, "load_context", lambda *a, **k: (ctx, {"users": "all"}))
        monkeypatch.setattr(cli_mod, "select_users", lambda ctx, raw, only: [])
        monkeypatch.setattr(cli_mod, "check_t1", lambda *a, **k: PrivacyCheckResult(tier="T1", passed=True))

        result = CliRunner().invoke(cli_mod.main, ["--config-dir", str(tmp_path), "verify"])
        assert result.exit_code == 0
        assert "T1 filter read-back: PASS" in result.output
        gate = json.loads((tmp_path / "privacy_check.json").read_text())
        assert gate["passed"] is True
        assert gate["pms_version"] == [1, 43, 3, 10793]

    def test_t1_failure_recorded_and_blocks(self, tmp_path: Path, monkeypatch):
        from rowarr.engine.models import PrivacyCheckResult

        ctx = fake_ctx(tmp_path)
        ctx.plex.owned_collections.return_value = {}
        ctx.plex.version = "1.43.3.10793"
        monkeypatch.setattr(cli_mod, "load_context", lambda *a, **k: (ctx, {"users": "all"}))
        monkeypatch.setattr(cli_mod, "select_users", lambda ctx, raw, only: [])
        monkeypatch.setattr(
            cli_mod, "check_t1", lambda *a, **k: PrivacyCheckResult(tier="T1", passed=False, detail={"u": "drift"})
        )

        result = CliRunner().invoke(cli_mod.main, ["--config-dir", str(tmp_path), "verify"])
        assert result.exit_code == 1
        assert json.loads((tmp_path / "privacy_check.json").read_text())["passed"] is False


class TestUninstallCommand:
    def test_dry_run_reports_without_touching(self, tmp_path: Path, monkeypatch):
        ctx = fake_ctx(tmp_path)
        owned = MagicMock()
        owned.title = "Picked for You"
        owned.labels = [SimpleNamespace(tag="Rowarr_sarah")]
        section = MagicMock()
        section.collections.return_value = [owned]
        ctx.plex.sections.return_value = [section]
        restore = MagicMock()
        monkeypatch.setattr(cli_mod, "load_context", lambda *a, **k: (ctx, {"users": "all"}))
        monkeypatch.setattr(cli_mod, "restore_user_restrictions", restore)

        result = CliRunner().invoke(cli_mod.main, ["--config-dir", str(tmp_path), "uninstall", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "1 rowarr collection(s)" in result.output
        assert "no changes made" in result.output
        ctx.plex.delete_owned_collection.assert_not_called()
