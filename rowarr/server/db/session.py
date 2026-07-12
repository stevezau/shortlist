"""Engine/session factory and migration bootstrap for the SQLite DB at /config/rowarr.db."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

ALEMBIC_DIR = Path(__file__).parent / "alembic"


def db_url(config_dir: Path) -> str:
    return f"sqlite:///{config_dir / 'rowarr.db'}"


def make_engine(config_dir: Path):
    engine = create_engine(db_url(config_dir), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def run_migrations(config_dir: Path) -> None:
    """Apply Alembic migrations to head (every schema change ships one — project rule)."""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url(config_dir))
    command.upgrade(cfg, "head")
    logger.info("database migrated to head at {}", config_dir / "rowarr.db")
