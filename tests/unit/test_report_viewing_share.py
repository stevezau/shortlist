"""The dashboard's viewing share: of the titles people watched, how many were in their Shortlist row.

It replaced "picks watched while their row still showed them" as the dashboard's rate. That one divided
by every title ever SHOWN — a 20-to-30 title row nobody will watch most of — so it sat under 1% whether
Shortlist was working or not (0.7% on a real server: 71 of 10,898). This one divides by what people
actually WATCHED, which is the thing a recommendation competes for: 99 of 535 over 30 days on the same server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.db.models import Collection, PickRow, Run, SharedRowWatch, User, WatchedTitle
from shortlist.server.services.report_service import effectiveness
from tests.db_helpers import create_schema, disposing_engine

NOW = datetime.now(UTC)


@pytest.fixture
def sessions():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        factory = sessionmaker(engine)
        with factory() as session:
            session.add(User(id=1, plex_account_id=7, username="alex", slug="alex", enabled=True))
            session.add(User(id=2, plex_account_id=8, username="sam", slug="sam", enabled=True))
            session.add(User(id=3, plex_account_id=9, username="off", slug="off", enabled=False))
            session.add(Collection(slug="picked", name="Picked for You", enabled=True))
            # Shortlist has been running for 60 days; the "all" window starts here, not at the dawn of Plex.
            session.add(Run(trigger="nightly", status="ok", started_at=NOW - timedelta(days=60)))
            session.commit()
        yield factory


def watched(
    sessions,
    *,
    user_id: int,
    tmdb_id: int | None,
    days_ago: int,
    rating_key: int = 0,
    source_days_ago=None,
    media_type: str = "movie",
    section_key: str = "1",
    title: str = "",
):
    with sessions() as session:
        session.add(
            WatchedTitle(
                user_id=user_id,
                section_key=section_key,
                rating_key=rating_key or (tmdb_id or 0) + 5000,
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=title,
                year=2020,
                viewed_at=NOW - timedelta(days=days_ago),
                source_viewed_at=None if source_days_ago is None else NOW - timedelta(days=source_days_ago),
            )
        )
        session.commit()


def pick(
    sessions,
    *,
    user_id: int,
    tmdb_id: int,
    watched_days_ago: int | None,
    delivered_days_ago: int | None = None,
    media_type: str = "movie",
):
    with sessions() as session:
        session.add(
            PickRow(
                user_id=user_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                rating_key=tmdb_id,
                rank=1,
                collection_slug="picked",
                section_key="1",
                library="Movies",
                title=f"T{tmdb_id}",
                # Delivered well before anything in these tests is watched, unless a test says otherwise:
                # a person's viewing only counts from their first delivery.
                created_at=NOW - timedelta(days=delivered_days_ago if delivered_days_ago is not None else 59),
                watched_at=None if watched_days_ago is None else NOW - timedelta(days=watched_days_ago),
            )
        )
        session.commit()


def share(sessions, window: str = "30") -> dict:
    with sessions() as session:
        return effectiveness(session, window)["overall"]["viewing_share"]


class TestViewingShare:
    def test_counts_what_people_watched_and_how_much_of_it_their_row_had_shown_them(self, sessions):
        for tmdb_id in (1, 2, 3):
            watched(sessions, user_id=1, tmdb_id=tmdb_id, days_ago=5)
        watched(sessions, user_id=2, tmdb_id=1, days_ago=5)  # the same film, a different person: its own title
        pick(sessions, user_id=1, tmdb_id=2, watched_days_ago=5)
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)  # shown, never watched: not in either count
        pick(sessions, user_id=2, tmdb_id=8, watched_days_ago=None)

        assert share(sessions) == {"watched": 4, "from_rows": 1, "rate": 0.25}

    def test_a_watch_outside_the_window_counts_on_neither_side(self, sessions):
        watched(sessions, user_id=1, tmdb_id=1, days_ago=5)
        watched(sessions, user_id=1, tmdb_id=2, days_ago=40)
        pick(sessions, user_id=1, tmdb_id=2, watched_days_ago=40)

        assert share(sessions) == {"watched": 1, "from_rows": 0, "rate": 0.0}

    def test_people_shortlist_builds_nothing_for_are_left_out(self, sessions):
        # A disabled account gets no rows, so everything it watches would read as the rows failing.
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)
        pick(sessions, user_id=3, tmdb_id=9, watched_days_ago=None)  # delivered once, then disabled
        watched(sessions, user_id=1, tmdb_id=1, days_ago=5)
        watched(sessions, user_id=3, tmdb_id=2, days_ago=5)
        watched(sessions, user_id=3, tmdb_id=3, days_ago=5)

        assert share(sessions)["watched"] == 1

    def test_a_shared_row_watch_counts_as_from_a_row(self, sessions):
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)
        watched(sessions, user_id=1, tmdb_id=1, days_ago=5)
        with sessions() as session:
            session.add(
                SharedRowWatch(
                    user_id=1,
                    collection_slug="popular",
                    tmdb_id=1,
                    media_type="movie",
                    watched_at=NOW - timedelta(days=5),
                )
            )
            session.commit()

        assert share(sessions) == {"watched": 1, "from_rows": 1, "rate": 1.0}

    def test_from_rows_never_exceeds_watched(self, sessions):
        # A pick credited by the live listener before the nightly sync has recorded the watch: counting it
        # would put more than 100% of what was watched down to the rows.
        pick(sessions, user_id=1, tmdb_id=2, watched_days_ago=1)

        assert share(sessions) == {"watched": 0, "from_rows": 0, "rate": None}

    def test_a_transferred_watch_is_dated_by_when_it_really_happened(self, sessions):
        # A history transfer scrobbles "now" and keeps the true date in source_viewed_at; 2,000 titles
        # all "watched today" would bury every real watch in the window.
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)
        watched(sessions, user_id=1, tmdb_id=1, days_ago=0, source_days_ago=900)
        watched(sessions, user_id=1, tmdb_id=2, days_ago=5)

        assert share(sessions)["watched"] == 1

    def test_all_time_starts_at_each_persons_first_pick(self, sessions):
        watched(sessions, user_id=1, tmdb_id=1, days_ago=400)  # years of history from before their first row
        watched(sessions, user_id=1, tmdb_id=2, days_ago=50)
        pick(sessions, user_id=1, tmdb_id=2, watched_days_ago=50)

        assert share(sessions, "all") == {"watched": 1, "from_rows": 1, "rate": 1.0}

    def test_a_window_longer_than_shortlist_has_run_starts_at_the_install_too(self, sessions):
        # Found on a real server: the 90-day window counted 1,412 titles watched against 804 for all time,
        # because 90 days reached back before the first run — viewing no row could have been part of.
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)  # first delivery 59 days ago
        watched(sessions, user_id=1, tmdb_id=1, days_ago=80)
        watched(sessions, user_id=1, tmdb_id=2, days_ago=50)

        assert share(sessions, "90") == share(sessions, "all") == {"watched": 1, "from_rows": 0, "rate": 0.0}

    def test_a_person_counts_from_their_own_first_delivery_not_the_servers(self, sessions):
        # Someone enabled last week: their viewing from before that is not something a row showed them,
        # and counting it put 20 unrelated watches under one real credit.
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)
        pick(sessions, user_id=2, tmdb_id=1, watched_days_ago=2, delivered_days_ago=6)
        for tmdb_id in range(10, 30):
            watched(sessions, user_id=2, tmdb_id=tmdb_id, days_ago=20)
        watched(sessions, user_id=2, tmdb_id=1, days_ago=2)

        assert share(sessions) == {"watched": 1, "from_rows": 1, "rate": 1.0}

    def test_a_shared_row_credit_does_not_decide_who_is_counted(self, sessions):
        # Starting someone with no picks at their first shared-row credit selects people by the outcome:
        # the one who watched from the row counts from exactly that watch, and 50 days of other viewing
        # vanish — a real 1 of 51 reported as 100%.
        for tmdb_id in range(10, 60):
            watched(sessions, user_id=2, tmdb_id=tmdb_id, days_ago=20)
        watched(sessions, user_id=2, tmdb_id=1, days_ago=5)
        with sessions() as session:
            session.add(
                SharedRowWatch(
                    user_id=2,
                    collection_slug="popular",
                    tmdb_id=1,
                    media_type="movie",
                    watched_at=NOW - timedelta(days=5),
                )
            )
            session.commit()

        assert share(sessions) == {"watched": 0, "from_rows": 0, "rate": None}

    def test_someone_never_delivered_to_counts_on_neither_side(self, sessions):
        watched(sessions, user_id=2, tmdb_id=1, days_ago=5)

        assert share(sessions) == {"watched": 0, "from_rows": 0, "rate": None}

    def test_a_film_and_a_show_sharing_a_tmdb_id_are_different_titles(self, sessions):
        # TMDB numbers movies and shows separately, so movie 1 and show 1 are unrelated.
        pick(sessions, user_id=1, tmdb_id=1, watched_days_ago=5, media_type="show")
        watched(sessions, user_id=1, tmdb_id=1, days_ago=5, media_type="movie")

        assert share(sessions) == {"watched": 1, "from_rows": 0, "rate": 0.0}

    def test_a_title_plex_could_not_match_to_tmdb_still_counts_as_watched_once(self, sessions):
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)
        watched(sessions, user_id=1, tmdb_id=None, days_ago=5, rating_key=77, title="Home Video")
        watched(sessions, user_id=1, tmdb_id=None, days_ago=5, rating_key=78, title="Other Video")
        # The same unmatched film in a second library: a ratingKey is only unique within one library.
        watched(sessions, user_id=1, tmdb_id=None, days_ago=5, rating_key=12, title="Home Video", section_key="4")

        assert share(sessions)["watched"] == 2

    @pytest.mark.parametrize("window", ["7", "30", "90", "all"])
    def test_a_series_started_from_a_row_before_the_window_still_counts_while_they_watch_it(self, sessions, window):
        """Both sides must be the same title-in-window. `watched` is dated by the LATEST view, which moves
        with every new episode, while the credit is stamped once, on the first watch. Windowing the
        credit dropped a series someone started from their row 40 days ago and is still watching out of
        "from rows" on the 7- and 30-day windows, and only there."""
        pick(sessions, user_id=1, tmdb_id=100, watched_days_ago=40, delivered_days_ago=55, media_type="show")
        watched(sessions, user_id=1, tmdb_id=100, days_ago=2, media_type="show", section_key="2")
        watched(sessions, user_id=1, tmdb_id=200, days_ago=3)

        assert share(sessions, window) == {"watched": 2, "from_rows": 1, "rate": 0.5}

    def test_a_film_from_their_row_rewatched_in_the_window_counts_as_from_the_row(self, sessions):
        pick(sessions, user_id=1, tmdb_id=2, watched_days_ago=50)
        watched(sessions, user_id=1, tmdb_id=2, days_ago=4)

        assert share(sessions, "7") == {"watched": 1, "from_rows": 1, "rate": 1.0}

    def test_a_shared_row_credit_before_the_window_counts_for_a_title_watched_in_it(self, sessions):
        pick(sessions, user_id=1, tmdb_id=9, watched_days_ago=None)
        watched(sessions, user_id=1, tmdb_id=1, days_ago=5, media_type="show", section_key="2")
        with sessions() as session:
            session.add(
                SharedRowWatch(
                    user_id=1,
                    collection_slug="popular",
                    tmdb_id=1,
                    media_type="show",
                    watched_at=NOW - timedelta(days=45),
                )
            )
            session.commit()

        assert share(sessions) == {"watched": 1, "from_rows": 1, "rate": 1.0}
