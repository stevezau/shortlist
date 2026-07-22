"""Read watched FLAGS straight from the Plex Media Server database.

Plex's history API returns playback *sessions* only. It never returns a mark-as-watched, at any
depth or date window — proven on SFLIX: six titles sitting in a user's row were all flagged watched
inside a 12-second window in Oct 2024 (a bulk mark), and none of them appear in that account's
11,462-play history. On that server the API sees ~1,000 of one user's ~13,200 watched titles.

`metadata_item_settings` is the complete source: one row per (account, item) carrying `view_count`,
which counts marks as well as plays. It is keyed by `guid`, which joins to `metadata_items.id` — the
ratingKey everything else here is keyed on.

**Read-only, always.** Nothing here opens the file writable, and nothing should — it is a
multi-gigabyte live production database, and corrupting it would cost someone their whole library.
Backfilling our own `watch_events` from it achieves the same result with none of that risk.

Read-only is not the same as *safe to read badly*, though. Plex runs this database in WAL mode, so
it changes underneath us, and SQLite's `immutable=1` is documented as producing "incorrect query
results and/or SQLITE_CORRUPT errors" on a file that does in fact change. Measured against a live
writer: 286 of 292 `immutable=1` scans raised `database disk image is malformed`, and the handful
that returned gave silently wrong row counts — and an uncheckpointed WAL is invisible entirely, so a
perfectly healthy database can read as empty or as "not a Plex database". `mode=ro` is the correct
choice: it reads the WAL and gets a consistent MVCC snapshot. `immutable=1` is kept only as a
fallback for a genuinely read-only mount, where SQLite refuses `mode=ro` because it cannot create
the `-shm` file it wants.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

# Plex's metadata_type: 1 movie, 2 show, 3 season, 4 episode.
_MOVIE, _SHOW, _SEASON, _EPISODE = 1, 2, 3, 4

# Only rows Plex considers watched. `view_offset` (partial progress) is deliberately excluded: a
# half-watched film is not finished, and the play-history source already reports partial completion.
#
# The self-joins resolve an EPISODE to the show it belongs to (episode -> season -> show via
# `parent_id`). That is not cosmetic: both play-history sources store episodes under the
# GRANDPARENT key (`history.py`), and `build_library_index` only ever indexes shows — so an
# episode's own ratingKey matches nothing downstream. Emitting it would make a bulk "mark season
# watched" a no-op AND push episode names ("Kenia Monge", "Countdown") into the recently-watched
# list the AI prompt and the Users screen are built from.
_WATCHED_SQL = f"""
    SELECT CASE WHEN mi.metadata_type = {_EPISODE} THEN COALESCE(sh.id, mi.id) ELSE mi.id END
               AS rating_key,
           CASE WHEN mi.metadata_type = {_EPISODE} THEN COALESCE(sh.title, mi.title) ELSE mi.title END
               AS title,
           CASE WHEN mi.metadata_type = {_EPISODE} THEN COALESCE(sh.year, mi.year) ELSE mi.year END
               AS year,
           mi.metadata_type AS metadata_type,
           MAX(s.view_count) AS view_count,
           MAX(s.last_viewed_at) AS last_viewed_at
      FROM metadata_item_settings s
      JOIN metadata_items mi ON mi.guid = s.guid
      LEFT JOIN metadata_items se ON se.id = mi.parent_id
      LEFT JOIN metadata_items sh ON sh.id = se.parent_id
     WHERE s.account_id = ? AND s.view_count > 0
       AND mi.metadata_type IN ({_MOVIE}, {_SHOW}, {_EPISODE})
     GROUP BY rating_key, mi.metadata_type
"""


@dataclass(frozen=True)
class WatchedFlag:
    """One item an account has flagged as watched, whether by playing it or by marking it."""

    rating_key: int
    title: str
    year: int | None
    media_type: str  # "movie" | "show"
    view_count: int
    last_viewed_at: datetime | None


class PlexDbUnavailable(RuntimeError):
    """The database could not be read — missing, unreadable, or not a Plex library DB."""


class PlexDbReader:
    """Reads watched flags for any account out of a PMS library database.

    Args:
        path: The `com.plexapp.plugins.library.db` file, or the directory holding it.
    """

    FILENAME = "com.plexapp.plugins.library.db"

    def __init__(self, path: str | Path):
        candidate = Path(path)
        self._path = candidate / self.FILENAME if candidate.is_dir() else candidate

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        """Open read-only. `mode=ro` first — see the module docstring on why `immutable=1` is wrong
        for a live WAL database, and why it is still the right fallback for a read-only mount."""
        try:
            if not self._path.is_file():
                raise PlexDbUnavailable(f"no Plex database at {self._path}")
        except OSError as e:
            # Permission denied on the mount is the commonest Docker misconfiguration here, and
            # `Path.is_file()` re-raises it on Python 3.12 (the runtime) while swallowing it on 3.14.
            raise PlexDbUnavailable(f"cannot read {self._path}: {type(e).__name__}") from e
        try:
            con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            con.execute("SELECT 1").fetchone()  # force the open, so a failure lands here not later
            return con
        except sqlite3.Error as first:
            try:
                con = sqlite3.connect(f"file:{self._path}?immutable=1", uri=True)
                con.execute("SELECT 1").fetchone()
            except sqlite3.Error as e:
                raise PlexDbUnavailable(f"could not open {self._path}: {type(e).__name__}") from e
            wal = self._path.with_name(self._path.name + "-wal")
            if wal.exists() and wal.stat().st_size > 0:
                # Read-only mounts can't give us the WAL, so recent changes are invisible. Say so —
                # silently returning stale watch data is exactly the failure this feature exists to
                # stop, and the fix (mount the Databases directory writable, or don't) is the
                # owner's to make.
                logger.warning(
                    "plex db: opened {} read-only-immutable ({}) and its write-ahead log is "
                    "{} bytes — recent watches may not be visible yet",
                    self._path.name,
                    type(first).__name__,
                    wal.stat().st_size,
                )
            return con

    def check(self) -> str:
        """Confirm the file is readable and looks like a Plex library DB. Returns a human summary.

        Backs the settings "Test" button, so a failure has to say what is actually wrong rather than
        surface a sqlite error code.
        """
        with contextlib.closing(self._connect()) as con:
            try:
                accounts = con.execute(
                    "SELECT COUNT(DISTINCT account_id) FROM metadata_item_settings WHERE view_count > 0"
                ).fetchone()[0]
            except sqlite3.Error as e:
                raise PlexDbUnavailable(
                    f"{self._path.name} opened, but does not look like a Plex library database "
                    f"({type(e).__name__}) — point this at com.plexapp.plugins.library.db"
                ) from e
        return f"connected — watched items recorded for {accounts} account(s)"

    def watched_for(self, plex_account_id: int) -> list[WatchedFlag]:
        """Every movie/show this account has watched, marks included, one row per title.

        Episodes are folded into their show, so a marked season yields the show once — matching how
        the play-history source keys episodes and how the library index is built.

        Returns an empty list — never raises — when the account simply has nothing recorded, so a
        user Plex has never seen is indistinguishable from one with no watches, which is correct.

        Raises:
            PlexDbUnavailable: the database could not be opened or queried.
        """
        with contextlib.closing(self._connect()) as con:
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(_WATCHED_SQL, (plex_account_id,)).fetchall()
            except sqlite3.Error as e:
                raise PlexDbUnavailable(f"reading watched flags failed: {type(e).__name__}") from e

        # An episode row and a show-level row can both resolve to the same show, and the same title
        # in two libraries shares a guid — so collapse to one flag per ratingKey, keeping the
        # strongest evidence.
        best: dict[int, WatchedFlag] = {}
        for row in rows:
            media = _media_type_of(row["metadata_type"])
            if media is None:
                continue
            key = int(row["rating_key"])
            flag = WatchedFlag(
                rating_key=key,
                title=row["title"] or "",
                year=row["year"],
                media_type=media,
                view_count=int(row["view_count"] or 0),
                last_viewed_at=_epoch_to_dt(row["last_viewed_at"]),
            )
            current = best.get(key)
            if current is None or flag.view_count > current.view_count:
                best[key] = flag
        logger.debug("plex db: {} watched title(s) for account {}", len(best), plex_account_id)
        return list(best.values())


def _media_type_of(metadata_type: int | None) -> str | None:
    """Plex's numeric type → ours. Episodes have already been folded into their show by the query."""
    if metadata_type == _MOVIE:
        return "movie"
    if metadata_type in (_SHOW, _EPISODE):
        return "show"
    return None  # seasons, artists, tracks, photos — nothing a row can recommend


def _epoch_to_dt(value: int | None) -> datetime | None:
    """Plex stores these as unix seconds. A missing or nonsense value means "watched, when unknown"
    — worth keeping the row for, since the filter cares THAT it was watched, not when."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
