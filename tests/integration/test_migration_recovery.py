"""Every migration must be re-runnable after a crash.

SQLite auto-commits DDL. A migration interrupted after its statements ran but before Alembic bumped
`alembic_version` (the container is killed, two deployers race — as happened live on SFLIX) leaves
the schema change committed and the version stamp behind it. Alembic re-runs that revision on the
next boot, so every revision has to survive being applied to a database that already has its
changes: a re-run must FINISH the job, not fail on "table already exists" / "duplicate column".

This is not a tidiness rule. A revision that cannot be re-run bricks the container permanently —
it fails on every boot, and there is no state to roll back to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from shortlist.server.db import session as db_session

pytestmark = pytest.mark.integration


def _revisions() -> list[tuple[str, str | None]]:
    """Every (revision, parent) in the migration tree, oldest first — read from the scripts.

    Enumerated rather than hard-coded so a new migration is covered the moment it is added: the
    revision that bricks a container will otherwise be the one nobody remembered to list here.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(db_session.ALEMBIC_DIR))
    script = ScriptDirectory.from_config(cfg)
    return [(rev.revision, rev.down_revision) for rev in reversed(list(script.walk_revisions()))]


REVISIONS = _revisions()


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()


@pytest.mark.parametrize(("revision", "parent"), REVISIONS, ids=[rev for rev, _ in REVISIONS])
def test_every_revision_is_re_runnable_after_a_crash(tmp_path: Path, revision: str, parent: str | None):
    """Rewind the version stamp to before `revision` on an already-migrated DB, and boot again.

    That is exactly the state a crash mid-`revision` leaves behind: its DDL committed, its version
    stamp never written. The next boot re-applies it — and must reach head.
    """
    db_session.run_migrations(tmp_path)
    engine = db_session.make_engine(tmp_path)
    head = _version(engine)

    with engine.begin() as conn:
        if parent is None:
            # A crash inside the FIRST migration: the version table exists but was never stamped.
            conn.execute(sa.text("DELETE FROM alembic_version"))
        else:
            conn.execute(sa.text("UPDATE alembic_version SET version_num = :v"), {"v": parent})

    db_session.run_migrations(tmp_path)  # must not raise

    assert _version(engine) == head
    with engine.connect() as conn:
        # Recovery must not duplicate the default row the initial migration seeds: a re-run finishes
        # the job, it does not redo it. (Re-seeding would give the owner two "Picked for You" rows.)
        assert [r[0] for r in conn.execute(sa.text("SELECT slug FROM collections")).fetchall()] == ["picked"]


def test_a_crash_before_the_default_seed_still_seeds_it(tmp_path: Path):
    """A crash mid-initial that created the tables but lost the default-row seed: the re-run must seed
    it (the initial migration's seed is guarded by 'collections is empty', not by the version stamp)."""
    db_session.run_migrations(tmp_path)
    engine = db_session.make_engine(tmp_path)
    head = _version(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM collections"))  # seed lost
        conn.execute(sa.text("DELETE FROM alembic_version"))  # stamp never written (crash before it)

    db_session.run_migrations(tmp_path)

    with engine.connect() as conn:
        assert _version(engine) == head  # the real head, not a literal a new migration invalidates
        assert [r[0] for r in conn.execute(sa.text("SELECT slug FROM collections")).fetchall()] == ["picked"]


def test_no_migration_is_numbered_inside_the_reserved_squashed_range(tmp_path: Path):
    """Revisions 0002-0028 belong to the squashed pre-release migrations, and `_heal_squashed_revision`
    re-stamps any DB carrying one of them BACK to 0001. A post-baseline migration numbered in that
    range is therefore un-stamped and replayed on every boot — silently survivable only while the
    migration happens to be idempotent, and corrupting the moment one does a backfill or a drop.
    """
    live = {rev for rev, _ in REVISIONS} - {"0001"}
    assert not (live & db_session._SQUASHED_REVISIONS), (
        f"migration(s) {sorted(live & db_session._SQUASHED_REVISIONS)} sit in the reserved "
        "0002-0028 range and will be re-stamped backward on every boot — number new ones from 0029"
    )


def test_booting_twice_leaves_the_stamp_alone(tmp_path: Path):
    """The consequence the range guards against, asserted directly: a second boot on an up-to-date DB
    must be a no-op, not a heal-and-replay."""
    db_session.run_migrations(tmp_path)
    engine = db_session.make_engine(tmp_path)
    head = _version(engine)

    db_session.run_migrations(tmp_path)

    assert _version(engine) == head
