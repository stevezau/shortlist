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
        from `models.py` shows up here — including a nullability the ORM does not claim, and a
        foreign key's `ondelete`, neither of which any other test in this repo can see.

        The `ondelete` half is what keeps `User`'s cascade policy honest in both directions: the ORM
        says RESTRICT on the three tables that are the only copy of what they hold, 0055 says
        RESTRICT in the schema, and a future migration that quietly turned one into a CASCADE (or an
        ORM edit that did) would land here as a diff rather than as a lost snapshot table.
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


class TestAnInterruptedRebuildDoesNotBrickTheContainer:
    """A crash mid-`batch_alter_table` used to make every later boot fail, for ever.

    SQLite cannot ALTER a constraint, so Alembic rebuilds: CREATE `_alembic_tmp_X`, copy, DROP,
    RENAME. Under pysqlite the CREATE autocommits (no transaction is open yet) while the rest rolls
    back — so a crash leaves the temp table committed and the migration unapplied, and the next boot
    dies on "table _alembic_tmp_X already exists". No data is lost; the app simply cannot start.

    This release rebuilds six tables where earlier ones rebuilt one, so the window is wider.
    """

    def test_a_leftover_temp_table_is_swept_instead_of_blocking_the_upgrade(self, tmp_path: Path):
        import sqlite3

        run_migrations(tmp_path)  # get to head normally
        db = tmp_path / "shortlist.db"

        # Exactly what an interrupted rebuild leaves behind.
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE _alembic_tmp_picks (id INTEGER PRIMARY KEY, marker TEXT)")
            conn.execute("INSERT INTO _alembic_tmp_picks (marker) VALUES ('half-copied')")

        run_migrations(tmp_path)  # must not raise "table _alembic_tmp_picks already exists"

        with sqlite3.connect(db) as conn:
            left = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_alembic_tmp_%'"
            ).fetchall()
            real = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        assert left == [], "the leftover must be swept, not left to block the next boot"
        assert real == 0, "the REAL table is untouched — only the abandoned copy is dropped"


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


class TestMigration0055RestrictsUserDeletes:
    """0055 rebuilds `picks`, `run_users` and `restriction_snapshots` to declare
    `ON DELETE RESTRICT` on their `users.id` foreign key.

    The rebuild is the risk, not the constraint. `restriction_snapshots` holds every user's share
    filters *as they were before Shortlist touched them* and is what uninstall restores from
    (plex-safety rule 2) — there is no second copy anywhere — so a batch rebuild that dropped a row,
    an index or a column value would be unrecoverable. Every test here seeds real-shaped rows at
    0054 and compares them across the migration.
    """

    #: A share filter worth losing sleep over: a pre-existing foreign condition Shortlist must never
    #: rebuild away, plus the exclude it merged in.
    FILTERS_BEFORE = '{"filterMovies": "contentRating!=R", "filterTelevision": ""}'
    FILTERS_AFTER = '{"filterMovies": "contentRating!=R,label!=shortlist_mike", "filterTelevision": ""}'

    TABLES = ("picks", "run_users", "restriction_snapshots")

    @classmethod
    def _seed_at_0054(cls, config_dir: Path) -> None:
        command.upgrade(_alembic(config_dir), "0054")
        con = sqlite3.connect(config_dir / "shortlist.db")
        con.execute(
            "INSERT INTO users (id, plex_account_id, username, slug, avatar_url, nickname, friendly_name,"
            " user_type, restricted, restriction_profile, enabled, cold_start, label, request_tag, prefs)"
            " VALUES (3, 555000100, 'sarah', 'sarah', '', '', '', 'shared', 0, '', 1, 0,"
            " 'shortlist_sarah', '', '{}')"
        )
        con.execute(
            "INSERT INTO runs (id, trigger, started_at, finished_at, status, dry_run, stats)"
            " VALUES (7, 'schedule', '2026-01-02 03:04:05', '2026-01-02 03:09:00', 'ok', 0, '{\"users_ok\": 1}')"
        )
        con.execute(
            "INSERT INTO run_users (run_id, user_id, status, error, reason, duration_ms, llm_tokens,"
            " llm_tokens_by_step, exa_searches, diff, breakdown, trace)"
            " VALUES (7, 3, 'ok', NULL, NULL, 4200, 1234, '{\"llm_web\": 1234}', 2,"
            ' \'{"added": ["Dune"]}\', \'[{"row_slug": "picked"}]\', \'{"seeds": [1, 2]}\')'
        )
        for pick_id, tmdb_id in ((1, 438631), (2, 693134)):
            con.execute(
                "INSERT INTO picks (id, run_id, user_id, tmdb_id, media_type, rating_key, rank,"
                " collection_slug, section_key, library, title, reason, sources, affinity, seed_tmdb_id,"
                " seed_title, created_at, watched_at) VALUES (?, 7, 3, ?, 'movie', 9001, 1, 'picked', '1',"
                " 'Movies', 'Dune', 'because', 'tmdb_similar', 0.87, 12345, 'Arrival',"
                " '2026-01-02 03:08:00', NULL)",
                (pick_id, tmdb_id),
            )
        con.execute(
            "INSERT INTO restriction_snapshots (id, user_id, taken_at, reason, filters_before, filters_after)"
            " VALUES (1, 3, '2026-01-02 03:04:10', 'initial', ?, ?)",
            (cls.FILTERS_BEFORE, cls.FILTERS_AFTER),
        )
        con.commit()
        con.close()

    @staticmethod
    def _shape(config_dir: Path) -> dict[str, dict]:
        """Every row, index and column of the three tables — what the rebuild must not change."""
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            return {
                table: {
                    "rows": con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall(),
                    "indexes": sorted(row[1] for row in con.execute(f"PRAGMA index_list({table})")),
                    "columns": [row[1:] for row in con.execute(f"PRAGMA table_info({table})")],
                }
                for table in TestMigration0055RestrictsUserDeletes.TABLES
            }
        finally:
            con.close()

    @staticmethod
    def _user_fk(config_dir: Path, table: str) -> str | None:
        """The `ON DELETE` this table declares on its `users.id` FK, as SQLite reports it."""
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            return next((row[6] for row in con.execute(f"PRAGMA foreign_key_list({table})") if row[2] == "users"), None)
        finally:
            con.close()

    def test_no_ondelete_was_declared_before_0055_ran(self, tmp_path: Path):
        """Without this the assertion below could pass against a schema that already said RESTRICT —
        which is exactly how this repo once shipped a migration that was a no-op on every real DB."""
        self._seed_at_0054(tmp_path)

        assert {t: self._user_fk(tmp_path, t) for t in self.TABLES} == dict.fromkeys(self.TABLES, "NO ACTION")

    def test_the_users_fk_declares_restrict_afterwards(self, tmp_path: Path):
        self._seed_at_0054(tmp_path)

        run_migrations(tmp_path)

        assert {t: self._user_fk(tmp_path, t) for t in self.TABLES} == dict.fromkeys(self.TABLES, "RESTRICT")

    def test_the_rebuild_keeps_every_row_value_index_and_column(self, tmp_path: Path):
        """The whole risk of 0055. `restriction_snapshots` is the only copy of a person's original
        share filters, so a rebuild that loses a row — or silently rewrites a JSON blob — is
        unrecoverable, and 'the migration ran' says nothing about that."""
        self._seed_at_0054(tmp_path)
        before = self._shape(tmp_path)

        # Stops at 0055 rather than running to head: this asserts what 0055's REBUILD preserved, and
        # a later migration that legitimately adds a column to one of these tables (0056 adds
        # `picks.rating`/`picks.year`) would otherwise fail it for the wrong reason.
        command.upgrade(_alembic(tmp_path), "0055")

        assert self._shape(tmp_path) == before
        # Named explicitly, not just covered by the dict compare above: this is the value uninstall
        # feeds back to plex.tv, and a foreign condition dropped from it is a permission change.
        con = sqlite3.connect(tmp_path / "shortlist.db")
        snapshot = con.execute(
            "SELECT user_id, reason, filters_before, filters_after FROM restriction_snapshots"
        ).fetchone()
        con.close()
        assert snapshot == (3, "initial", self.FILTERS_BEFORE, self.FILTERS_AFTER)

    @pytest.mark.parametrize("table", TABLES)
    def test_deleting_a_user_now_raises_from_each_table(self, table: str, tmp_path: Path):
        """One table at a time, so each of the three is proven to hold the delete on its own —
        together they would pass even if only one constraint had actually been rebuilt."""
        self._seed_at_0054(tmp_path)
        run_migrations(tmp_path)

        con = sqlite3.connect(tmp_path / "shortlist.db")
        con.execute("PRAGMA foreign_keys=ON")
        for other in (t for t in self.TABLES if t != table):
            con.execute(f"DELETE FROM {other}")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                con.execute("DELETE FROM users WHERE id = 3")
        finally:
            con.rollback()
            con.close()

    def test_the_downgrade_drops_the_clause_without_dropping_the_data(self, tmp_path: Path):
        """And drops it to NO ACTION, never to a CASCADE: downgrading must not be the thing that
        makes a `DELETE FROM users` able to take the restriction snapshots with it."""
        self._seed_at_0054(tmp_path)
        before = self._shape(tmp_path)
        run_migrations(tmp_path)

        command.downgrade(_alembic(tmp_path), "0054")

        assert {t: self._user_fk(tmp_path, t) for t in self.TABLES} == dict.fromkeys(self.TABLES, "NO ACTION")
        assert self._shape(tmp_path) == before


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
