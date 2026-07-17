"""Per-row scheduler: each enabled row is grouped by its own cron; rows sharing a cron fire together,
and a blank/disabled/invalid cron never fires. There is no global schedule."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from shortlist.server.db.models import Collection
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.scheduler import schedule_groups


@pytest.fixture
def app(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    # The migration seeds the default 'picked' row with a cron; clear it so each test owns the set.
    with factory() as session:
        session.query(Collection).delete()
        session.commit()
    yield SimpleNamespace(state=SimpleNamespace(sessions=factory))
    engine.dispose()


def _add(factory, slug: str, schedule: str, *, enabled: bool = True) -> None:
    with factory() as session:
        session.add(Collection(slug=slug, name=slug, schedule=schedule, enabled=enabled))
        session.commit()


class TestScheduleGroups:
    def test_rows_sharing_a_cron_group_and_blank_schedules_never_fire(self, app):
        factory = app.state.sessions
        _add(factory, "a", "30 3 * * *")
        _add(factory, "b", "30 3 * * *")  # same cron as a -> one job fires both
        _add(factory, "c", "0 6 * * *")  # its own cron -> its own job
        _add(factory, "d", "")  # no schedule -> never fires

        groups = schedule_groups(app)

        assert set(groups) == {"30 3 * * *", "0 6 * * *"}  # 'd' contributes no job
        assert len(groups["30 3 * * *"]) == 2  # a + b run together
        assert len(groups["0 6 * * *"]) == 1

    def test_disabled_and_invalid_crons_are_skipped_not_crashed(self, app):
        factory = app.state.sessions
        _add(factory, "off", "30 3 * * *", enabled=False)  # disabled -> excluded
        _add(factory, "bad", "not a valid cron")  # invalid -> skipped, never raises
        _add(factory, "good", "0 4 * * *")

        groups = schedule_groups(app)

        assert set(groups) == {"0 4 * * *"}


class TestBuildScope:
    """A per-row scheduled run rebuilds ONLY its rows (`build_only`), but the config still exposes
    EVERY row to privacy classification, the share-filter sync, the sweep, and shelf promotion — so
    an out-of-scope SHARED row is never misclassified and over-hidden (the leak-safe guarantee)."""

    def _cfg(self, build_only):
        from shortlist.engine.models import EngineConfig, RowSpec

        personal = RowSpec(slug="picked", name_template="", size=10)
        shared = RowSpec(slug="popular", name_template="Popular", size=10, shared=True)
        cfg = EngineConfig(rows=[personal, shared], rows_defined=True, build_only=build_only)
        return cfg, personal, shared

    def test_scope_limits_should_build_but_not_the_row_view(self):
        cfg, personal, shared = self._cfg(frozenset({"picked"}))
        assert cfg.should_build(personal) is True
        assert cfg.should_build(shared) is False  # out of scope -> not rebuilt this run
        # ...yet BOTH stay visible to the classification/promotion helpers (they iterate the full lists),
        # so the out-of-scope shared row is never dropped from the "what's shared" set and over-hidden.
        assert cfg.per_person_rows() == [personal]
        assert cfg.shared_rows() == [shared]

    def test_a_full_run_builds_every_row(self):
        cfg, personal, shared = self._cfg(None)
        assert cfg.should_build(personal) is True
        assert cfg.should_build(shared) is True
