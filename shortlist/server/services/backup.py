"""Automatic SQLite backup management for the /config directory.

Takes point-in-time copies of shortlist.db (via SQLite's backup API for crash-safety), rotates old
ones, and hooks into startup (pre-migration) and APScheduler (daily). The backup directory lives at
/config/backups/ so it's inside the user's persisted volume.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path

from loguru import logger

DEFAULT_MAX_BACKUPS = 10
BACKUP_SUBDIR = "backups"
#: A backup is written under its final name plus this, and renamed only once complete. The listing and
#: rotation match `shortlist_*.db`, which never matches it, so an unfinished copy is never a restore point.
PARTIAL_SUFFIX = ".partial"
#: A partial and the SQLite journal beside it.
_PARTIAL_GLOB = f"shortlist_*.db{PARTIAL_SUFFIX}*"
#: One backup at a time per process, so the sweep of stale partials can never take one still being written.
_backup_lock = threading.Lock()


def safe_backup_name(name: str) -> str:
    """A backup filename, or raise — never a path.

    ``config_dir / BACKUP_SUBDIR / name`` with an unvalidated ``name`` lets `../../etc/passwd` escape
    the backups directory, and restore then copies whatever it finds over the database. Owner-only and
    self-inflicted, but it costs one check to make the traversal impossible rather than merely
    unattractive.
    """
    cleaned = (name or "").strip()
    if not cleaned or cleaned != cleaned.strip("/\\") or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("backup name must be a plain filename")
    return cleaned


def _backup_dir(config_dir: Path) -> Path:
    d = config_dir / BACKUP_SUBDIR
    d.mkdir(exist_ok=True)
    return d


def take_backup(config_dir: Path, *, label: str = "scheduled", max_keep: int = DEFAULT_MAX_BACKUPS) -> Path | None:
    """Take a crash-safe backup of shortlist.db using SQLite's online backup API.

    Returns:
        The new backup's path on success. ``None`` in two cases this function does not tell apart:
        there is no ``shortlist.db`` to back up, or the copy failed (logged here). A caller that must
        know checks the database itself first. ``run_migrations`` calls this only once ``shortlist.db``
        exists with a non-zero size and a migration is pending, so ``None`` there can only mean a
        failed copy, and it refuses to migrate. Never read ``None`` as "nothing to back up" alone.

    Raises:
        OSError: The ``backups/`` folder could not be created; that happens outside the copy's handler.
    """
    db_path = config_dir / "shortlist.db"
    if not db_path.exists():
        return None

    backup_dir = _backup_dir(config_dir)
    # LOCAL time, matching every log line (the container's TZ). An operator picking a restore point
    # reads these filenames against a log that narrates what happened at 03:31 their time; a UTC
    # filename asks them to convert, and an off-by-a-timezone choice restores the wrong night.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # The label lands in a filename, and `POST /api/system/jobs` takes an unvalidated payload — so
    # `{"label": "../../../../tmp/x"}` wrote a complete copy of the database, encrypted Plex tokens
    # included, outside /config. The restore path has always validated its filename; this one didn't.
    safe_label = re.sub(r"[^a-z0-9_-]", "-", (label or "backup").strip().lower())[:32] or "backup"
    backup_path = backup_dir / f"shortlist_{ts}_{safe_label}.db"
    # Copied under another name and renamed once complete. A process stopped mid-copy (a container
    # stop during "Back up now" or the pre-migration backup) would leave a half-written file under
    # the real name if copied in place: listed, kept by rotation, and restorable with no integrity check.
    partial = backup_path.with_name(backup_path.name + PARTIAL_SUFFIX)

    with _backup_lock:
        _sweep_partials(backup_dir)
        try:
            src = dst = None
            try:
                src = sqlite3.connect(str(db_path))
                dst = sqlite3.connect(str(partial))
                src.backup(dst)
            finally:
                # Both handles, always, and before the rename or the unlink below. Closing only on the
                # success path would leak two SQLite connections on a failure mid-`backup()` and then
                # unlink a file `dst` still held — and this runs on every boot.
                for conn in (dst, src):
                    if conn is not None:
                        conn.close()
            size = partial.stat().st_size
            os.replace(partial, backup_path)
        except Exception as e:
            logger.error("backup failed: {}", e)
            return None
        finally:
            partial.unlink(missing_ok=True)  # nothing left to remove once the rename has happened
    logger.info("backup created: {} ({:.1f} KB)", backup_path.name, size / 1024)

    # Housekeeping must never invalidate the backup it is tidying up around. This runs on every boot,
    # so an exception escaping here failed the boot AFTER a good backup had already been written —
    # a crash-loop on a host that recreates the container automatically, and the backup was lost with
    # it. Rotation failing just means old files linger, which costs disk and nothing else.
    try:
        _rotate(backup_dir, max_keep)
    except OSError as e:
        logger.warning("could not rotate old backups ({}); keeping them, the new backup is fine", e)
    return backup_path


def _sweep_partials(backup_dir: Path) -> None:
    """Remove unfinished copies a stopped process left behind. Housekeeping: a failure only logs."""
    for stale in backup_dir.glob(_PARTIAL_GLOB):
        try:
            stale.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("could not remove an unfinished backup {} ({})", stale.name, type(e).__name__)
            continue
        logger.info("removed an unfinished backup left by an earlier stop: {}", stale.name)


def _is_partial(name: str) -> bool:
    return fnmatch(name, _PARTIAL_GLOB)


def _rotate(backup_dir: Path, max_keep: int) -> None:
    """Keep only the most recent `max_keep` backups, delete the rest.

    Every file is handled independently: `glob` gives a snapshot, and by the time we `stat` or
    `unlink` an entry it may be gone — a concurrent boot rotating the same directory, a manual
    tidy-up, a network filesystem. One vanished file must not stop the rest being rotated, and must
    not be reported as a failure: it is already in the state we wanted.
    """

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0  # sorts last, so it is a rotation candidate; the unlink below tolerates it

    # Never the backup a restore is waiting for: it would be gone by the restart that applies it.
    waiting = pending_restore(backup_dir.parent)
    keep = waiting["backup"] if waiting else None
    backups = sorted((b for b in backup_dir.glob("shortlist_*.db") if b.name != keep), key=_mtime, reverse=True)
    for old in backups[max_keep:]:
        try:
            old.unlink()
        except FileNotFoundError:
            continue  # someone else got there first — the desired outcome either way
        logger.debug("rotated old backup: {}", old.name)


def list_backups(config_dir: Path) -> list[dict]:
    """Return metadata for all existing backups, newest first."""
    backup_dir = config_dir / BACKUP_SUBDIR
    if not backup_dir.exists():
        return []
    backups = sorted(backup_dir.glob("shortlist_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for p in backups:
        stat = p.stat()
        result.append(
            {
                "name": p.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            }
        )
    return result


def _passes_integrity_check(path: Path) -> bool:
    """True when `path` is a non-empty SQLite file with an `alembic_version` table that answers
    `PRAGMA integrity_check` with exactly 'ok'.

    An empty file passes SQLite's own check, and so would a database from some other program; neither
    is a Shortlist backup worth replacing the live database with. The read-only open of a WAL-mode file
    can leave `-wal`/`-shm` sidecars beside it, which would otherwise be picked up by the real database
    when the staged file is moved into place, so they are removed.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            has_version = (
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()
                is not None
            )
        finally:
            conn.close()
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
    except sqlite3.Error:
        return False
    return rows == [("ok",)] and has_version


def restore_backup(config_dir: Path, backup_name: str, *, max_keep: int = DEFAULT_MAX_BACKUPS) -> bool:
    """Restore a backup by copying it over the current DB. Returns True on success.

    Nothing may have the database open: not a pooled connection, not a session. A connection left open
    keeps writing to the WAL this unlinks, and closing it checkpoints those pages back over the restored
    file. So the running app never calls this; it queues the restore (`request_restore`) and the next
    boot applies it (`apply_pending_restore`) before anything opens the database.

    Args:
        config_dir: the /config directory.
        backup_name: a file in /config/backups.
        max_keep: the owner's backup limit, for the rotation the pre-restore copy triggers.
    """
    try:
        backup_name = safe_backup_name(backup_name)
    except ValueError:
        # `../../etc/passwd` would otherwise escape the backups directory and be copied over the DB.
        logger.error("refusing a backup name that is not a plain filename: {!r}", backup_name)
        return False
    backup_path = config_dir / BACKUP_SUBDIR / backup_name
    db_path = config_dir / "shortlist.db"
    staged = config_dir / RESTORE_STAGING
    if _is_partial(backup_name) or not backup_path.exists():
        logger.error("backup not found: {}", backup_name)
        return False

    # Copied aside FIRST. The pre-restore copy below rotates the backups, and the file it rotates out is
    # the oldest, which can be the very one being restored.
    try:
        shutil.copyfile(backup_path, staged)
    except OSError as e:
        staged.unlink(missing_ok=True)
        logger.error("could not read backup {} ({}) — the database was not changed", backup_name, type(e).__name__)
        return False

    # A truncated or corrupt file would replace a working database and take the server down on the next boot.
    if not _passes_integrity_check(staged):
        staged.unlink(missing_ok=True)
        logger.error(
            "refusing to restore {}: it failed SQLite's integrity check, so the database was not changed", backup_name
        )
        return False

    # The pre-restore backup is the ONLY way back from a restore chosen by mistake, so a restore
    # that could not take one does not proceed. `take_backup` returns None on a full disk, a
    # permission problem, or a locked database — and the next two steps unlink the WAL and replace
    # the live database, which is exactly when there is nothing left to go back to. Refusing loses
    # the restore; continuing loses the server's current state with no copy of it anywhere.
    if take_backup(config_dir, label="pre-restore", max_keep=max_keep) is None:
        staged.unlink(missing_ok=True)
        logger.error(
            "refusing to restore {}: could not take a pre-restore backup first, so the current "
            "database would be overwritten with no way back",
            backup_name,
        )
        return False

    # Remove WAL/SHM files (they belong to the old DB)
    for suffix in (".db-wal", ".db-shm"):
        wal = config_dir / f"shortlist{suffix}"
        if wal.exists():
            wal.unlink()

    # A rename, not a copy: a crash leaves either the old database or the restored one, never half of each.
    os.replace(staged, db_path)
    logger.info("restored from backup: {}", backup_name)
    return True


#: A restore the owner asked for, waiting for the restart that applies it. In /config, not /config/backups,
#: so the backup listing and its rotation never see it.
RESTORE_PENDING = "restore-pending.json"
#: Where a backup is copied before it replaces the database, so the swap is a rename.
RESTORE_STAGING = "shortlist.db.restoring"
#: A restore still waiting after this long is not applied. It rolls back who can see which rows, and an
#: owner who asked for it a day ago and never restarted should not have it land on an unplanned restart
#: (a host reboot, an auto-updater at 05:30).
RESTORE_EXPIRES_AFTER = timedelta(hours=24)


def request_restore(config_dir: Path, backup_name: str, *, max_keep: int = DEFAULT_MAX_BACKUPS) -> bool:
    """Queue a restore of this backup for the next boot. False when there is no such backup.

    The running app keeps the database it has open until the restart; `apply_pending_restore` swaps
    the backup in at the start of the next boot, where nothing has the database open yet.
    """
    try:
        backup_name = safe_backup_name(backup_name)
    except ValueError:
        logger.error("refusing a backup name that is not a plain filename: {!r}", backup_name)
        return False
    # An unfinished copy is not a backup, whatever the folder holds under that name.
    if _is_partial(backup_name) or not (config_dir / BACKUP_SUBDIR / backup_name).exists():
        logger.error("backup not found: {}", backup_name)
        return False
    queued = {"backup": backup_name, "requested_at": datetime.now(UTC).isoformat(), "max_keep": max_keep}
    (config_dir / RESTORE_PENDING).write_text(json.dumps(queued))
    logger.warning("restore of {} queued — it is applied when Shortlist next starts", backup_name)
    return True


def pending_restore(config_dir: Path) -> dict | None:
    """``{"backup", "requested_at"}`` for the restore waiting for a restart, or None."""
    try:
        queued = json.loads((config_dir / RESTORE_PENDING).read_text())
        return {"backup": str(queued["backup"]), "requested_at": str(queued["requested_at"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cancel_restore(config_dir: Path) -> dict | None:
    """Forget the restore waiting for a restart. Returns what was waiting, or None."""
    waiting = pending_restore(config_dir)
    (config_dir / RESTORE_PENDING).unlink(missing_ok=True)
    return waiting


def apply_pending_restore(config_dir: Path, now: datetime | None = None) -> dict | None:
    """Apply the restore `request_restore` queued, if there is one. Call before anything opens the database.

    The request is removed FIRST, so a restore that fails (or crashes the boot) is not retried on every
    start after it; the pre-restore copy `restore_backup` takes is the way back from a bad one. Never
    raises: a boot that cannot restore still starts, on the database it had.

    Returns:
        None when nothing was queued, else ``{"backup": name, "status": "restored" | "failed" | "expired"}``.
    """
    marker = config_dir / RESTORE_PENDING
    if not marker.exists():
        # A boot killed inside the copy below had already removed the request, so nothing else would.
        (config_dir / RESTORE_STAGING).unlink(missing_ok=True)
        return None
    try:
        queued = json.loads(marker.read_text())
        name = str(queued["backup"])
        requested_at = datetime.fromisoformat(str(queued["requested_at"]))
        max_keep = int(queued.get("max_keep") or DEFAULT_MAX_BACKUPS)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        name, requested_at, max_keep = "", None, DEFAULT_MAX_BACKUPS
    marker.unlink(missing_ok=True)
    if not name or requested_at is None:
        logger.error("ignoring an unreadable restore request in {} — the database was not changed", RESTORE_PENDING)
        return {"backup": name, "status": "failed"}
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=UTC)
    if (now or datetime.now(UTC)) - requested_at > RESTORE_EXPIRES_AFTER:
        logger.warning("not restoring {}: it was asked for over a day ago and never applied", name)
        return {"backup": name, "status": "expired"}
    try:
        restored = restore_backup(config_dir, name, max_keep=max_keep)
    except Exception:
        logger.exception("restoring {} failed — the database was not changed", name)
        restored = False
    if not restored:
        (config_dir / RESTORE_STAGING).unlink(missing_ok=True)
    return {"backup": name, "status": "restored" if restored else "failed"}


def read_setting(config_dir: Path, key: str) -> object | None:
    """One setting straight from `shortlist.db`, for the moment before the app opens it. None if unreadable."""
    db_path = config_dir / "shortlist.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(db_path)
        try:
            row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        finally:
            con.close()
        value = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    return value.get("v") if isinstance(value, dict) else None
