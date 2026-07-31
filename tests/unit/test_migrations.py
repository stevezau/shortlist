"""The migrated schema must BE the schema the ORM declares, and boot must not churn the backups.

Every other test builds its database with `Base.metadata.create_all`, so the test schema is whatever
the ORM says regardless of what the migrations actually did. That is how 0049 and 0050 shipped eleven
columns nullable that the ORM calls NOT NULL: the test database was stricter than every real one, and
a NULL production accepted was unreachable in a test.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext

from shortlist.server.db.models import Base
from shortlist.server.db.session import ALEMBIC_DIR, db_url, make_engine, run_migrations


def _alembic(config_dir: Path) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url(config_dir))
    return cfg


def _diffs(config_dir: Path) -> list:
    engine = make_engine(config_dir)
    try:
        with engine.connect() as conn:
            return compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()


def _not_null(config_dir: Path, table: str) -> dict[str, bool]:
    con = sqlite3.connect(config_dir / "shortlist.db")
    try:
        return {row[1]: bool(row[3]) for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


#: Every column 0049/0050 created nullable while the ORM declared it NOT NULL.
TIGHTENED = {
    "run_log_lines": ("ts", "user_slug", "stage", "counts", "reason", "level"),
    "watched_titles": ("title", "watch_count", "viewed_at", "updated_at"),
    "watch_sync_state": ("item_count",),
}


class TestTheMigratedSchemaMatchesTheORM:
    def test_a_migrated_database_has_no_drift_from_the_models(self, tmp_path: Path):
        """The guard that makes the next hand-written migration impossible to get subtly wrong.

        Any column, index, unique constraint or foreign key that the migrations create differently
        from `models.py` shows up here — including a nullability the ORM does not claim, which no
        other test in this repo can see.
        """
        run_migrations(tmp_path)

        assert _diffs(tmp_path) == []

    @pytest.mark.parametrize("table,columns", sorted(TIGHTENED.items()))
    def test_the_eleven_columns_0049_and_0050_left_nullable_are_not_null(
        self, table: str, columns: tuple[str, ...], tmp_path: Path
    ):
        run_migrations(tmp_path)

        flags = _not_null(tmp_path, table)
        assert {column: flags[column] for column in columns} == dict.fromkeys(columns, True)


class TestMigration0053BackfillsBeforeItTightens:
    """0053 rebuilds three tables. An ALTER that fails halfway through boot is worse than a
    defensive UPDATE, and the backfilled values have to be the honest ones — a `viewed_at` set to
    "now" would let a title nobody can date masquerade as tonight's watch and become a seed."""

    @staticmethod
    def _seed_at_0052(config_dir: Path) -> None:
        command.upgrade(_alembic(config_dir), "0052")
        con = sqlite3.connect(config_dir / "shortlist.db")

        def insert(table: str, **values) -> None:
            """INSERT satisfying every NOT NULL column, so the fixture doesn't pin today's schema."""
            row = dict(values)
            for _cid, name, type_, notnull, default, pk in con.execute(f"PRAGMA table_info({table})"):
                if notnull and default is None and not pk and name not in row:
                    row[name] = 0 if type_.upper().startswith(("INT", "BOOL", "FLOAT", "NUM")) else ""
            columns = ", ".join(row)
            con.execute(f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

        insert("runs", id=7, trigger="schedule", started_at="2026-01-02 03:04:05", status="ok", stats="{}")
        insert("users", id=3, plex_account_id=111, username="bob", slug="bob", prefs="{}")
        # An all-NULL narration line, and a fully-populated one that must survive byte-identical.
        con.execute("INSERT INTO run_log_lines (run_id, seq) VALUES (7, 1)")
        con.execute(
            "INSERT INTO run_log_lines (run_id, seq, ts, user_slug, stage, counts, reason, level) "
            "VALUES (7, 2, '2026-01-02 03:05:06.000007', 'bob', 'deliver', '{\"added\": 3}', 'built', 'warning')"
        )
        # 100: nothing at all. 101: no viewed_at but a known updated_at. 102: the populated control.
        for rating_key, title, count, viewed, updated in (
            (100, None, None, None, None),
            (101, "Dune", None, None, "2025-06-01 10:00:00"),
            (102, "Arrival", 4, "2026-07-01 08:00:00", "2026-07-02 09:00:00"),
        ):
            con.execute(
                "INSERT INTO watched_titles (user_id, section_key, rating_key, media_type, title, watch_count,"
                " viewed_at, updated_at) VALUES (3, '1', ?, 'movie', ?, ?, ?, ?)",
                (rating_key, title, count, viewed, updated),
            )
        con.execute("INSERT INTO watch_sync_state (user_id, section_key, item_count) VALUES (3, '1', NULL)")
        con.execute("INSERT INTO watch_sync_state (user_id, section_key, item_count) VALUES (3, '2', 42)")
        con.commit()
        con.close()

    def test_the_columns_really_were_nullable_before_0053_ran(self, tmp_path: Path):
        """Without this the tightening test could pass against a schema that was already correct —
        which is exactly how this repo once shipped a migration that was a no-op on every real DB."""
        self._seed_at_0052(tmp_path)

        flags = _not_null(tmp_path, "run_log_lines")
        assert not any(flags[column] for column in TIGHTENED["run_log_lines"])

    def test_existing_nulls_are_backfilled_and_populated_rows_are_untouched(self, tmp_path: Path):
        self._seed_at_0052(tmp_path)

        run_migrations(tmp_path)

        con = sqlite3.connect(tmp_path / "shortlist.db")
        log = dict(
            zip(
                ("ts", "user_slug", "stage", "counts", "reason", "level"),
                con.execute(
                    "SELECT ts, user_slug, stage, counts, reason, level FROM run_log_lines WHERE seq = 1"
                ).fetchone(),
                strict=True,
            )
        )
        kept = con.execute(
            "SELECT ts, user_slug, stage, counts, reason, level FROM run_log_lines WHERE seq = 2"
        ).fetchone()
        titles = {
            row[0]: row[1:]
            for row in con.execute("SELECT rating_key, title, watch_count, viewed_at FROM watched_titles")
        }
        state = dict(con.execute("SELECT section_key, item_count FROM watch_sync_state"))
        con.close()

        # A line's timestamp comes from its run, not from this boot: "now" would date a three-month-old
        # line to whenever the upgrade happened.
        assert log["ts"] == "2026-01-02 03:04:05"
        assert (log["user_slug"], log["stage"], log["counts"], log["reason"], log["level"]) == (
            "",
            "",
            "{}",
            "",
            "info",
        )
        assert kept == ("2026-01-02 03:05:06.000007", "bob", "deliver", '{"added": 3}', "built", "warning")

        assert titles[100] == ("", 1, "1970-01-01 00:00:00")  # nothing known -> the epoch, never "now"
        assert titles[101] == ("Dune", 1, "2025-06-01 10:00:00")  # dated from updated_at
        assert titles[102] == ("Arrival", 4, "2026-07-01 08:00:00")  # untouched
        assert state == {"1": 0, "2": 42}

    def test_a_null_is_rejected_afterwards_and_the_indexes_survived_the_rebuild(self, tmp_path: Path):
        """Backfilled data alone would pass even if the ALTER silently did nothing, and a batch
        rebuild that dropped `uq_watched_title` would let the cache double every title."""
        self._seed_at_0052(tmp_path)

        run_migrations(tmp_path)

        con = sqlite3.connect(tmp_path / "shortlist.db")
        con.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO run_log_lines (run_id, seq, ts) VALUES (7, 99, NULL)")
        con.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO watched_titles (user_id, section_key, rating_key, media_type, title)"
                " VALUES (3, '1', 999, 'movie', NULL)"
            )
        con.rollback()

        indexes = sorted(row[1] for row in con.execute("PRAGMA index_list(watched_titles)"))
        cascades = {row[2]: row[6] for row in con.execute("PRAGMA foreign_key_list(watched_titles)")}
        con.close()

        assert "ix_watched_titles_user_viewed" in indexes
        assert "sqlite_autoindex_watched_titles_1" in indexes  # uq_watched_title
        assert cascades == {"users": "CASCADE"}

    def test_the_downgrade_widens_the_columns_again(self, tmp_path: Path):
        self._seed_at_0052(tmp_path)
        run_migrations(tmp_path)

        command.downgrade(_alembic(tmp_path), "0052")

        flags = _not_null(tmp_path, "watched_titles")
        assert not any(flags[column] for column in TIGHTENED["watched_titles"])


class TestThePreMigrationBackup:
    """A backup is insurance against a migration. Taking one on EVERY boot meant ten restarts — a
    crash loop, or a week of `docker restart` — evicted all ten retained backups (rotation keeps 10)
    and replaced them with ten copies of the already-broken state."""

    @staticmethod
    def _backups(config_dir: Path) -> list[str]:
        return sorted(p.name for p in (config_dir / "backups").glob("*.db"))

    def test_restarting_with_nothing_to_migrate_takes_no_backup(self, tmp_path: Path):
        run_migrations(tmp_path)

        for _ in range(10):
            run_migrations(tmp_path)

        assert self._backups(tmp_path) == []

    def test_a_pending_migration_still_takes_one(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0052")

        run_migrations(tmp_path)

        assert len(self._backups(tmp_path)) == 1
        assert self._backups(tmp_path)[0].endswith("_pre-migration.db")
