"""Engine/session factory and migration bootstrap for the SQLite DB at /config/shortlist.db."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

ALEMBIC_DIR = Path(__file__).parent / "alembic"


def db_url(config_dir: Path) -> str:
    return f"sqlite:///{config_dir / 'shortlist.db'}"


def make_engine(config_dir: Path):
    engine = create_engine(db_url(config_dir), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # A run's parallel candidate fetches write cache rows concurrently; WAL allows one writer at
        # a time, so without a busy timeout a second writer would fail with "database is locked".
        # 5s lets it wait out the brief write lock instead.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


# The exact pre-release revisions collapsed into 0001_initial. Healing is gated on THIS frozen set —
# never "any unknown revision" — so it can only ever fire for this one squash transition. Gating on
# "unknown" would also (wrongly) fire on a post-release image rollback, where the DB is stamped NEWER
# than the running code; that must take the normal upgrade path, not be re-stamped backward.
# NOTE: this reserves 0002-0028 forever. A post-baseline migration numbered inside the range would
# be treated as a squashed revision and re-stamped BACKWARD to 0001 on every boot, replaying it each
# time — so new migrations start at 0029.
_SQUASHED_REVISIONS = frozenset(f"{i:04d}" for i in range(2, 29))  # 0002..0028


def _heal_squashed_revision(cfg: AlembicConfig, config_dir: Path) -> None:
    """Re-stamp a DB stamped at one of the now-squashed revisions to the ``0001`` baseline.

    The 28 pre-release migrations were collapsed into ``0001_initial``. A DB stamped at one of those
    removed revisions can't ``upgrade`` (alembic can't find the revision). Its schema already matches the
    baseline, so stamp it forward to ``0001`` — but ONLY when every expected table is present, so an
    unexpectedly-incomplete DB fails loudly instead of being silently marked up-to-date. Fresh DBs and
    DBs already on a live revision are left alone.
    """
    from sqlalchemy import inspect

    from shortlist.server.db.models import Base

    engine = create_engine(db_url(config_dir))
    try:
        with engine.connect() as conn:
            # Read the stamp with raw SQL — alembic's own get_current_revision() resolves it against the
            # scripts and would itself raise on a squashed-away revision. No table = fresh DB.
            if not inspect(conn).has_table("alembic_version"):
                return
            current = conn.exec_driver_sql("select version_num from alembic_version").scalar()
            if current not in _SQUASHED_REVISIONS:
                return  # fresh DB, already on 0001, or a real post-release revision — nothing to heal
            existing = set(inspect(conn).get_table_names())
            missing = set(Base.metadata.tables) - existing
            if missing:
                logger.error(
                    "DB at unknown revision {} and MISSING tables {} — not auto-stamping; upgrade will report it",
                    current,
                    sorted(missing),
                )
                return
            # Rewrite the stamp with raw SQL — alembic's stamp() would try to compute a path FROM the
            # squashed-away revision and raise. The schema already matches the baseline, so this is safe.
            logger.warning("DB at squashed revision {}; schema already complete → stamping to baseline 0001", current)
            conn.exec_driver_sql("update alembic_version set version_num = '0001'")
            conn.commit()
    finally:
        engine.dispose()


def run_migrations(config_dir: Path) -> None:
    """Apply Alembic migrations to head (every schema change ships one — project rule)."""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url(config_dir))
    _heal_squashed_revision(cfg, config_dir)
    command.upgrade(cfg, "head")
    logger.info("database migrated to head at {}", config_dir / "shortlist.db")
