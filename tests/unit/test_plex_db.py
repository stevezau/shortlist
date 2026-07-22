"""Reading watched FLAGS out of a PMS library database.

Built against a real SQLite file shaped like the production one (verified on SFLIX: 262,880
`metadata_item_settings` rows across 49 accounts), not a mock — the whole value of this source is
that it matches Plex's actual schema, which a mock can't prove.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from shortlist.engine.clients.plex_db import PlexDbReader, PlexDbUnavailable

MOVIE, SHOW, SEASON, EPISODE = 1, 2, 3, 4


# The real DDL, recorded from the live server (plex-safety rule 11) — the column sets Plex actually
# ships, not a hand-written approximation. `parent_id` is the one that matters: episode -> season ->
# show is what folds a marked season into the show key everything else is built on.
SCHEMA = (Path(__file__).parent.parent / "fixtures" / "pms_library_schema.sql").read_text()


def make_plex_db(
    tmp_path: Path,
    rows: list[tuple],
    *,
    name: str = PlexDbReader.FILENAME,
    parents: dict[int, int] | None = None,
    guids: dict[int, str] | None = None,
) -> Path:
    """A PMS library DB from the recorded schema.

    Args:
        rows: ``(account_id, rating_key, title, year, metadata_type, view_count, last_viewed_at)``.
        parents: ``child ratingKey -> parent ratingKey`` (episode->season, season->show).
        guids: ``ratingKey -> guid``, for modelling two libraries sharing one guid.
    """
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for account, rating_key, title, year, kind, views, last in rows:
        guid = (guids or {}).get(rating_key, f"plex://item/{rating_key}")
        con.execute(
            "INSERT OR IGNORE INTO metadata_items (id, guid, title, year, metadata_type, parent_id) "
            "VALUES (?,?,?,?,?,?)",
            (rating_key, guid, title, year, kind, (parents or {}).get(rating_key)),
        )
        if account is not None:
            con.execute(
                "INSERT INTO metadata_item_settings (account_id, guid, view_offset, view_count, last_viewed_at) "
                "VALUES (?,?,?,?,?)",
                (account, guid, 0, views, last),
            )
    con.commit()
    con.close()
    return path


class TestWatchedFlags:
    MOO = 218833834

    def test_it_finds_a_marked_title_the_play_history_never_saw(self, tmp_path: Path):
        """The reported bug in its exact shape: six titles flagged inside a 23-second window in
        Oct 2024 — a bulk mark, absent from that account's 11,462-play history."""
        db = make_plex_db(
            tmp_path,
            [
                (self.MOO, 9587, "Gravity", 2013, MOVIE, 1, 1728609330),
                (self.MOO, 5476, "Chronicle", 2012, MOVIE, 1, 1728609324),
            ],
        )

        watched = PlexDbReader(db).watched_for(self.MOO)

        assert {w.title for w in watched} == {"Gravity", "Chronicle"}
        assert all(w.media_type == "movie" for w in watched)
        assert watched[0].last_viewed_at is not None and watched[0].last_viewed_at.year == 2024

    def test_it_reads_one_account_and_not_another(self, tmp_path: Path):
        """Per-account is the entire point — this is the data the owner token cannot reach."""
        db = make_plex_db(
            tmp_path,
            [(self.MOO, 1, "Theirs", 2020, MOVIE, 1, 100), (999, 2, "Someone else's", 2020, MOVIE, 1, 100)],
        )

        assert [w.title for w in PlexDbReader(db).watched_for(self.MOO)] == ["Theirs"]

    def test_unwatched_and_partially_watched_rows_are_ignored(self, tmp_path: Path):
        """A row exists as soon as you press play. Only `view_count > 0` means finished."""
        db = make_plex_db(
            tmp_path,
            [(self.MOO, 1, "Watched", 2020, MOVIE, 1, 100), (self.MOO, 2, "Started only", 2020, MOVIE, 0, 100)],
        )

        assert [w.title for w in PlexDbReader(db).watched_for(self.MOO)] == ["Watched"]

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [(MOVIE, "movie"), (SHOW, "show"), (EPISODE, "show"), (SEASON, None)],
    )
    def test_the_media_type_matrix(self, tmp_path: Path, kind: int, expected: str | None):
        """Seasons carry no watchable title of their own; movies and shows map straight through."""
        db = make_plex_db(tmp_path, [(self.MOO, 1, "T", 2020, kind, 1, 100)])

        got = PlexDbReader(db).watched_for(self.MOO)

        assert [w.media_type for w in got] == ([expected] if expected else [])

    def test_a_mark_with_no_timestamp_is_still_watched(self, tmp_path: Path):
        """The filter cares THAT it was watched, not when — dropping these would reopen the bug."""
        db = make_plex_db(tmp_path, [(self.MOO, 1, "Marked", 2020, MOVIE, 1, None)])

        got = PlexDbReader(db).watched_for(self.MOO)

        assert len(got) == 1 and got[0].last_viewed_at is None

    def test_an_account_with_nothing_recorded_is_empty_not_an_error(self, tmp_path: Path):
        db = make_plex_db(tmp_path, [(self.MOO, 1, "T", 2020, MOVIE, 1, 100)])

        assert PlexDbReader(db).watched_for(12345) == []

    def test_a_directory_resolves_to_the_database_inside_it(self, tmp_path: Path):
        """People will paste the folder they mounted, not the file."""
        make_plex_db(tmp_path, [(self.MOO, 1, "T", 2020, MOVIE, 1, 100)])

        assert len(PlexDbReader(tmp_path).watched_for(self.MOO)) == 1


class TestFailureModes:
    def test_a_missing_database_says_so(self, tmp_path: Path):
        with pytest.raises(PlexDbUnavailable, match="no Plex database"):
            PlexDbReader(tmp_path / "nope.db").watched_for(1)

    def test_a_file_that_is_not_a_plex_database_explains_itself(self, tmp_path: Path):
        """Pointing at the wrong .db is the likeliest setup mistake, and a bare sqlite error code
        would tell the owner nothing about how to fix it."""
        other = tmp_path / PlexDbReader.FILENAME
        con = sqlite3.connect(other)
        con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()

        with pytest.raises(PlexDbUnavailable, match=re.escape("com.plexapp.plugins.library.db")):
            PlexDbReader(other).check()

    def test_check_reports_how_many_accounts_were_found(self, tmp_path: Path):
        db = make_plex_db(
            tmp_path,
            [(1, 1, "A", 2020, MOVIE, 1, 100), (2, 2, "B", 2020, MOVIE, 1, 100), (2, 3, "C", 2020, MOVIE, 1, 100)],
        )

        assert "2 account(s)" in PlexDbReader(db).check()


class TestItNeverWrites:
    """A live PMS database is multi-gigabyte production data. Corrupting it costs someone their whole
    library, so the read must be provably incapable of touching it."""

    def test_reading_leaves_no_journal_and_does_not_modify_the_file(self, tmp_path: Path):
        db = make_plex_db(tmp_path, [(1, 1, "T", 2020, MOVIE, 1, 100)])
        before = (db.stat().st_mtime_ns, db.stat().st_size, db.read_bytes())

        PlexDbReader(db).watched_for(1)
        PlexDbReader(db).check()

        assert (db.stat().st_mtime_ns, db.stat().st_size, db.read_bytes()) == before
        assert not list(tmp_path.glob("*-wal")), "immutable=1 must not create a write-ahead log"
        assert not list(tmp_path.glob("*-journal")), "immutable=1 must not create a rollback journal"
        assert not list(tmp_path.glob("*-shm"))

    def test_a_read_only_file_is_still_readable(self, tmp_path: Path):
        """The documented setup mounts the database read-only, so this is the normal case."""
        db = make_plex_db(tmp_path, [(1, 1, "T", 2020, MOVIE, 1, 100)])
        db.chmod(0o444)

        assert len(PlexDbReader(db).watched_for(1)) == 1


class TestEpisodesFoldIntoTheirShow:
    """The bug that made the TV half a silent no-op.

    A mark lands on the EPISODE, but both play-history sources store episodes under the show's
    ratingKey (`history.py`) and the library index only ever contains shows — so emitting an
    episode's own key matches nothing downstream. Worse, `metadata_items.title` for an episode is
    the episode NAME ("Kenia Monge", "Countdown"), which would then displace real show titles in the
    recently-watched list the AI prompt and the Users screen are built from.
    """

    MOO = 218833834
    SHOW_KEY, SEASON_KEY, EP1, EP2 = 194553, 194554, 408653, 408654

    def _marked_season(self, tmp_path: Path) -> list:
        return make_plex_db(
            tmp_path,
            [
                (None, self.SHOW_KEY, "#TextMeWhenYouGetHome", 2019, SHOW, 0, None),
                (None, self.SEASON_KEY, "Season 1", 2019, SEASON, 0, None),
                (self.MOO, self.EP1, "Kenia Monge", 2019, EPISODE, 1, 1728609330),
                (self.MOO, self.EP2, "Hannah Anderson", 2019, EPISODE, 1, 1728609336),
            ],
            parents={self.EP1: self.SEASON_KEY, self.EP2: self.SEASON_KEY, self.SEASON_KEY: self.SHOW_KEY},
        )

    def test_a_marked_season_becomes_the_show_once(self, tmp_path: Path):
        got = PlexDbReader(self._marked_season(tmp_path)).watched_for(self.MOO)

        assert [(w.rating_key, w.title) for w in got] == [(self.SHOW_KEY, "#TextMeWhenYouGetHome")]

    def test_it_never_emits_an_episode_title(self, tmp_path: Path):
        """Episode names reaching `distinct_recent` would make the AI prompt read
        'Recently watched: Kenia Monge, Hannah Anderson'."""
        got = PlexDbReader(self._marked_season(tmp_path)).watched_for(self.MOO)

        assert not {w.title for w in got} & {"Kenia Monge", "Hannah Anderson"}

    def test_an_orphaned_episode_still_counts(self, tmp_path: Path):
        """A season or show row missing from the DB must not silently drop the watch."""
        db = make_plex_db(tmp_path, [(self.MOO, self.EP1, "Orphan", 2019, EPISODE, 1, 100)])

        got = PlexDbReader(db).watched_for(self.MOO)

        assert [(w.rating_key, w.media_type) for w in got] == [(self.EP1, "show")]


class TestDuplicateGuids:
    """The same title in two libraries is one guid and two ratingKeys — this repo already reasons
    about that elsewhere. The join is one-to-many, so it fans out."""

    MOO = 218833834

    def test_one_watch_of_a_title_in_two_libraries_is_one_flag(self, tmp_path: Path):
        shared = "plex://movie/shared-guid"
        db = make_plex_db(
            tmp_path,
            [(self.MOO, 100, "Gravity", 2013, MOVIE, 1, 100), (None, 200, "Gravity", 2013, MOVIE, 0, None)],
            guids={100: shared, 200: shared},
        )

        got = PlexDbReader(db).watched_for(self.MOO)

        assert len(got) == 2, "both library copies are watched — but each exactly once"
        assert len({w.rating_key for w in got}) == 2
        assert all(w.view_count == 1 for w in got), "a fan-out would double the count"


class TestLiveWalDatabase:
    """Plex runs this database in WAL mode, so it changes while we read it.

    `immutable=1` is documented as producing "incorrect query results and/or SQLITE_CORRUPT errors"
    on a file that does in fact change — and an uncheckpointed WAL is invisible to it entirely, so a
    perfectly healthy database can read as empty. Those wrong reads would land in `watch_events` as
    permanent rows nothing ever re-examines.
    """

    def _wal_db(self, tmp_path: Path) -> Path:
        db = make_plex_db(tmp_path, [(1, 1, "Checkpointed", 2020, MOVIE, 1, 100)])
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "INSERT INTO metadata_items (id, guid, title, year, metadata_type) VALUES (2,'g2','In the WAL',2020,1)"
        )
        con.execute(
            "INSERT INTO metadata_item_settings (account_id, guid, view_offset, view_count, last_viewed_at) "
            "VALUES (1,'g2',0,1,100)"
        )
        con.commit()  # committed, but left in the WAL — exactly PMS's steady state
        return db, con

    def test_it_sees_writes_still_sitting_in_the_write_ahead_log(self, tmp_path: Path):
        """The whole failure: `immutable=1` reads 1 here, `mode=ro` reads 2."""
        db, keepalive = self._wal_db(tmp_path)
        try:
            titles = {w.title for w in PlexDbReader(db).watched_for(1)}
        finally:
            keepalive.close()

        assert titles == {"Checkpointed", "In the WAL"}, "an uncheckpointed WAL must not be invisible"

    def test_a_live_database_is_never_reported_as_not_a_plex_database(self, tmp_path: Path):
        """`check()` reading through an invisible WAL would tell the owner their real Plex database
        is the wrong file."""
        db, keepalive = self._wal_db(tmp_path)
        try:
            assert "connected" in PlexDbReader(db).check()
        finally:
            keepalive.close()


class TestPermissionDenied:
    """The commonest Docker misconfiguration for this feature (PUID/PGID vs a Plex data mount).

    `Path.is_file()` re-raises PermissionError on Python 3.12 — the container runtime, and the only
    version CI tests — while swallowing it on 3.14, which is what the local venv runs. So this must
    be asserted against the reader, not left to the environment.
    """

    def test_an_unreadable_file_is_a_clean_failure_not_a_raw_errno(self, tmp_path: Path):
        db = make_plex_db(tmp_path, [(1, 1, "T", 2020, MOVIE, 1, 100)])
        db.chmod(0o000)
        try:
            with pytest.raises(PlexDbUnavailable):
                PlexDbReader(db).watched_for(1)
        finally:
            db.chmod(0o644)  # so tmp_path cleanup can remove it
