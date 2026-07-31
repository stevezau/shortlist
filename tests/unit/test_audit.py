"""The audit vocabulary — one set of level words, enforced at the writer and in the source."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shortlist.server.db.models import Event
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.audit import LEVELS, add_audit

SERVER = Path(__file__).resolve().parents[2] / "shortlist"


@pytest.fixture
def db_session(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


class TestTheAuditLevelVocabulary:
    def test_add_audit_refuses_a_level_outside_the_vocabulary(self, db_session):
        # The typo this catches is "warn": every reader matches "warning", so a short form is
        # written happily and then never comes back from a filter.
        with pytest.raises(ValueError, match="warning"):
            add_audit(db_session, "row.remove", "warn", removed=1)

    def test_add_audit_accepts_each_word_and_writes_it_through(self, db_session):
        for level in sorted(LEVELS):
            add_audit(db_session, "row.remove", level, removed=1)
        db_session.commit()

        written = {e.level for e in db_session.query(Event).all()}
        assert written == set(LEVELS)

    def test_no_source_file_writes_a_level_outside_the_vocabulary(self):
        """`Event(...)` can be constructed directly, so the writer's guard is not the whole story.

        This is the check that would have caught the original split: five call sites said "warn"
        while `notifications.py` filtered on "warning", and nothing anywhere disagreed out loud.
        """
        # Migrations are exempt: a migration that rewrites an old value has to be able to name it,
        # and 0054 is exactly that migration.
        offenders = [
            f"{path.relative_to(SERVER.parent)}:{i}: {line.strip()}"
            for path in SERVER.rglob("*.py")
            if "alembic/versions" not in path.as_posix()
            for i, line in enumerate(path.read_text().splitlines(), 1)
            for m in re.finditer(r'level=(["\'])([a-z]+)\1', line)
            if m.group(2) not in LEVELS
        ]
        assert offenders == [], "audit levels must be one of " + ", ".join(sorted(LEVELS))
