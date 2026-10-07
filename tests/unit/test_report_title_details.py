"""What the dashboard needs to show a title as a title: its poster, its year, its look-up links, and who watched it.

"Most watched" and "Recently watched from Shortlist" were bare text — a name and a count — so nothing on the
dashboard said what a title was or offered a way to look it up. Each line now carries a Plex `rating_key`
for the poster, the TMDB id the look-up links are built from, the year, and (for the top titles) the first
few people who watched it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.db.models import Base, Collection, PickRow, SharedRowWatch, User, WatchedTitle
from shortlist.server.services.report_service import effectiveness
from tests.db_helpers import disposing_engine

NOW = datetime.now(UTC)


@pytest.fixture
def sessions():
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        with factory() as session:
            for uid, name in ((1, "alex"), (2, "sam"), (3, "kim"), (4, "lee")):
                session.add(
                    User(
                        id=uid, plex_account_id=uid + 100, username=name, slug=name, enabled=True, nickname=name.title()
                    )
                )
            session.add(Collection(slug="picked", name="Picked for You", enabled=True))
            session.add(Collection(slug="popular", name="Popular", enabled=True, build="shared"))
            session.commit()
        yield factory


def pick(sessions, *, user_id: int, tmdb_id: int, watched_hours_ago: float, rating_key: int = 0, year=2021):
    with sessions() as session:
        session.add(
            PickRow(
                user_id=user_id,
                tmdb_id=tmdb_id,
                media_type="show",
                rating_key=rating_key,
                rank=1,
                collection_slug="picked",
                section_key="2",
                library="TV Shows",
                title=f"T{tmdb_id}",
                year=year,
                created_at=NOW - timedelta(days=10),
                watched_at=NOW - timedelta(hours=watched_hours_ago),
            )
        )
        session.commit()


def shared_watch(sessions, *, user_id: int, tmdb_id: int, watched_hours_ago: float):
    with sessions() as session:
        session.add(
            SharedRowWatch(
                user_id=user_id,
                collection_slug="popular",
                tmdb_id=tmdb_id,
                media_type="show",
                title=f"T{tmdb_id}",
                watched_at=NOW - timedelta(hours=watched_hours_ago),
            )
        )
        session.commit()


def history(sessions, *, user_id: int, tmdb_id: int, rating_key: int, year: int):
    with sessions() as session:
        session.add(
            WatchedTitle(
                user_id=user_id,
                section_key="2",
                rating_key=rating_key,
                tmdb_id=tmdb_id,
                media_type="show",
                title=f"T{tmdb_id}",
                year=year,
                viewed_at=NOW - timedelta(hours=1),
            )
        )
        session.commit()


def report(sessions) -> dict:
    with sessions() as session:
        return effectiveness(session, "30")


class TestTopTitles:
    def test_a_top_title_carries_its_poster_key_year_and_first_three_watchers(self, sessions):
        for uid, hours in ((1, 40), (2, 30), (3, 20), (4, 10)):
            pick(sessions, user_id=uid, tmdb_id=7, watched_hours_ago=hours, rating_key=7007 if uid == 2 else 0)

        top = report(sessions)["top_titles"][0]

        assert top["tmdb_id"] == 7
        assert top["rating_key"] == 7007  # any delivery that was matched to the library will do
        assert top["year"] == 2021
        assert top["watchers"] == 4
        # Newest first, three at most: faces for a glance, the count says the rest.
        assert [w["name"] for w in top["watcher_sample"]] == ["Lee", "Kim", "Sam"]
        assert [w["id"] for w in top["watcher_sample"]] == [4, 3, 2]

    def test_a_title_found_only_through_a_shared_row_takes_its_poster_key_from_watch_history(self, sessions):
        # Shared rows write no picks, so the only Plex key on record is the one their watch history holds.
        shared_watch(sessions, user_id=1, tmdb_id=9, watched_hours_ago=5)
        history(sessions, user_id=1, tmdb_id=9, rating_key=9009, year=2019)

        top = report(sessions)["top_titles"][0]

        assert (top["rating_key"], top["year"]) == (9009, 2019)
        assert [w["name"] for w in top["watcher_sample"]] == ["Alex"]

    def test_a_title_never_matched_to_the_library_has_no_poster_key(self, sessions):
        pick(sessions, user_id=1, tmdb_id=5, watched_hours_ago=5, rating_key=0, year=None)

        top = report(sessions)["top_titles"][0]

        assert (top["rating_key"], top["year"]) == (0, None)

    def test_a_placeholder_key_from_watch_history_is_not_a_poster_key(self, sessions):
        # The watch cache stores -tmdb_id when a history source gave no ratingKey. That is not a Plex key,
        # and /api/picks/-9/poster only ever 404s.
        shared_watch(sessions, user_id=1, tmdb_id=9, watched_hours_ago=5)
        history(sessions, user_id=1, tmdb_id=9, rating_key=-9, year=2019)

        assert report(sessions)["top_titles"][0]["rating_key"] == 0

    def test_a_film_and_a_show_sharing_a_tmdb_id_keep_their_own_posters(self, sessions):
        with sessions() as session:
            for media_type, key in (("movie", 111), ("show", 222)):
                session.add(
                    PickRow(
                        user_id=1,
                        tmdb_id=3,
                        media_type=media_type,
                        rating_key=key,
                        rank=1,
                        collection_slug="picked",
                        section_key="1",
                        library="Movies",
                        title=f"T3 {media_type}",
                        created_at=NOW - timedelta(days=10),
                        watched_at=NOW - timedelta(hours=3),
                    )
                )
            session.commit()

        keys = {(t["media_type"], t["rating_key"]) for t in report(sessions)["top_titles"]}

        assert keys == {("movie", 111), ("show", 222)}

    def test_a_watch_outside_the_window_is_not_a_face(self, sessions):
        pick(sessions, user_id=1, tmdb_id=7, watched_hours_ago=5)
        pick(sessions, user_id=2, tmdb_id=7, watched_hours_ago=40 * 24)  # 40 days: outside the 30-day window

        top = report(sessions)["top_titles"][0]

        assert top["watchers"] == 1
        assert [w["name"] for w in top["watcher_sample"]] == ["Alex"]


class TestRecentWatches:
    def test_a_recent_watch_carries_its_tmdb_id_poster_key_and_year(self, sessions):
        pick(sessions, user_id=1, tmdb_id=7, watched_hours_ago=2, rating_key=7007, year=2021)

        line = report(sessions)["recent"][0]

        assert (line["tmdb_id"], line["rating_key"], line["year"]) == (7, 7007, 2021)

    def test_a_shared_row_watch_takes_its_poster_key_and_year_from_that_persons_history(self, sessions):
        shared_watch(sessions, user_id=2, tmdb_id=9, watched_hours_ago=2)
        history(sessions, user_id=2, tmdb_id=9, rating_key=9009, year=2019)

        line = report(sessions)["recent"][0]

        assert (line["tmdb_id"], line["rating_key"], line["year"]) == (9, 9009, 2019)
