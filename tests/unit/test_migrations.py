"""The migrated schema must BE the schema the ORM declares, and boot must not churn the backups.

Every other test builds its database with `Base.metadata.create_all`, so the test schema is whatever
the ORM says regardless of what the migrations actually did. That is how 0049 and 0050 shipped eleven
columns nullable that the ORM calls NOT NULL: the test database was stricter than every real one, and
a NULL production accepted was unreachable in a test.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
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


def _write_setting(config_dir: Path, key: str, value) -> None:
    """Write a setting exactly as the app does — the `{"v": ...}` envelope `SettingsStore` reads
    back, and the NOT NULL `updated_at` its ORM default fills in. A bare value here would test a
    shape the product never writes — the "fake must be no easier than the real server" rule."""
    con = sqlite3.connect(config_dir / "shortlist.db")
    try:
        con.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps({"v": value}), datetime.now(UTC).isoformat(sep=" ")),
        )
        con.commit()
    finally:
        con.close()


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


class TestUpgradingAnOldInstall:
    """A database that has been running since an early beta must reach head with its data intact.

    Every other test in this file starts empty, so they prove the SCHEMA arrives correctly and say
    nothing about the rows already in it. The failure this guards is the one that only ever happens
    to other people: a `batch_alter_table` rebuild that silently drops rows, a NOT NULL added over a
    column real data left NULL, a FK rebuild that loses its parent. Nobody upgrading from 1.0 onward
    is on a fresh database, and 1.0 is the version people jump to from an old beta.
    """

    #: The oldest revision a real install can be sitting on. `0001` is the squashed initial schema,
    #: so this walks the whole published chain.
    OLDEST = "0001"

    def _seed_at_oldest(self, config_dir: Path) -> None:
        """Bring a database up to the oldest revision and put representative rows in it."""
        cfg = _alembic(config_dir)
        command.upgrade(cfg, self.OLDEST)
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            # Only the tables 0001 created, and only the columns it declared — anything newer would
            # be testing the destination rather than the journey.
            cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
            assert "username" in cols, "0001 is not the schema this test assumes"
            # Every NOT NULL column 0001 declared, spelled out — a partial insert would fail on
            # the constraint rather than on anything this test is about.
            con.execute(
                "INSERT INTO users (plex_account_id, username, slug, avatar_url, user_type, enabled,"
                " cold_start, label, request_tag, prefs)"
                " VALUES (100, 'sarah', 'sarah', '', 'shared', 1, 0, 'shortlist_sarah', 'sarah', '{}')"
            )
            con.execute(
                "INSERT INTO settings (key, value, updated_at)"
                " VALUES ('plex.url', '\"http://pms:32400\"', '2026-01-01 00:00:00')"
            )
            con.commit()
        finally:
            con.close()

    def test_an_early_beta_database_reaches_head_with_its_rows_intact(self, tmp_path: Path):
        self._seed_at_oldest(tmp_path)

        run_migrations(tmp_path)

        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            users = con.execute("SELECT username, slug, plex_account_id FROM users").fetchall()
            settings = con.execute("SELECT value FROM settings WHERE key = 'plex.url'").fetchall()
            columns = {row[1] for row in con.execute("PRAGMA table_info(users)")}
        finally:
            con.close()
        # Proves the chain was actually walked rather than the database sitting where it was seeded:
        # `nickname` arrives in 0030 and `restricted` in 0041, neither of which exists at 0001.
        assert {"nickname", "restricted"} <= columns, "the upgrade did not run past the seeded revision"
        assert users == [("sarah", "sarah", 100)], "the upgrade lost or altered an existing user"
        assert settings == [('"http://pms:32400"',)], "the upgrade lost an existing setting"

    def test_an_upgraded_old_database_has_no_drift_from_the_models(self, tmp_path: Path):
        """The same no-drift guarantee the fresh path gets.

        A migration can be written so it produces the right schema from empty and a subtly different
        one from an existing database — a `batch_alter_table` that reflects what is actually there
        rather than what the migration assumes. This is the only test that would catch that.
        """
        self._seed_at_oldest(tmp_path)

        run_migrations(tmp_path)

        assert _diffs(tmp_path) == []


class TestRecencyDefaultAppliesToEveryInstall:
    """`recommendations.recency` defaults to 0.5 for EVERY install, new or existing.

    No migration is involved, and that is the point worth pinning. An earlier pair of migrations
    pinned 0.0 for servers already in use so an upgrade could not re-rank them; the owner chose the
    opposite (2026-08-11) — age-blind ranking is the behaviour the setting exists to correct, so
    every server adopts the corrected default and opts OUT with the slider. With nothing to pin, the
    `DEFAULTS` entry does the whole job, and a database migrated to head must carry no row for the
    key at all.
    """

    def _value(self, config_dir: Path) -> tuple[bool, object]:
        """(has_row, effective value) — a stored row would mean something is pinning this again."""
        from shortlist.server.db.session import make_session_factory
        from shortlist.server.services.secrets import SecretBox
        from shortlist.server.settings_store import SettingsStore

        sessions = make_session_factory(make_engine(config_dir))
        with sessions() as session:
            store = SettingsStore(session, SecretBox(config_dir))
            return store.has_row("recommendations.recency"), store.get("recommendations.recency")

    def test_a_fresh_install_gets_it(self, tmp_path: Path):
        run_migrations(tmp_path)

        assert self._value(tmp_path) == (False, 0.5)

    def test_a_server_already_in_use_gets_it_too(self, tmp_path: Path):
        """Nothing may write a row behind the owner's back: an upgraded server has to reach the same
        effective value a new one does, or the default silently means two different things."""
        command.upgrade(_alembic(tmp_path), "0061")
        _write_setting(tmp_path, "setup.completed", True)

        run_migrations(tmp_path)

        has_row, value = self._value(tmp_path)
        assert not has_row, "a migration is pinning this again — an upgrade must not stash a value"
        assert value == 0.5

    def test_an_explicit_choice_is_never_overwritten(self, tmp_path: Path):
        """Someone who turned it down keeps exactly what they chose, upgrade or not."""
        command.upgrade(_alembic(tmp_path), "0061")
        _write_setting(tmp_path, "recommendations.recency", 0.0)

        run_migrations(tmp_path)

        assert self._value(tmp_path) == (True, 0.0)


class TestDroppingTheAutoWebSearchBackend:
    """0063 pins every install to the backend it was actually using.

    `auto` was the stored default, so almost every real database holds it (or no row at all, which
    reads as the same thing). Getting the mapping wrong doesn't error — it silently moves a server
    onto a backend it never chose, or leaves a value no validator accepts.
    """

    @staticmethod
    def _provider(config_dir: Path):
        from shortlist.server.db.session import make_session_factory
        from shortlist.server.services.secrets import SecretBox
        from shortlist.server.settings_store import SettingsStore

        sessions = make_session_factory(make_engine(config_dir))
        with sessions() as session:
            return SettingsStore(session, SecretBox(config_dir)).get("llm_web.search_provider")

    def test_a_fresh_install_defaults_to_the_providers_own_search(self, tmp_path: Path):
        run_migrations(tmp_path)
        assert self._provider(tmp_path) == "native"

    def test_a_configured_searxng_wins(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "llm_web.search_provider", "auto")
        _write_setting(tmp_path, "searxng.url", "http://searx.local:8080")

        run_migrations(tmp_path)

        assert self._provider(tmp_path) == "searxng"

    def test_a_configured_exa_key_wins_when_there_is_no_searxng(self, tmp_path: Path):
        """The key is written ENCRYPTED, which is the only shape a real database holds — `exa.apikey`
        is a SECRET_KEY, so production stores a Fernet token, never plaintext. The migration only
        tests it for non-emptiness, and this is what proves that holds against the real shape rather
        than a friendlier one (0032 was a no-op on every real database for exactly this reason)."""
        from shortlist.server.services.secrets import SecretBox

        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "llm_web.search_provider", "auto")
        _write_setting(tmp_path, "exa.apikey", SecretBox(tmp_path).encrypt("exa-key"))

        run_migrations(tmp_path)

        assert self._provider(tmp_path) == "exa"

    def test_searxng_beats_exa_when_both_are_set_up(self, tmp_path: Path):
        """Auto's own tie-break, kept: the free local one, so an upgrade never starts a bill."""
        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "llm_web.search_provider", "auto")
        _write_setting(tmp_path, "exa.apikey", "exa-key")
        _write_setting(tmp_path, "searxng.url", "http://searx.local:8080")

        run_migrations(tmp_path)

        assert self._provider(tmp_path) == "searxng"

    def test_an_install_with_no_external_backend_falls_back_to_native(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "llm_web.search_provider", "auto")

        run_migrations(tmp_path)

        assert self._provider(tmp_path) == "native"

    def test_an_install_that_never_wrote_the_row_is_pinned_too(self, tmp_path: Path):
        """No row meant "the default", and the default WAS auto — so these installs need pinning as
        much as the explicit ones, or they silently land on the new default instead."""
        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "exa.apikey", "exa-key")

        run_migrations(tmp_path)

        assert self._provider(tmp_path) == "exa"

    def test_an_explicit_choice_is_never_rewritten(self, tmp_path: Path):
        """Someone who already picked a backend by name keeps it, whatever else is configured."""
        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "llm_web.search_provider", "native")
        _write_setting(tmp_path, "searxng.url", "http://searx.local:8080")

        run_migrations(tmp_path)

        assert self._provider(tmp_path) == "native"

    def test_the_downgrade_leaves_a_choice_it_never_made(self, tmp_path: Path):
        """An explicit `native` was legal on 0062 too, so it belongs to the owner. Deleting it sent
        the install back to 0062's `auto` default — which, with an Exa key on file, silently resumed
        Exa searches and billing on the next run."""
        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "llm_web.search_provider", "native")
        _write_setting(tmp_path, "exa.apikey", "exa-key")
        run_migrations(tmp_path)

        command.downgrade(_alembic(tmp_path), "0062")

        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            row = con.execute("SELECT value FROM settings WHERE key = 'llm_web.search_provider'").fetchone()
        finally:
            con.close()
        assert row is not None and json.loads(row[0])["v"] == "native"

    def test_the_downgrade_does_clear_what_the_upgrade_pinned(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0062")
        _write_setting(tmp_path, "llm_web.search_provider", "auto")
        _write_setting(tmp_path, "searxng.url", "http://searx.local:8080")
        run_migrations(tmp_path)

        command.downgrade(_alembic(tmp_path), "0062")

        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            row = con.execute("SELECT value FROM settings WHERE key = 'llm_web.search_provider'").fetchone()
        finally:
            con.close()
        assert row is None  # back to 0062's own default


class TestFreshnessBecomesADayCount:
    """0065 replaces the 0..1 `freshness` fraction with `refresh_days`, the cadence itself.

    The one thing this migration must not do is change how often anybody's row rebuilds. The
    conversion therefore runs every stored value through the very curve the engine applied to it the
    night before (`round(1 + (1 - f) * 13)`), and these pin the cells that curve actually lands on —
    including the two ends, where "frozen" and "nightly" are exact rather than approximate.

    The maintainer's own server sat at 0.55 -> 7 days; verified against a copy of that database
    before release, and `test_the_maintainers_stored_value_keeps_its_cadence` is that case in the
    suite so it cannot regress unnoticed.
    """

    @staticmethod
    def _days(config_dir: Path):
        from shortlist.server.db.session import make_session_factory
        from shortlist.server.services.secrets import SecretBox
        from shortlist.server.settings_store import SettingsStore

        sessions = make_session_factory(make_engine(config_dir))
        with sessions() as session:
            return SettingsStore(session, SecretBox(config_dir)).get("recommendations.refresh_days")

    @pytest.mark.parametrize(
        ("freshness", "days", "why"),
        [
            (0.0, 0, "frozen stays frozen — 0 is a choice, not an absent value"),
            (1.0, 1, "nightly stays nightly"),
            (0.5, 8, "the OLD default resolved to 8, so the new default must be 8, not a tidy 7"),
            (0.55, 7, "the maintainer's own server"),
            (0.25, 11, "a slow row keeps its pace"),
        ],
    )
    def test_every_stored_fraction_keeps_the_cadence_it_already_had(
        self, tmp_path: Path, freshness: float, days: int, why: str
    ):
        command.upgrade(_alembic(tmp_path), "0064")
        _write_setting(tmp_path, "recommendations.freshness", freshness)

        run_migrations(tmp_path)

        assert self._days(tmp_path) == days, why

    def test_the_maintainers_stored_value_keeps_its_cadence(self, tmp_path: Path):
        """0.55 was live on the maintainer's server and resolved to 7 days. Called out separately
        from the matrix because it is the one value a real upgrade was observed to convert."""
        command.upgrade(_alembic(tmp_path), "0064")
        _write_setting(tmp_path, "recommendations.freshness", 0.55)

        run_migrations(tmp_path)

        assert self._days(tmp_path) == 7

    def test_an_install_that_never_set_it_lands_on_the_same_cadence_it_had(self, tmp_path: Path):
        """No stored row means the OLD default (0.5) was in force, which meant 8 days. The new
        default has to be 8 for that server to see no change — a tidier-looking 7 would quietly
        speed up every row on every install that never touched the setting."""
        run_migrations(tmp_path)

        assert self._days(tmp_path) == 8

    def test_a_per_row_override_is_converted_too(self, tmp_path: Path):
        """The column, not just the global. A row carrying its own fraction must come out carrying
        the day count that fraction meant."""
        command.upgrade(_alembic(tmp_path), "0064")
        with make_engine(tmp_path).begin() as conn:
            conn.execute(sa.text("UPDATE collections SET freshness = 0.25 WHERE slug = 'picked'"))

        run_migrations(tmp_path)

        with make_engine(tmp_path).begin() as conn:
            days = conn.execute(sa.text("SELECT refresh_days FROM collections WHERE slug = 'picked'")).scalar()
        assert days == 11

    def test_a_row_that_inherits_still_inherits(self, tmp_path: Path):
        """NULL means "use the global" and is NOT the same as 0 ("frozen"). Converting it to a number
        would silently pin every inheriting row to whatever the global happened to be that night."""
        command.upgrade(_alembic(tmp_path), "0064")
        with make_engine(tmp_path).begin() as conn:
            conn.execute(sa.text("UPDATE collections SET freshness = NULL WHERE slug = 'picked'"))

        run_migrations(tmp_path)

        with make_engine(tmp_path).begin() as conn:
            days = conn.execute(sa.text("SELECT refresh_days FROM collections WHERE slug = 'picked'")).scalar()
        assert days is None

    def test_the_downgrade_reproduces_the_same_cadence(self, tmp_path: Path):
        """The conversion is many-to-one (0.55 and 0.58 both meant 7 days), so the downgrade cannot
        return the exact original — it returns a fraction that resolves to the SAME day count, which
        is the only thing the engine ever read from it. Round-tripping must therefore be a no-op in
        behaviour, not in bytes."""
        command.upgrade(_alembic(tmp_path), "0064")
        _write_setting(tmp_path, "recommendations.freshness", 0.55)
        run_migrations(tmp_path)

        command.downgrade(_alembic(tmp_path), "0064")
        run_migrations(tmp_path)

        assert self._days(tmp_path) == 7

    @pytest.mark.parametrize("days", [14, 30, 90, 365])
    def test_a_cadence_the_old_scheme_cannot_express_downgrades_to_slow_not_to_frozen(self, tmp_path: Path, days: int):
        """The half of the range the first version of this migration got wrong.

        `1 - (days-1)/13` is already 0.0 at 14 days and negative past it, and clamping that to 0.0
        lands on the OLD scheme's "never refresh once built". So every cadence the new field exists
        to make expressible — monthly, quarterly — downgraded to a permanently frozen row. The
        nearest HONEST pre-0065 value is the slowest the old scheme could say, a fortnight: being
        slowed is recoverable, being silently stopped is not.
        """
        run_migrations(tmp_path)
        _write_setting(tmp_path, "recommendations.refresh_days", days)

        command.downgrade(_alembic(tmp_path), "0064")
        run_migrations(tmp_path)

        assert self._days(tmp_path) == 14, "a slow row must come back slow, never frozen"

    def test_a_deliberately_frozen_row_still_downgrades_to_frozen(self, tmp_path: Path):
        """The other side of that clamp: 0 genuinely means "never" in BOTH schemes, so the fix above
        must not turn a pinned row into a fortnightly one."""
        run_migrations(tmp_path)
        _write_setting(tmp_path, "recommendations.refresh_days", 0)

        command.downgrade(_alembic(tmp_path), "0064")
        run_migrations(tmp_path)

        assert self._days(tmp_path) == 0


class TestRunsBeganAt:
    """0068 splits "when the run was asked for" from "when it actually started".

    The backfill is the load-bearing half. Without it every run already in the database reads NULL,
    which the Runs page renders as "never ran" — so the fix for one run reporting nine minutes it
    never worked would have told the same lie about the entire history, including last night's
    successful nightly.
    """

    @staticmethod
    def _seed_at_0067(config_dir: Path) -> None:
        command.upgrade(_alembic(config_dir), "0067")
        con = sqlite3.connect(config_dir / "shortlist.db")

        def insert(table: str, **values) -> None:
            row = dict(values)
            for _cid, name, type_, notnull, default, pk in con.execute(f"PRAGMA table_info({table})"):
                if notnull and default is None and not pk and name not in row:
                    row[name] = 0 if type_.upper().startswith(("INT", "BOOL", "FLOAT", "NUM")) else ""
            columns = ", ".join(row)
            con.execute(f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

        # One of each status a finished run can carry, all from before the column existed.
        insert("runs", id=1, trigger="schedule", started_at="2026-01-02 03:04:05", status="ok", stats="{}")
        insert("runs", id=2, trigger="manual", started_at="2026-01-02 04:04:05", status="error", stats="{}")
        insert("runs", id=3, trigger="manual", started_at="2026-01-02 05:04:05", status="aborted", stats="{}")
        con.commit()
        con.close()

    def _began(self, config_dir: Path) -> dict[int, str | None]:
        con = sqlite3.connect(config_dir / "shortlist.db")
        rows = dict(con.execute("SELECT id, began_at FROM runs").fetchall())
        con.close()
        return rows

    def test_the_column_really_did_not_exist_before_0068(self, tmp_path: Path):
        """Or the backfill test could pass against a schema that already had it — how this repo once
        shipped a migration that was a no-op on every real database."""
        self._seed_at_0067(tmp_path)

        con = sqlite3.connect(tmp_path / "shortlist.db")
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        con.close()
        assert "began_at" not in columns

    def test_runs_that_finished_keep_a_duration_and_aborted_ones_do_not(self, tmp_path: Path):
        self._seed_at_0067(tmp_path)

        run_migrations(tmp_path)

        began = self._began(tmp_path)
        assert began[1] == "2026-01-02 03:04:05", "a completed run must not start reading 'never ran'"
        assert began[2] == "2026-01-02 04:04:05", "an errored run reached the engine too"
        assert began[3] is None, (
            "'aborted' covers both cancelled-while-queued and cancelled-mid-run; claiming the queue "
            "wait as work for those is the bug this migration exists to fix"
        )

    def test_the_backfill_never_overwrites_a_real_stamp_on_a_replay(self, tmp_path: Path):
        """`test_every_revision_is_re_runnable_after_a_crash` replays 0068, and by then real runs may
        have stamped themselves. `WHERE began_at IS NULL` is what keeps a replay a no-op."""
        self._seed_at_0067(tmp_path)
        run_migrations(tmp_path)
        con = sqlite3.connect(tmp_path / "shortlist.db")
        con.execute("UPDATE runs SET began_at = '2026-05-05 05:05:05' WHERE id = 1")
        con.commit()
        con.close()

        command.stamp(_alembic(tmp_path), "0067")
        run_migrations(tmp_path)

        assert self._began(tmp_path)[1] == "2026-05-05 05:05:05"


class TestRowFallbackName:
    """0070 — a row's name for someone whose own name cannot be filled in (issue #84)."""

    def test_0070_adds_the_column_and_backfills_nothing(self, tmp_path: Path):
        """No backfill, on purpose, and this test is the record of why.

        The first version copied the global row-name template into every `{top_seed}` row so those
        people's rows kept appearing. That one decision produced every serious defect in this change:
        it matched the wrong column (the Rows page stores a template in `name`), it wrote values
        `render_row_name` discards, it could not see per-user overrides, and the global template IS
        the default row's title — so it gave two of a person's rows one name in one library, the exact
        collision the change exists to prevent. Guessing for the operator was the problem. The editor
        asks them instead, and a row nobody has named simply is not built for people who cannot be
        named — their existing collection is left alone, not deleted.
        """
        # The fixture matters: a fresh DB has no `row.name_template` row in `settings` and no row
        # carrying `{top_seed}`, so the DELETED backfill would have matched nothing and this test
        # would pass against it too — pinning nothing. Build the state it WOULD have acted on.
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0069")
        _write_setting(tmp_path, "row.name_template", "Spécifiquement pour le grand {user}")
        con = sqlite3.connect(tmp_path / "shortlist.db")
        con.execute("UPDATE collections SET name = 'Parce que vous avez regardé {top_seed}' WHERE slug = 'picked'")
        con.commit()
        con.close()
        command.upgrade(_alembic(tmp_path), "0070")

        flags = _not_null(tmp_path, "collections")
        assert "fallback_name" in flags
        assert flags["fallback_name"] is False

        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            values = [row[0] for row in con.execute("SELECT fallback_name FROM collections")]
        finally:
            con.close()
        assert all(v is None for v in values), f"0070 must invent nothing, found {values}"


class TestRunUsersCost:
    def test_0069_adds_nullable_cost_to_run_users(self, tmp_path: Path):
        """Nullable with NO backfill: a legacy run has no per-row cost and must read 'not recorded'.
        Backfilling 0 would claim every historical row took no time — the same class of confidently
        wrong answer this feature exists to remove."""
        run_migrations(tmp_path)

        flags = _not_null(tmp_path, "run_users")
        assert "cost" in flags
        assert flags["cost"] is False


class TestPickFinishedAtBackfill:
    """0072 adds `picks.finished_at` and backfills FILMS from `watched_at`.

    This repo has shipped a migration that was a no-op on every real database (0032), so a backfill
    is asserted against rows that existed BEFORE the upgrade, never against a fresh schema — a test
    that inserts after the fact would pass even if the UPDATE never ran.
    """

    def _seed_pick(self, config_dir: Path, *, pick_id: int, media_type: str, watched: str | None) -> None:
        with sqlite3.connect(config_dir / "shortlist.db") as db:
            db.execute(
                "INSERT INTO picks (id, user_id, tmdb_id, media_type, rating_key, rank, collection_slug, "
                "section_key, library, title, reason, sources, affinity, created_at, watched_at) "
                "VALUES (?, 1, 500, ?, 9, 1, 'picked', '1', 'Movies', 'T', '', '', 1.0, "
                "'2026-07-01 00:00:00', ?)",
                (pick_id, media_type, watched),
            )

    def _finished(self, config_dir: Path, pick_id: int):
        with sqlite3.connect(config_dir / "shortlist.db") as db:
            return db.execute("SELECT finished_at FROM picks WHERE id = ?", (pick_id,)).fetchone()[0]

    def test_a_watched_film_is_backfilled_with_its_own_watch_time(self, tmp_path: Path):
        """A film's watched flag IS completion, so copying it is exact rather than a guess."""
        command.upgrade(_alembic(tmp_path), "0071")
        self._seed_pick(tmp_path, pick_id=1, media_type="movie", watched="2026-07-10 09:30:00")

        run_migrations(tmp_path)

        assert self._finished(tmp_path, 1) == "2026-07-10 09:30:00"

    def test_a_watched_series_is_left_empty_to_fill_in_going_forward(self, tmp_path: Path):
        """We know which shows are past the bar today but not WHEN they crossed it, and inventing
        that date would file old watches in the wrong week of the trend chart permanently."""
        command.upgrade(_alembic(tmp_path), "0071")
        self._seed_pick(tmp_path, pick_id=2, media_type="show", watched="2026-07-10 09:30:00")

        run_migrations(tmp_path)

        assert self._finished(tmp_path, 2) is None

    def test_an_unwatched_film_stays_empty(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0071")
        self._seed_pick(tmp_path, pick_id=3, media_type="movie", watched=None)

        run_migrations(tmp_path)

        assert self._finished(tmp_path, 3) is None


class TestManageSharingDefaultsToManaged:
    """0073 adds `users.manage_sharing`, and its DEFAULT is the whole safety story.

    Every account on an upgrading server must keep having its share filters managed. A column that
    landed as 0/NULL would, on the first run after the upgrade, strip Shortlist's excludes from
    EVERY account — the entire server's row privacy, undone by a migration nobody was watching.
    Asserted against a user row that existed BEFORE the upgrade, because this repo has shipped a
    migration that was a no-op on every real database (0032).
    """

    def _seed_user(self, config_dir: Path) -> None:
        with sqlite3.connect(config_dir / "shortlist.db") as db:
            db.execute(
                "INSERT INTO users (id, plex_account_id, username, slug, avatar_url, nickname, friendly_name,"
                " user_type, restricted, restriction_profile, enabled, cold_start, label, request_tag, prefs)"
                " VALUES (7, 900, 'kid', 'kid', '', '', '', 'managed', 1, '', 1, 0, 'shortlist_kid', '', '{}')"
            )

    def test_an_existing_account_is_still_managed_after_the_upgrade(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0072")
        self._seed_user(tmp_path)

        run_migrations(tmp_path)

        with sqlite3.connect(tmp_path / "shortlist.db") as db:
            assert db.execute("SELECT manage_sharing FROM users WHERE id = 7").fetchone()[0] == 1

    def test_the_column_is_not_nullable_so_no_account_is_ever_undecided(self, tmp_path: Path):
        run_migrations(tmp_path)

        with sqlite3.connect(tmp_path / "shortlist.db") as db:
            column = next(c for c in db.execute("PRAGMA table_info(users)") if c[1] == "manage_sharing")
        assert column[3] == 1  # notnull
        assert column[4].strip("'") == "1"  # server default


class TestPerRowRequestSettings0074:
    """0074 adds this row's own Sonarr/Radarr floors and target, plus the row slug on a queued request.

    Every column is nullable with no backfill, because NULL is what "inherit the global setting"
    means — the same convention `watched_pct` and `cold_start` already use. That is what makes the
    upgrade a no-op on a live server: nothing changes until the owner sets an override.
    """

    _ROW_COLUMNS = (
        "req_min_rating",
        "req_min_votes",
        "req_min_demand",
        "req_min_year",
        "req_max_year",
        "req_auto_send",
        "req_auto_min_demand",
        "req_auto_min_rating",
        "req_max_per_row",
        "req_radarr_quality_profile_id",
        "req_radarr_root_folder",
        "req_sonarr_quality_profile_id",
        "req_sonarr_root_folder",
    )

    @staticmethod
    def _columns(config_dir: Path, table: str) -> dict[str, bool]:
        """column -> whether it is NOT NULL."""
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            return {r[1]: bool(r[3]) for r in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()

    def test_every_new_column_is_nullable_so_an_existing_row_inherits(self, tmp_path: Path):
        run_migrations(tmp_path)
        collections = self._columns(tmp_path, "collections")
        for name in self._ROW_COLUMNS:
            assert name in collections, f"{name} missing after upgrade"
            assert not collections[name], f"{name} must be nullable — NULL is how a row inherits"

    def test_the_seeded_default_row_reads_null_for_every_override(self, tmp_path: Path):
        """The upgrade must not opt the existing row into anything. A server that upgrades tonight
        keeps filing everything exactly where it filed it yesterday."""
        run_migrations(tmp_path)
        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            cols = ", ".join(self._ROW_COLUMNS)
            rows = con.execute(f"SELECT {cols} FROM collections").fetchall()
        finally:
            con.close()
        assert rows, "expected the seeded default row"
        for row in rows:
            assert set(row) == {None}

    def test_a_queued_request_carries_a_nullable_row_slug(self, tmp_path: Path):
        """NULL on everything queued before per-row settings existed; the approve path falls back to
        the global config for those, which is what they were queued under anyway."""
        run_migrations(tmp_path)
        candidates = self._columns(tmp_path, "request_candidates")
        assert "row_slug" in candidates
        assert not candidates["row_slug"]

    def test_the_downgrade_removes_them_again(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0073")
        collections = self._columns(tmp_path, "collections")
        assert not [name for name in self._ROW_COLUMNS if name in collections]
        assert "row_slug" not in self._columns(tmp_path, "request_candidates")


class TestRowAutoUserTag0075:
    """0075 adds the per-row override for tagging requests with the wanting person's slug.

    Nullable, no backfill, no server default — NULL is "inherit the global switch". A FALSE backfill
    would pin every existing row to "off", and the global switch would then reach none of them.
    """

    @staticmethod
    def _columns(config_dir: Path) -> dict[str, bool]:
        """column -> whether it is NOT NULL."""
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            return {r[1]: bool(r[3]) for r in con.execute("PRAGMA table_info(collections)")}
        finally:
            con.close()

    def test_the_column_is_nullable_and_the_seeded_row_inherits(self, tmp_path: Path):
        run_migrations(tmp_path)
        columns = self._columns(tmp_path)
        assert "req_auto_user_tag" in columns
        assert not columns["req_auto_user_tag"], "NULL is how a row inherits the global switch"
        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            rows = con.execute("SELECT req_auto_user_tag FROM collections").fetchall()
        finally:
            con.close()
        assert rows, "expected the seeded default row"
        assert {r[0] for r in rows} == {None}

    def test_the_downgrade_removes_it_again(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0074")
        assert "req_auto_user_tag" not in self._columns(tmp_path)


class TestRowSonarrMonitor0084:
    """0084 adds the per-row override for how much of a show Sonarr takes (issue #100).

    Nullable, no backfill, no server default — NULL is "inherit `requests.sonarr.monitor`", whose own
    default is "all". Anything else would pin every existing row to a mode its owner never chose, on
    the one setting that decides how much gets downloaded.
    """

    @staticmethod
    def _columns(config_dir: Path) -> dict[str, bool]:
        """column -> whether it is NOT NULL."""
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            return {r[1]: bool(r[3]) for r in con.execute("PRAGMA table_info(collections)")}
        finally:
            con.close()

    def test_the_column_is_nullable_and_the_seeded_row_inherits(self, tmp_path: Path):
        run_migrations(tmp_path)
        columns = self._columns(tmp_path)
        assert "req_sonarr_monitor" in columns
        assert not columns["req_sonarr_monitor"], "NULL is how a row inherits the global mode"
        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            rows = con.execute("SELECT req_sonarr_monitor FROM collections").fetchall()
        finally:
            con.close()
        assert rows, "expected the seeded default row"
        assert {r[0] for r in rows} == {None}

    def test_running_it_again_over_an_already_migrated_database_is_a_no_op(self, tmp_path: Path):
        """The maintainer's server runs `dev` builds, so this migration lands on databases that may
        already carry the column from an earlier build of the same change."""
        run_migrations(tmp_path)
        run_migrations(tmp_path)
        assert "req_sonarr_monitor" in self._columns(tmp_path)

    def test_the_downgrade_removes_it_again(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0083")
        assert "req_sonarr_monitor" not in self._columns(tmp_path)


class TestSharedPickMediaTypeBackfill0081:
    """0081 puts `media_type` back on shared-row picks written before `_pick_dicts` carried it.

    Without it `_shared_key` refuses the entry — correctly, since TMDB ids are namespaced per type —
    so every watch off a shared row before that fix credits nothing. Measured on a real server: 28
    plays by five people, resolving to 9 credits the dashboard could not see.
    """

    @staticmethod
    def _seed(config_dir: Path, picks: list[dict], pick_rows: list[tuple]) -> None:
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            con.execute(
                "INSERT INTO runs (id, trigger, started_at, finished_at, status, dry_run, stats)"
                " VALUES (900, 'schedule', '2026-08-23 17:30:00', '2026-08-23 18:58:00', 'ok', 0, '{}')"
            )
            con.execute(
                "INSERT INTO run_shared_rows (run_id, collection_slug, row_title, status, picks) "
                "VALUES (900, 'staff', 'Staff', 'ok', ?)",
                (json.dumps(picks),),
            )
            for rating_key, tmdb_id, media_type in pick_rows:
                con.execute(
                    "INSERT INTO picks (run_id, user_id, collection_slug, section_key, library, "
                    "tmdb_id, media_type, rating_key, rank, title, reason, created_at) "
                    "VALUES (900, 1, 'other', '1', 'Movies', ?, ?, ?, 1, 'T', '', '2026-08-23 17:30:00')",
                    (tmdb_id, media_type, rating_key),
                )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _picks_back(config_dir: Path) -> list[dict]:
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            (blob,) = con.execute(
                "SELECT picks FROM run_shared_rows WHERE run_id=900 AND collection_slug='staff'"
            ).fetchone()
        finally:
            con.close()
        return json.loads(blob)

    def _run_0081(self, tmp_path: Path, picks: list[dict], pick_rows: list[tuple]) -> list[dict]:
        command.upgrade(_alembic(tmp_path), "0080")
        self._seed(tmp_path, picks, pick_rows)
        command.upgrade(_alembic(tmp_path), "0081")
        return self._picks_back(tmp_path)

    def test_a_typed_pick_is_recovered_from_its_rating_key(self, tmp_path: Path):
        out = self._run_0081(
            tmp_path,
            [{"tmdb_id": 136315, "rating_key": 500, "title": "The Bear"}],
            [(500, 136315, "show")],
        )
        assert out[0]["media_type"] == "show", "the credit path still cannot key this pick"

    def test_a_rating_key_that_disagrees_on_the_title_is_left_alone(self, tmp_path: Path):
        """Joined on rating_key AND tmdb_id. Plex reuses `metadata_items.id`, so a rating key alone
        can point at a title that is no longer the one in the blob — and retyping from that would
        credit the wrong thing, which is the exact failure `_shared_key` refuses to risk."""
        out = self._run_0081(
            tmp_path,
            [{"tmdb_id": 136315, "rating_key": 500, "title": "The Bear"}],
            [(500, 999999, "movie")],  # same key, a DIFFERENT title
        )
        assert "media_type" not in out[0], "retyped a pick from a reused rating key"

    def test_a_pick_with_no_ids_at_all_is_left_alone(self, tmp_path: Path):
        """The oldest rows carry only title/year/rank. Nothing can recover those, and this must not
        invent an answer for them."""
        out = self._run_0081(tmp_path, [{"title": "Ancient", "year": 2019}], [(500, 136315, "show")])
        assert "media_type" not in out[0]

    def test_an_already_typed_pick_is_untouched(self, tmp_path: Path):
        """Idempotent — re-running must change nothing, and must never overwrite a real value."""
        out = self._run_0081(
            tmp_path,
            [{"tmdb_id": 136315, "rating_key": 500, "media_type": "movie"}],
            [(500, 136315, "show")],  # the table says show; the blob already says movie
        )
        assert out[0]["media_type"] == "movie", "overwrote a type the row already carried"


def test_0083_adds_the_complete_column_to_a_database_already_stamped_0082(tmp_path):
    """A database stamped 0082 never replays it, so editing 0082 fixes fresh installs and nothing else.

    Not hypothetical: an earlier 0082 shipped without `complete` and ran against the development
    server. Without 0083 every snapshot insert and every `/watching-account/snapshots` read there
    raises `no such column` — loud rather than lossy, but the feature is dead on exactly the machine
    it was built against. Same shape as 0032, a migration that was a no-op on every real database.

    This test recreates the old table shape and stamps the DB back to 0082, which is the only way to
    reproduce what the live server actually looks like.
    """
    import sqlalchemy as sa

    from shortlist.server.db.session import make_engine, run_migrations

    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE watch_state_snapshots"))
            # Recreate it exactly as the pre-`complete` version of 0082 did.
            conn.execute(
                sa.text(
                    "CREATE TABLE watch_state_snapshots ("
                    "id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "taken_at DATETIME NOT NULL, job_id INTEGER, restored_at DATETIME, "
                    "state JSON NOT NULL, FOREIGN KEY(user_id) REFERENCES users (id))"
                )
            )
            # Stamped back to 0082 — the state the development server is actually in.
            conn.execute(sa.text("UPDATE alembic_version SET version_num = '0082'"))
    finally:
        engine.dispose()

    run_migrations(tmp_path)

    engine = make_engine(tmp_path)
    try:
        columns = {c["name"] for c in sa.inspect(engine).get_columns("watch_state_snapshots")}
    finally:
        engine.dispose()
    assert "complete" in columns


class TestRequestLanguagePreference0085:
    """0085 adds the per-row language overrides and the language of a queued title.

    Nullable with no backfill on the row columns — NULL is "inherit the global", whose own default is
    "any", i.e. today's behaviour. Anything else would start filtering requests on servers whose owner
    never asked for it.
    """

    @staticmethod
    def _columns(config_dir: Path, table: str) -> dict[str, bool]:
        """column -> whether it is NOT NULL."""
        con = sqlite3.connect(config_dir / "shortlist.db")
        try:
            return {r[1]: bool(r[3]) for r in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()

    def test_the_row_columns_are_nullable_and_the_seeded_row_inherits(self, tmp_path: Path):
        run_migrations(tmp_path)
        columns = self._columns(tmp_path, "collections")
        for name in ("req_language_mode", "req_preferred_languages", "req_min_rating_other"):
            assert name in columns
            assert not columns[name], f"{name}: NULL is how a row inherits the global setting"
        con = sqlite3.connect(tmp_path / "shortlist.db")
        try:
            rows = con.execute(
                "SELECT req_language_mode, req_preferred_languages, req_min_rating_other FROM collections"
            ).fetchall()
        finally:
            con.close()
        assert rows, "expected the seeded default row"
        assert {r for r in rows} == {(None, None, None)}

    def test_a_queued_title_gets_an_empty_language_not_a_null_one(self, tmp_path: Path):
        """`language` is NOT NULL with a "" default because "" is the unknown sentinel the gate and
        the inbox both test against — and a pre-0085 row genuinely IS unknown: nothing recorded it."""
        run_migrations(tmp_path)
        columns = self._columns(tmp_path, "request_candidates")
        assert "language" in columns
        assert columns["language"], "NOT NULL — the code treats '' as unknown, never None"

    def test_running_it_again_over_an_already_migrated_database_is_a_no_op(self, tmp_path: Path):
        """The maintainer's server runs `dev` builds, so this lands on databases that may already
        carry the columns from an earlier build of the same change.

        The `stamp` is what gives this test teeth. Without it the second `run_migrations` sees the
        version already at head and executes no script at all — so it would pass just as happily with
        every `if name not in existing` guard deleted. Winding the version back while LEAVING the
        columns in place is the actual shape of the problem: a database that already has them under a
        revision it no longer claims."""
        run_migrations(tmp_path)
        command.stamp(_alembic(tmp_path), "0084")
        run_migrations(tmp_path)
        assert "req_language_mode" in self._columns(tmp_path, "collections")
        assert "language" in self._columns(tmp_path, "request_candidates")

    def test_the_downgrade_removes_them_again(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0084")
        rows = self._columns(tmp_path, "collections")
        assert not ({"req_language_mode", "req_preferred_languages", "req_min_rating_other"} & set(rows))
        assert "language" not in self._columns(tmp_path, "request_candidates")
