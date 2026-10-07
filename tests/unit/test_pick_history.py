"""The pick history reader (#138): what rows showed on earlier REAL runs, for cooldown and keep-out rows."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import MediaType
from shortlist.server.db.models import Base, PickRow, Run, User
from shortlist.server.services.pick_history import DbPickHistory
from tests.db_helpers import disposing_engine
from tests.watch_fixtures import live_row, personal_delivery

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
TODAY = NOW.date()


@pytest.fixture
def session():
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        with sessionmaker(engine)() as s:
            yield s


def factory_of(session):
    """A sessions factory over the test's database, as the server hands one to the history reader."""
    return lambda: sessionmaker(bind=session.get_bind())()


def add_user(session, slug: str, account_id: int) -> User:
    user = User(plex_account_id=account_id, username=slug, slug=slug)
    session.add(user)
    session.commit()
    return user


def add_run(session, days_ago: int, *, dry_run: bool = False) -> Run:
    run = Run(trigger="schedule", started_at=NOW - timedelta(days=days_ago), dry_run=dry_run)
    session.add(run)
    session.commit()
    return run


def add_pick(
    session,
    user: User,
    run: Run | None,
    tmdb_id: int,
    *,
    row: str = "ai-row",
    media: str = "movie",
    days_ago: int = 0,
):
    session.add(
        PickRow(
            run_id=None if run is None else run.id,
            user_id=user.id,
            tmdb_id=tmdb_id,
            media_type=media,
            rating_key=tmdb_id,
            rank=1,
            collection_slug=row,
            section_key="1",
            created_at=NOW - timedelta(days=days_ago) if run is None else run.started_at,
        )
    )
    if run is not None and not run.dry_run:
        live_row(session, user.id, row, "1")
        personal_delivery(session, run.id, user_id=user.id, slug=row)
    session.commit()


def key(tmdb_id: int, media: MediaType = MediaType.MOVIE):
    return (media, tmdb_id)


@pytest.fixture
def alex(session) -> User:
    return add_user(session, "alex", 1)


class TestFirstShownSince:
    def test_dry_runs_never_count_toward_cooldown(self, session, alex):
        add_pick(session, alex, add_run(session, 1, dry_run=True), 10)

        history = DbPickHistory(factory_of(session))

        assert history.first_shown_since("alex", "ai-row", TODAY - timedelta(days=30)) == set()
        assert history.latest("alex", "ai-row") == set()

    def test_a_pick_whose_run_was_pruned_still_blocks_a_long_cooldown(self, session, alex):
        # Run pruning sets `run_id` to NULL on real picks it keeps; the window must not end at run retention.
        add_pick(session, alex, None, 10, days_ago=100)
        add_pick(session, alex, add_run(session, 100, dry_run=True), 20, days_ago=100)

        history = DbPickHistory(factory_of(session))

        assert history.first_shown_since("alex", "ai-row", TODAY - timedelta(days=120)) == {key(10)}
        assert history.first_shown_since("alex", "ai-row", TODAY - timedelta(days=30)) == set()
        assert history.latest("alex", "ai-row") == set()

    def test_first_shown_is_the_earliest_appearance(self, session, alex):
        for days in (40, 10, 1):
            add_pick(session, alex, add_run(session, days), 10)
        add_pick(session, alex, add_run(session, 3), 20)

        history = DbPickHistory(factory_of(session))

        assert history.first_shown_since("alex", "ai-row", TODAY - timedelta(days=30)) == {key(20)}
        assert history.first_shown_since("alex", "ai-row", TODAY - timedelta(days=60)) == {key(10), key(20)}

    def test_carried_forward_picks_do_not_reset_first_shown(self, session, alex):
        for days in (40, 20, 5, 1):
            add_pick(session, alex, add_run(session, days), 10)

        assert (
            DbPickHistory(factory_of(session)).first_shown_since("alex", "ai-row", TODAY - timedelta(days=30)) == set()
        )

    def test_a_dry_run_sighting_does_not_hide_the_real_first_one(self, session, alex):
        add_pick(session, alex, add_run(session, 2), 10)
        add_pick(session, alex, add_run(session, 50, dry_run=True), 10)

        assert DbPickHistory(factory_of(session)).first_shown_since("alex", "ai-row", TODAY - timedelta(days=30)) == {
            key(10)
        }

    def test_media_type_is_part_of_the_title(self, session, alex):
        add_pick(session, alex, add_run(session, 2), 10, media="show")

        got = DbPickHistory(factory_of(session)).first_shown_since("alex", "ai-row", TODAY - timedelta(days=30))

        assert got == {key(10, MediaType.SHOW)}

    def test_scoped_to_the_person_and_the_row(self, session, alex):
        sam = add_user(session, "sam", 2)
        run = add_run(session, 2)
        add_pick(session, alex, run, 10)
        add_pick(session, sam, run, 11)
        add_pick(session, alex, run, 12, row="other-row")

        got = DbPickHistory(factory_of(session)).first_shown_since("alex", "ai-row", TODAY - timedelta(days=30))

        assert got == {key(10)}

    def test_unknown_person_is_empty(self, session, alex):
        add_pick(session, alex, add_run(session, 2), 10)

        history = DbPickHistory(factory_of(session))

        assert history.first_shown_since("ghost", "ai-row", date(2020, 1, 1)) == set()
        assert history.latest("ghost", "ai-row") == set()


class TestLatest:
    def test_returns_only_the_newest_real_run_that_wrote_the_row(self, session, alex):
        add_pick(session, alex, add_run(session, 5), 10)
        newest = add_run(session, 2)
        add_pick(session, alex, newest, 20)
        add_pick(session, alex, newest, 21, media="show")
        add_pick(session, alex, add_run(session, 1, dry_run=True), 30)
        add_pick(session, alex, add_run(session, 0), 40, row="other-row")

        assert DbPickHistory(factory_of(session)).latest("alex", "ai-row") == {key(20), key(21, MediaType.SHOW)}
