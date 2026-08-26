"""Did they FINISH it, or just start it? — `WatchedItem.is_finished` and `picks.finished_at`.

`watched_at` comes from Plex's binary watched flag. For a movie that flag means played; for a SERIES
it flips on the first finished episode, so one episode of a 60-episode show has always scored
identically to a whole film. Measured on a real 47-user server (2026-08-16): of 158 show picks
credited as watched, only 21 were actually finished.

The threshold for a series is OURS by necessity — Plex publishes no show-level watched flag, only
`viewedLeafCount`/`leafCount` (`tests/fixtures/pms_watched_shows.xml.txt` records `2/176` coming back
as "watched"). These tests pin the strictest reading: every episode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.server.db.models import Base, Collection, Delivery, PickRow, User
from shortlist.server.services.run_persistence import live_pick_ids, reconcile_watched

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def watched_item(media_type: MediaType, *, tmdb_id: int = 100, viewed=None, leaf=None, when=None) -> WatchedItem:
    return WatchedItem(
        title="T",
        media_type=media_type,
        watched_at=when or NOW,
        tmdb_id=tmdb_id,
        viewed_leaf_count=viewed,
        leaf_count=leaf,
    )


class TestIsFinished:
    """The matrix that matters: type x episode counts. Every cell drives different reporting."""

    def test_a_movie_is_finished_because_plex_only_reports_it_once_played(self):
        assert watched_item(MediaType.MOVIE).is_finished is True

    def test_a_series_with_every_episode_watched_is_finished(self):
        assert watched_item(MediaType.SHOW, viewed=12, leaf=12).is_finished is True

    def test_a_series_watched_beyond_its_episode_count_is_finished(self):
        """Observed live: an on-deck item read 145% of its duration. Counts can exceed their total
        (a re-scanned library, a bulk mark), and `>=` must not turn that into 'unfinished'."""
        assert watched_item(MediaType.SHOW, viewed=14, leaf=12).is_finished is True

    @pytest.mark.parametrize("viewed", [1, 2, 3, 11])
    def test_a_series_short_of_its_last_episode_is_not_finished(self, viewed):
        """3 of 12 is the case the engine calls 'already seen' (its bar is min(80%, max(3, 15%))).
        Reporting deliberately disagrees: 'engaged enough not to re-recommend' is not 'finished'."""
        assert watched_item(MediaType.SHOW, viewed=viewed, leaf=12).is_finished is False

    def test_the_one_episode_case_that_plex_itself_calls_watched(self):
        """`unwatched=0` returns a series from its first episode — the whole reason this exists."""
        assert watched_item(MediaType.SHOW, viewed=1, leaf=176).is_finished is False

    @pytest.mark.parametrize("leaf", [None, 0])
    def test_a_series_with_no_known_episode_total_is_not_finished(self, leaf):
        """The opposite lean to the already-watched rule, which counts an unknown total as watched to
        avoid re-recommending. Here 'we cannot show they finished it' must not become a claim."""
        assert watched_item(MediaType.SHOW, viewed=9, leaf=leaf).is_finished is False

    def test_a_series_reporting_zero_watched_episodes_is_not_finished(self):
        assert watched_item(MediaType.SHOW, viewed=0, leaf=12).is_finished is False


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


#: The run that last delivered alex's row — its picks are what `live_pick_ids` calls live.
LIVE_RUN = 7
#: An earlier delivery of the SAME row: on their shelf then, swapped out since.
STALE_RUN = 6


@pytest.fixture
def user(sessions):
    with sessions() as session:
        session.add(User(id=1, plex_account_id=99, username="alex", slug="alex"))
        # A pick is only creditable while its row still exists, so these tests need a real one.
        session.add(Collection(id=1, slug="picked", name="Picked for You"))
        session.commit()
    return "alex"


def add_pick(
    sessions,
    *,
    tmdb_id: int,
    media_type: str,
    created: datetime,
    watched=None,
    finished=None,
    live: bool = True,
    slug: str = "picked",
) -> int:
    """One pick in one of alex's rows, with the delivery-ledger entry a real delivery leaves behind.

    The ledger entry is not decoration: liveness requires the collection to still be ON PLEX, and the
    ledger is the only record of that (`live_pick_ids`). A pick with no ledger row is a row Plex no
    longer has.

    `live=False` files it under an EARLIER run of the same row and puts a different title in the
    current one — the row rebuilt and dropped this title, which is exactly the case the membership
    rule exists to reject. Both picks are needed: with only the old delivery on record, that run
    WOULD be the newest one for the row, and the pick would read as live.
    """
    with sessions() as session:
        if session.get(Delivery, (slug, "alex", "1")) is None:
            session.add(Delivery(collection_slug=slug, user_slug="alex", library_key="1", rating_key=tmdb_id))
        pick = PickRow(
            user_id=1,
            run_id=LIVE_RUN if live else STALE_RUN,
            collection_slug=slug,
            section_key="1",
            library="Movies",
            tmdb_id=tmdb_id,
            media_type=media_type,
            rating_key=1,
            rank=1,
            created_at=created,
            watched_at=watched,
            finished_at=finished,
        )
        session.add(pick)
        if not live:
            session.add(
                PickRow(
                    user_id=1,
                    run_id=LIVE_RUN,
                    collection_slug=slug,
                    section_key="1",
                    library="Movies",
                    tmdb_id=999_000 + tmdb_id,
                    media_type=media_type,
                    rating_key=2,
                    rank=1,
                    created_at=created + timedelta(days=1),
                )
            )
        session.commit()
        return pick.id


def read(sessions, pick_id: int) -> PickRow:
    with sessions() as session:
        return session.query(PickRow).filter_by(id=pick_id).one()


def profile(history: list[WatchedItem]) -> UserProfile:
    return UserProfile(
        username="alex",
        plex_account_id=99,
        user_type=UserType.SHARED,
        slug="alex",
        history=history,
    )


class TestReconcileStampsFinished:
    def test_a_watched_movie_is_stamped_watched_and_finished_together(self, sessions, user):
        delivered = NOW - timedelta(days=3)
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=delivered)

        reconcile_watched(
            sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW - timedelta(days=1))])]
        )

        pick = read(sessions, pid)
        assert pick.watched_at is not None
        assert pick.finished_at == pick.watched_at, "a movie has no middle state — one event, both columns"

    def test_a_series_credited_on_one_episode_is_watched_but_not_finished(self, sessions, user):
        """The bug in one assertion: this pick counts as a hit and must NOT count as finished."""
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=3))

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=1, leaf=60, when=NOW - timedelta(days=1))])],
        )

        pick = read(sessions, pid)
        assert pick.watched_at is not None
        assert pick.finished_at is None

    def test_a_series_watched_out_is_stamped_finished(self, sessions, user):
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=3))

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=60, leaf=60, when=NOW - timedelta(days=1))])],
        )

        assert read(sessions, pid).finished_at is not None

    def test_an_already_credited_series_is_stamped_when_it_later_completes(self, sessions, user):
        """The forward-fill path. This pick was credited weeks ago and carries no `finished_at`; the
        migration deliberately leaves series NULL, so the only way it ever fills is right here."""
        old_watch = NOW - timedelta(days=40)
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=50), watched=old_watch)

        last_episode = NOW - timedelta(days=1)
        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=12, leaf=12, when=last_episode)])],
        )

        pick = read(sessions, pid)
        assert pick.finished_at is not None
        assert pick.watched_at.replace(tzinfo=UTC) == old_watch, "crediting must not move — only completion is new"

    def test_completion_is_dated_by_the_last_episode_not_by_tonights_sync(self, sessions, user):
        """Stamping `now` would file an old completion in this week's trend bucket. The show's own
        `lastViewedAt` IS when they finished it, because it is the most recent episode they watched."""
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=20))
        last_episode = NOW - timedelta(days=9)

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=8, leaf=8, when=last_episode)])],
        )

        assert read(sessions, pid).finished_at.replace(tzinfo=UTC) == last_episode

    def test_a_watch_before_delivery_stamps_neither_column(self, sessions, user):
        """Recommending something they had already seen is not a hit, and must not become a finish."""
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=2))

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=12, leaf=12, when=NOW - timedelta(days=30))])],
        )

        pick = read(sessions, pid)
        assert pick.watched_at is None
        assert pick.finished_at is None

    def test_a_title_still_in_the_row_is_credited_however_long_it_has_sat_there(self, sessions, user):
        """There is no clock any more. A row that never refreshes redelivers the same pick for
        months, and every night of that is a night they were shown it — the old 30-day cutoff called
        a watch on day 40 a miss while the title was on their shelf the whole time."""
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=120))

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, pid).watched_at is not None

    def test_a_movie_and_a_series_sharing_a_tmdb_id_are_kept_apart(self, sessions, user):
        """A TMDB id is unique only within its namespace. If the key dropped media_type, finishing the
        film would mark the series finished too."""
        movie = add_pick(sessions, tmdb_id=42, media_type="movie", created=NOW - timedelta(days=3))
        show = add_pick(sessions, tmdb_id=42, media_type="show", created=NOW - timedelta(days=3))

        reconcile_watched(
            sessions,
            [
                profile(
                    [
                        watched_item(MediaType.MOVIE, tmdb_id=42, when=NOW - timedelta(days=1)),
                        watched_item(MediaType.SHOW, tmdb_id=42, viewed=2, leaf=20, when=NOW - timedelta(days=1)),
                    ]
                )
            ],
        )

        assert read(sessions, movie).finished_at is not None
        assert read(sessions, show).watched_at is not None
        assert read(sessions, show).finished_at is None


class TestOnlyLiveRowsAreCredited:
    """The membership rule: a watch counts only if the title was in one of their rows at the time.

    Before this, credit was a 30-day clock from delivery with no membership test — so a title the row
    had dropped weeks earlier still scored as a hit, on a shelf that no longer showed it.
    """

    def test_a_title_in_their_current_row_counts(self, sessions, user):
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, pid).watched_at is not None

    def test_a_title_the_row_has_since_dropped_does_not(self, sessions, user):
        """Delivered, swapped out on the next rebuild, watched afterwards. They cannot have started it
        from a row that no longer lists it, and the old 30-day window credited this for four weeks."""
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=5), live=False)

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        pick = read(sessions, pid)
        assert pick.watched_at is None
        assert pick.finished_at is None

    def test_a_deleted_row_is_not_a_live_row(self, sessions, user):
        """The collection is off the server, so nothing can be watched from it — but its picks are
        kept (they are the only record of what was recommended) and are still the newest for their
        (user, row, library) group, so nothing else would rule them out."""
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))
        with sessions() as session:
            session.query(Collection).filter_by(slug="picked").delete()
            session.commit()

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, pid).watched_at is None

    def test_a_row_that_did_not_run_tonight_is_still_live(self, sessions, user):
        """Rows carry their own crons, so the newest run is routinely scoped to ONE row. Taking the
        newest run overall — rather than the newest per (user, row, library) — would read every row
        that didn't run tonight as empty, and stop crediting most of the server."""
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=9))
        with sessions() as session:
            session.add(Collection(id=2, slug="fresh", name="Fresh"))
            session.add(
                PickRow(
                    user_id=1,
                    run_id=LIVE_RUN + 1,  # tonight's run, which built the OTHER row
                    collection_slug="fresh",
                    section_key="1",
                    library="Movies",
                    tmdb_id=555,
                    media_type="movie",
                    rating_key=3,
                    rank=1,
                    created_at=NOW,
                )
            )
            session.commit()

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, pid).watched_at is not None

    def test_completion_lands_even_though_the_row_has_dropped_the_title(self, sessions, user):
        """Being watched is what REMOVES a title from a row, so re-testing membership when they
        finish a series months later would refuse to upgrade a single 'started' to 'finished'."""
        credited = NOW - timedelta(days=40)
        pid = add_pick(
            sessions,
            tmdb_id=200,
            media_type="show",
            created=NOW - timedelta(days=50),
            watched=credited,
            live=False,
        )

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=12, leaf=12, when=NOW - timedelta(days=1))])],
        )

        pick = read(sessions, pid)
        assert pick.finished_at is not None
        assert pick.watched_at.replace(tzinfo=UTC) == credited, "crediting must not move — only completion is new"

    def test_a_caller_supplied_snapshot_beats_what_the_rows_say_now(self, sessions, user):
        """A run rebuilds the rows BEFORE the reconcile, so by then the title is gone from the row —
        removed precisely because it was watched. The run hands in the pre-rebuild snapshot, and
        without that every genuine hit would score zero. This is the whole ordering fix."""
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=5), live=False)
        # As it was before tonight's rebuild: the title WAS on their shelf.
        snapshot = {1: {pid}}

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])], snapshot)

        assert read(sessions, pid).watched_at is not None

    def test_live_pick_ids_is_empty_when_no_row_has_ever_delivered(self, sessions, user):
        with sessions() as session:
            assert live_pick_ids(session) == {}


class TestTheCreditIsVisibleToTheReport:
    """The report intersects at ROW level, so where the stamp lands decides what it can see.

    `_landing` and `row_effectiveness` choose a matured cohort by `created_at` and then count the rows
    in it that also carry `watched_at`. Credit only the newest delivery and the stamp is by
    construction outside a cohort that ends `HIT_WINDOW_DAYS` ago, while the same title's older rows
    sit inside it feeding `delivered` — a real hit reads as a miss.
    """

    def test_earlier_deliveries_of_the_same_title_are_stamped_too(self, sessions, user):
        # The same title redelivered on three consecutive nights, the newest being what is on Plex.
        old = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=40), live=False)
        mid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=20), live=False)
        current = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, current).watched_at is not None
        assert read(sessions, old).watched_at is not None, "the 40-day-old row is the one a matured cohort contains"
        assert read(sessions, mid).watched_at is not None

    def test_a_delivery_made_after_the_watch_is_not_back_stamped(self, sessions, user):
        """Bounded on purpose: a row that put the title up AFTER they watched it cannot be why."""
        watched_when = NOW - timedelta(days=5)
        later = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))
        earlier = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=30), live=False)

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=watched_when)])])

        assert read(sessions, earlier).watched_at is not None
        assert read(sessions, later).watched_at is None, "delivered after the watch — not evidence of anything"

    def test_a_finished_series_spreads_both_columns(self, sessions, user):
        old = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=40), live=False)
        add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=1))

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=10, leaf=10, when=NOW)])],
        )

        pick = read(sessions, old)
        assert pick.watched_at is not None
        assert pick.finished_at is not None, "the finished cohort intersects at row level too"


class TestARewatchRowCannotClaimAnOldWatch:
    """A row with `excludes_watched` off leads with titles they HAVE seen — its whole population is
    old watches. The floor for "is this watch newer than the recommendation" is therefore per ROW: a
    per-user floor lets such a row inherit some other row's older delivery and credit a watch from
    years before it existed, writing `watched_at` earlier than `created_at`.
    """

    def test_a_watch_predating_this_rows_first_delivery_is_not_credited(self, sessions, user):
        long_ago = NOW - timedelta(days=400)
        # Delivered by the normal row two years back, and watched a year after that.
        add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=700), live=False)
        with sessions() as session:
            session.add(Collection(id=2, slug="rewatch", name="Watch it again"))
            session.commit()
        add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1), slug="rewatch")

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=long_ago)])])

        # Asserted over EVERY row for the title, not just the rewatch pick. A credit decided on one
        # row is written to the title's earlier rows too (`_spread_credit`), so checking only the
        # deciding row passes even when the decision is wrong — the stamp simply lands next door.
        with sessions() as session:
            stamped = session.query(PickRow).filter(PickRow.tmdb_id == 100, PickRow.watched_at.isnot(None)).all()
        assert not stamped, "no row was why they watched it a year before the rewatch row existed"

    def test_no_pick_is_ever_stamped_with_a_watch_older_than_its_own_delivery(self, sessions, user):
        """The invariant behind the case above, asserted directly: `watched_at >= created_at`, always."""
        add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=700), live=False)
        with sessions() as session:
            session.add(Collection(id=2, slug="rewatch", name="Watch it again"))
            session.commit()
        add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1), slug="rewatch")

        reconcile_watched(
            sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW - timedelta(days=400))])]
        )

        with sessions() as session:
            for pick in session.query(PickRow).filter(PickRow.watched_at.isnot(None)).all():
                assert pick.watched_at >= pick.created_at, f"{pick.tmdb_id} in {pick.collection_slug}"


class TestLivenessMeansOnPlexNotMerelyConfigured:
    def test_a_row_plex_no_longer_has_is_not_live(self, sessions, user):
        """A muted or retired row, or one a cold start skipped, has its collection DELETED and its
        ledger entry forgotten — while the `collections` row stays so the owner can switch it back on,
        and no later run re-delivers the group to move its MAX run_id. Testing `collections` alone
        would leave those picks creditable for ever."""
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))
        with sessions() as session:
            session.query(Delivery).delete()
            session.commit()

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, pid).watched_at is None

    def test_a_disabled_row_is_not_live(self, sessions, user):
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))
        with sessions() as session:
            session.query(Collection).filter_by(slug="picked").update({"enabled": False})
            session.commit()

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, pid).watched_at is None

    def test_picks_detached_from_their_run_are_not_live(self, sessions, user):
        """`DELETE /api/runs` and the retention prune both null every pick's `run_id`. The documented
        cost is that such picks stop being creditable until their row next delivers."""
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))
        with sessions() as session:
            session.query(PickRow).update({"run_id": None})
            session.commit()

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, pid).watched_at is None

    def test_a_group_recovers_the_moment_its_row_delivers_again(self, sessions, user):
        """Detachment is not permanent. Both paths that null `run_id` are wholesale — `DELETE /api/runs`
        nulls every pick, and the retention prune takes the OLDEST runs — so a group can never end up
        with its newest delivery detached and an older one surviving to be mistaken for live. The next
        delivery re-stamps the group and crediting resumes.
        """
        with sessions() as session:
            session.query(PickRow).update({"run_id": None})
            session.commit()
        fresh = add_pick(sessions, tmdb_id=100, media_type="movie", created=NOW - timedelta(days=1))

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        assert read(sessions, fresh).watched_at is not None
