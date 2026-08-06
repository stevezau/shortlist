"""Watch-history: the share-token source's token matrix + orchestration, plus seed derivation."""

from __future__ import annotations

from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.plex_pms import WatchedRead
from shortlist.engine.history import (
    NoWatchToken,
    ShareTokenWatchSource,
    derive_seeds,
    disliked_seed_keys,
    distinct_recent,
    ratings_are_trustworthy,
)
from shortlist.engine.models import MediaType, UserType
from tests.conftest import make_profile, make_watched


def _section(key: str, section_type: str) -> SimpleNamespace:
    """A stand-in for a plexapi library section — the source only reads `.key` and `.type`."""
    return SimpleNamespace(key=key, type=section_type)


def _read(*items) -> WatchedRead:
    """What the PMS client hands back. `covers_window` is False throughout: `fetch` always asks
    for everything (`since=None`), so there is no window to have covered."""
    return WatchedRead(items=list(items), covers_window=False)


class TestShareTokenWatchSource:
    """The one watch source: reads each user's COMPLETE watched set from the PMS as that user.

    The value under test is which token it reads AS (the user_type matrix) and that it aggregates
    every library fail-soft — the XML→WatchedItem parsing lives in test_clients (PMS `watched_titles`).
    """

    def _source(self, mock_plex, mock_plextv, *, owner_token: str = "OWNER-TOK") -> ShareTokenWatchSource:
        # Two libraries, one of each type, so fetch() must iterate both and pick the media type per one.
        mock_plex._server.library.sections.return_value = [_section("1", "movie"), _section("2", "show")]
        mock_plex.watched_titles = MagicMock(return_value=_read())
        return ShareTokenWatchSource(mock_plex, mock_plextv, owner_token=owner_token)

    def test_owner_is_read_with_the_admin_token_never_the_shared_roster(self, mock_plex, mock_plextv):
        """The owner isn't shared to their own server, so they aren't in shared_servers — read their
        own watched state with the admin token, and don't waste a roster call to discover that."""
        source = self._source(mock_plex, mock_plextv)
        source.fetch(make_profile(username="steve", user_type=UserType.OWNER, account_id=1), min_completion=0.7)

        tokens_used = {call.args[2] for call in mock_plex.watched_titles.call_args_list}
        assert tokens_used == {"OWNER-TOK"}
        mock_plextv.shared_server_tokens.assert_not_called()

    def test_a_shared_user_is_read_with_their_own_roster_token(self, mock_plex, mock_plextv):
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {100: "SARAH-TOK", 200: "OTHER-TOK"}

        source.fetch(make_profile(username="sarah", user_type=UserType.SHARED, account_id=100), min_completion=0.7)

        tokens_used = {call.args[2] for call in mock_plex.watched_titles.call_args_list}
        assert tokens_used == {"SARAH-TOK"}  # never the owner's, never another user's
        mock_plextv.canary_server_token.assert_not_called()  # already in the roster — no switch needed

    def test_the_roster_is_fetched_once_and_reused_across_users(self, mock_plex, mock_plextv):
        """One shared_servers call covers the whole roster; a 40-user run must not call it 40 times."""
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {100: "A", 200: "B"}

        source.fetch(make_profile(username="a", account_id=100), min_completion=0.7)
        source.fetch(make_profile(username="b", account_id=200), min_completion=0.7)

        mock_plextv.shared_server_tokens.assert_called_once()

    def test_a_managed_user_absent_from_the_roster_is_read_via_a_switched_token(self, mock_plex, mock_plextv):
        """A managed sub-account with no share invite of its own isn't in shared_servers — the source
        switches to it and exchanges for a server token (the canary path)."""
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {}  # not shared to it directly
        mock_plextv.canary_server_token.return_value = "KID-TOK"

        source.fetch(make_profile(username="kid", user_type=UserType.MANAGED, account_id=200), min_completion=0.7)

        mock_plextv.canary_server_token.assert_called_once_with(200)
        tokens_used = {call.args[2] for call in mock_plex.watched_titles.call_args_list}
        assert tokens_used == {"KID-TOK"}

    def test_no_obtainable_token_yields_empty_history_and_reads_nothing(self, mock_plex, mock_plextv):
        """If neither the roster nor a switch can produce a token, fail soft to "nothing watched" (it
        may re-surface a title they've seen) rather than crash the run — and never read the PMS."""
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {}
        mock_plextv.canary_server_token.side_effect = PermissionError("PIN-protected")

        result = source.fetch(
            make_profile(username="pin", user_type=UserType.MANAGED, account_id=200), min_completion=0.7
        )

        assert result == []
        mock_plex.watched_titles.assert_not_called()

    def test_a_missing_token_raises_from_fetch_section_rather_than_reading_as_empty(self, mock_plex, mock_plextv):
        """`fetch` may fail soft; `fetch_section` must not. It feeds the watched-title CACHE, which
        treats what a read returns as the truth for the window it covers and deletes the rest — so
        "plex.tv would not mint a token just now" arriving as "they have watched nothing" wipes the
        history it was meant to refresh and stamps the sync a success."""
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {}
        mock_plextv.canary_server_token.side_effect = PermissionError("PIN-protected")
        profile = make_profile(username="pin", user_type=UserType.MANAGED, account_id=200)

        with pytest.raises(NoWatchToken):
            source.fetch_section(profile, _section("1", "movie"), MediaType.MOVIE)

        mock_plex.watched_titles.assert_not_called()

    def test_the_owner_still_reads_with_the_admin_token_rather_than_raising(self, mock_plex, mock_plextv):
        """The OWNER row of the matrix is NOT vacuous here: `_token_for` returns the admin token
        without consulting the roster, so it takes a different path to the raise above and must keep
        reading normally. (SHARED-with-a-roster-miss collapses onto the MANAGED row — both fall
        through to the same canary exchange and the same `token is None`.)"""
        source = self._source(mock_plex, mock_plextv)
        profile = make_profile(username="steve", user_type=UserType.OWNER, account_id=1)

        source.fetch_section(profile, _section("1", "movie"), MediaType.MOVIE)

        assert {call.args[2] for call in mock_plex.watched_titles.call_args_list} == {"OWNER-TOK"}

    def test_selects_the_media_type_per_library_and_aggregates_across_them(self, mock_plex, mock_plextv):
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {100: "SARAH-TOK"}
        mock_plex.watched_titles.side_effect = lambda key, mt, tok, since=None: (
            _read(make_watched("Heat", media_type=MediaType.MOVIE))
            if mt is MediaType.MOVIE
            else _read(make_watched("Suits", media_type=MediaType.SHOW))
        )

        items = source.fetch(make_profile(account_id=100), min_completion=0.7)

        # Movie library read as type=movie, show library as type=show, results merged.
        by_key = {call.args[0]: call.args[1] for call in mock_plex.watched_titles.call_args_list}
        assert by_key == {"1": MediaType.MOVIE, "2": MediaType.SHOW}
        assert {(i.title, i.media_type) for i in items} == {
            ("Heat", MediaType.MOVIE),
            ("Suits", MediaType.SHOW),
        }
        # A direct engine run holds no state between runs, so it has nothing to be incremental
        # against — it must always ask for everything.
        assert all(call.kwargs.get("since") is None for call in mock_plex.watched_titles.call_args_list)

    def test_one_unreadable_library_degrades_to_empty_without_failing_the_others(self, mock_plex, mock_plextv):
        """A single library erroring must not lose the user's whole history — the other library's
        titles still come back (fail-soft per library, matching the old sources' stance)."""
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {100: "SARAH-TOK"}

        def read(key, media_type, token, since=None):
            if media_type is MediaType.MOVIE:
                raise RuntimeError("section unreadable")
            return _read(make_watched("Suits", media_type=MediaType.SHOW))

        mock_plex.watched_titles.side_effect = read
        items = source.fetch(make_profile(account_id=100), min_completion=0.7)

        assert [i.title for i in items] == ["Suits"]

    def test_an_unshared_library_is_skipped_without_losing_the_shared_ones(self, mock_plex, mock_plextv):
        """A 403 means the owner never shared that library — "nothing watched there" is correct.

        `sections()` is the OWNER's list, so this is the normal state for anyone with a partial
        share, not an error worth warning about on every read.
        """
        from shortlist.engine.clients.plex_pms import SectionNotShared

        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {100: "SARAH-TOK"}

        def read(key, media_type, token, since=None):
            if media_type is MediaType.SHOW:
                raise SectionNotShared("section 12 is not shared with this user")
            return _read(make_watched("Dune", media_type=MediaType.MOVIE))

        mock_plex.watched_titles.side_effect = read
        items = source.fetch(make_profile(account_id=100), min_completion=0.7)

        assert [i.title for i in items] == ["Dune"]


class TestDistinctRecent:
    def test_a_binge_collapses_to_one_entry_and_lets_variety_through(self):
        # 20 episodes of one show + a few other titles. The naive "last N raw watches" would be all
        # one show; distinct_recent must collapse the binge to a single entry so variety survives.
        history = [make_watched("Suits", days_ago=i, media_type=MediaType.SHOW) for i in range(20)]
        history += [
            make_watched("Heat", days_ago=1, media_type=MediaType.MOVIE),
            make_watched("Dune", days_ago=2, media_type=MediaType.MOVIE),
        ]
        titles = [w.title for w in distinct_recent(history, limit=5)]
        assert titles.count("Suits") == 1
        assert set(titles) == {"Suits", "Heat", "Dune"}

    def test_looks_back_past_a_binge_to_fill_distinct_titles(self):
        # Only look at the 3 most-recent RAW watches and you'd see one show; distinct_recent looks
        # deeper to reach the requested number of distinct titles.
        history = [make_watched("Suits", days_ago=i, media_type=MediaType.SHOW) for i in range(3)]
        history += [make_watched(f"Movie {n}", days_ago=10 + n, media_type=MediaType.MOVIE) for n in range(4)]
        got = distinct_recent(history, limit=4)
        assert len(got) == 4
        assert got[0].title == "Suits"  # most recent distinct title first
        assert {w.title for w in got[1:]} == {"Movie 0", "Movie 1", "Movie 2"}

    def test_a_movie_and_a_show_with_the_same_name_stay_separate(self):
        history = [
            make_watched("Fargo", days_ago=1, media_type=MediaType.SHOW),
            make_watched("Fargo", days_ago=2, media_type=MediaType.MOVIE),
        ]
        assert len(distinct_recent(history, limit=5)) == 2


class TestDeriveSeeds:
    def test_weight_is_pure_recency_so_a_recent_watch_outranks_an_old_favorite(self):
        # Weight is recency-only: a title watched once yesterday must outrank an old favourite
        # rewatched many times years ago (the SFLIX/MooHouse bug — The Girl on the Train, 18x but
        # ~8.7 years ago, dominated the seeds over titles watched this week).
        history = [
            make_watched("Old Favorite", days_ago=3000, watch_count=18),
            make_watched("Watched Yesterday", days_ago=1, watch_count=1),
        ]
        ids = {("Old Favorite", MediaType.MOVIE): 1, ("Watched Yesterday", MediaType.MOVIE): 2}
        seeds = derive_seeds(history, lambda w: ids.get((w.title, w.media_type)))
        assert seeds[0].title == "Watched Yesterday"
        assert seeds[0].weight > seeds[1].weight

    def test_seed_order_matches_recency_regardless_of_watch_count(self):
        # Because weight is strictly monotonic in recency, the seed ORDER is exactly the recent-watches
        # order — which is what makes the trace's "seeds" list match its "what they watched" panel. A
        # heavily-rewatched show does NOT jump ahead of a more-recent single watch.
        history = [
            make_watched("Binge", days_ago=10, media_type=MediaType.SHOW, watch_count=50),
            make_watched("One Movie", days_ago=2, media_type=MediaType.MOVIE, watch_count=1),
        ]
        ids = {("Binge", MediaType.SHOW): 1, ("One Movie", MediaType.MOVIE): 2}
        seeds = derive_seeds(history, lambda w: ids[(w.title, w.media_type)])
        # watch_count is still carried for display ("watched 50x"), just not scored.
        binge = next(s for s in seeds if s.title == "Binge")
        assert binge.watch_count == 50
        assert seeds[0].title == "One Movie"  # more recent wins despite the 50-episode binge

    def test_a_blocked_title_never_becomes_a_seed(self):
        """A blocked title stays in their history — it just stops shaping what they are recommended."""
        history = [
            make_watched("One-Off", days_ago=1, tmdb_id=111),
            make_watched("Actually Them", days_ago=5, tmdb_id=222),
        ]

        seeds = derive_seeds(history, lambda _w: None, blocked={111})

        assert [s.tmdb_id for s in seeds] == [222], "the blocked title was the most recent watch"

    def test_blocking_frees_the_budget_for_the_next_title(self):
        """Blocking must not just null out a slot — otherwise a person with three blocked recent
        watches gets a row built from two seeds instead of the three they asked for."""
        history = [make_watched(f"T{i}", days_ago=i + 1, tmdb_id=i) for i in range(5)]

        seeds = derive_seeds(history, lambda _w: None, max_seeds=2, blocked={0, 1})

        assert [s.tmdb_id for s in seeds] == [2, 3]

    def test_an_items_own_tmdb_id_wins_over_the_resolver(self):
        # The share-token source inlines the tmdb_id from the PMS GUID, so derive_seeds must use it
        # and never consult the (index/search) resolver for that item — the resolver here would fail.
        history = [make_watched("Heat", tmdb_id=949)]

        def resolver(_w):
            raise AssertionError("resolver must not be called when the item carries its own tmdb_id")

        seeds = derive_seeds(history, resolver)
        assert [s.tmdb_id for s in seeds] == [949]

    def test_unresolvable_titles_are_skipped(self):
        seeds = derive_seeds([make_watched("Unknown")], lambda w: None)
        assert seeds == []

    def test_max_seeds_cap(self):
        history = [make_watched(f"Movie {i}", days_ago=i) for i in range(10)]
        ids = {f"Movie {i}": i + 1 for i in range(10)}
        seeds = derive_seeds(history, lambda w: ids[w.title], max_seeds=4)
        assert len(seeds) == 4

    def test_a_budget_of_one_yields_a_single_media_type(self):
        # Not a defect — one seed cannot be both — but it is WHY the row editor steers a
        # movies-and-TV row to 2 rather than 1 for a `{top_seed}` name. A `media="both"` row seeded
        # by one show gathers no movie candidates at all, so its Movies collection never builds.
        history = [make_watched("Show", days_ago=1, media_type=MediaType.SHOW)]
        history += [make_watched("Movie", days_ago=2, media_type=MediaType.MOVIE)]
        ids = {"Show": 1, "Movie": 2}

        one = derive_seeds(history, lambda w: ids[w.title], max_seeds=1)
        two = derive_seeds(history, lambda w: ids[w.title], max_seeds=2)

        assert {s.media_type for s in one} == {MediaType.SHOW}
        assert {s.media_type for s in two} == {MediaType.SHOW, MediaType.MOVIE}

    def test_a_window_of_one_is_always_the_most_recent_watch(self):
        """The default, and every caller's behaviour before cycling existed."""
        history = [make_watched(f"Movie {i}", days_ago=i) for i in range(5)]
        ids = {f"Movie {i}": i + 1 for i in range(5)}

        for offset in range(4):  # the offset is inert at window 1 — no accidental rotation
            seeds = derive_seeds(history, lambda w: ids[w.title], max_seeds=1, window=1, cycle_offset=offset)
            assert [s.title for s in seeds] == ["Movie 0"]

    def test_a_window_cycles_one_step_per_offset_and_covers_every_watch(self):
        """The point of the feature: consecutive runs never repeat, and each watch in the window gets
        its turn. A random pick would satisfy neither, and a repeat looks exactly like the stuck row
        this exists to fix (issue #57).

        Deliberately gives the seeds a TMDB-ID order that contradicts their recency order. The step is
        taken over the ID order, so a fixture where the two agree (ids ascending with recency, the
        obvious way to write this) passes whichever ordering the code actually uses and cannot tell a
        correct implementation from the one that shipped this bug.
        """
        history = [make_watched(f"Movie {i}", days_ago=i) for i in range(5)]
        ids = {"Movie 0": 903, "Movie 1": 105, "Movie 2": 511, "Movie 3": 42, "Movie 4": 777}

        led_by = [
            derive_seeds(history, lambda w: ids[w.title], max_seeds=1, window=3, cycle_offset=day)[0].title
            for day in range(6)
        ]

        assert set(led_by) == {"Movie 0", "Movie 1", "Movie 2"}, "the window is covered, not sampled"
        assert all(a != b for a, b in pairwise(led_by)), "consecutive runs must differ"
        assert led_by[:3] == led_by[3:], "the cycle repeats with the window's period, so nothing is skipped"

    def test_cycling_keeps_moving_for_someone_who_watches_every_night(self):
        """The regression that made cycling WORSE than leaving it off, for the people most likely to
        turn it on.

        Stepping over the RECENCY order cancels against their history: that list's head shifts by one
        each time they finish something, and the offset advances by one a night, so the same seed led
        for `window` nights running while their newer watches never led at all — C, C, C, F, F, F for
        a person whose newest watch went D, E, F, G, H. Indistinguishable from the stuck row of issue
        #57, which is what this feature was built to relieve.
        """
        titles = [f"Movie {i}" for i in range(8)]
        ids = {t: i + 1 for i, t in enumerate(titles)}

        led_by = []
        for night in range(6):
            watched = titles[: night + 3]  # one more finished film every night
            history = [make_watched(t, days_ago=len(watched) - 1 - i) for i, t in enumerate(watched)]
            led_by.append(
                derive_seeds(history, lambda w: ids[w.title], max_seeds=1, window=3, cycle_offset=night)[0].title
            )

        assert all(a != b for a, b in pairwise(led_by)), f"a nightly watcher's row must keep moving, got {led_by}"
        assert len(set(led_by)) >= 4, f"and must not orbit two titles while they watch six, got {led_by}"

    def test_cycling_a_movies_and_tv_row_follows_the_more_recent_type(self):
        """A one-seed `media=both` row keeps only the strongest lead, so the two media types' leads
        have to compete on recency. Listing movies first unconditionally made a TV watcher's row
        announce a film from a month ago the moment they turned cycling on."""
        history = [make_watched(f"Show {i}", days_ago=i, media_type=MediaType.SHOW) for i in range(3)]
        history += [make_watched(f"Film {i}", days_ago=30 + i, media_type=MediaType.MOVIE) for i in range(3)]
        ids = {f"Show {i}": 10 + i for i in range(3)} | {f"Film {i}": 50 + i for i in range(3)}

        led_by = [
            derive_seeds(history, lambda w: ids[w.title], max_seeds=1, window=3, cycle_offset=day)[0]
            for day in range(3)
        ]

        assert all(s.media_type is MediaType.SHOW for s in led_by), f"got {[(s.title) for s in led_by]}"

    def test_a_window_wider_than_the_history_degrades_to_what_they_watched(self):
        """Someone with two watches and a window of five cycles between the two, rather than
        returning nothing on the days the window points past the end of their history."""
        history = [make_watched(f"Movie {i}", days_ago=i) for i in range(2)]
        ids = {f"Movie {i}": i + 1 for i in range(2)}

        led_by = [
            derive_seeds(history, lambda w: ids[w.title], max_seeds=1, window=5, cycle_offset=day)[0].title
            for day in range(4)
        ]

        assert led_by == ["Movie 0", "Movie 1", "Movie 0", "Movie 1"]

    def test_cycling_advances_both_media_types_of_a_movies_and_tv_row(self):
        """Cycling is per media type, not across the flat list. A `media=both` row seeds one movie and
        one show, so rotating the flat list would spend the whole window on whichever type this person
        watches more of and pin the other half to its newest title for ever."""
        history = [make_watched(f"Show {i}", days_ago=i, media_type=MediaType.SHOW) for i in range(3)]
        history += [make_watched(f"Movie {i}", days_ago=i, media_type=MediaType.MOVIE) for i in range(3)]
        ids = {f"Show {i}": i + 1 for i in range(3)} | {f"Movie {i}": 100 + i for i in range(3)}

        def led_by(day: int) -> dict:
            seeds = derive_seeds(history, lambda w: ids[w.title], max_seeds=2, window=3, cycle_offset=day)
            return {s.media_type: s.title for s in seeds}

        day0, day1 = led_by(0), led_by(1)

        assert day0 == {MediaType.MOVIE: "Movie 0", MediaType.SHOW: "Show 0"}
        assert day1 == {MediaType.MOVIE: "Movie 1", MediaType.SHOW: "Show 1"}, "both halves advance, not just one"

    def test_reserves_seed_budget_for_the_minority_media_type(self):
        # A TV-heavy watcher: 20 recent shows + 3 older movies. The movies must still seed, or a
        # media=both row's Movies half starves (SFLIX/MooHouse: 58 of her last 60 watches were TV).
        history = [make_watched(f"Show {i}", days_ago=i, media_type=MediaType.SHOW) for i in range(20)]
        history += [make_watched(f"Movie {i}", days_ago=40 + i, media_type=MediaType.MOVIE) for i in range(3)]
        ids = {f"Show {i}": i + 1 for i in range(20)}
        ids |= {f"Movie {i}": 100 + i for i in range(3)}
        seeds = derive_seeds(history, lambda w: ids[w.title], max_seeds=10)
        assert len(seeds) == 10
        # All 3 movies survive the cap despite ranking below every show by weight — without the
        # per-media reserve the top 10 would be all shows and the movie row would get no candidates.
        assert sum(1 for s in seeds if s.media_type is MediaType.MOVIE) == 3


class TestRatingsAreTrustworthy:
    """The account-level guard: whose ratings are opinions, and whose were written by a tool.

    The case this exists for is real and was measured, not imagined — see
    `tests/fixtures/pms_watched_user_rating.xml.txt`.
    """

    def test_an_account_with_no_ratings_is_trusted(self):
        """Nothing to disbelieve. Matters because it is the state ~70% of real people are in, and a
        guard that failed closed here would switch the feature off for almost everyone."""
        assert ratings_are_trustworthy([]) is True

    def test_a_handful_of_whole_ratings_is_trusted(self):
        assert ratings_are_trustworthy([10.0, 8.0, 2.0, 6.0, 10.0]) is True

    def test_an_account_of_mostly_fractional_ratings_is_not(self):
        """The Kometa shape: scores copied off IMDb, which land on decimals."""
        assert ratings_are_trustworthy([7.9, 8.8, 6.2, 5.4, 6.0, 9.1]) is False

    def test_it_abstains_below_five_ratings(self):
        """One stray fractional value must not condemn a real rater. A tool writes thousands, so a
        tiny sample is never evidence of one — and real raters are sparse (a median of 2 titles each
        across the 14 people on a live server who had rated anything at all)."""
        assert ratings_are_trustworthy([7.9]) is True
        assert ratings_are_trustworthy([7.9, 10.0, 8.0]) is True

    def test_a_mostly_human_account_survives_a_few_stray_values(self):
        """8 whole of 10 is at the floor — an owner who rates things AND once ran a sync script."""
        assert ratings_are_trustworthy([10.0] * 8 + [7.9, 6.2]) is True

    def test_nones_are_ignored_rather_than_counted_as_ratings(self):
        """`user_rating` is None for almost every watch. Counting those would put every account
        under the floor and disable the feature server-wide."""
        assert ratings_are_trustworthy([None] * 50 + [10.0, 8.0, 2.0, 6.0, 4.0]) is True


class TestDislikedSeedIds:
    """Which watched titles stop seeding — the matrix of rating x trust x threshold."""

    def _rated(self, *pairs) -> list:
        """Watched items as (tmdb_id, rating) pairs. Distinct titles so nothing collapses."""
        return [make_watched(f"Title {tid}", days_ago=1, tmdb_id=tid, user_rating=rating) for tid, rating in pairs]

    def test_a_title_rated_below_the_threshold_stops_seeding(self):
        history = self._rated((1, 2.0), (2, 10.0), (3, 8.0), (4, 6.0), (5, 4.0))

        assert disliked_seed_keys(history, 2.0) == {(1, MediaType.MOVIE)}

    def test_the_threshold_is_inclusive(self):
        """ "2 stars and below" has to include 2 stars, or the setting reads off by one."""
        history = self._rated((1, 4.0), (2, 6.0), (3, 8.0), (4, 10.0), (5, 10.0))

        assert disliked_seed_keys(history, 4.0) == {(1, MediaType.MOVIE)}

    def test_an_unrated_title_is_never_suppressed(self):
        """The 99.7% case. `None` must not compare as low."""
        history = [
            make_watched("Unrated", tmdb_id=9, user_rating=None),
            *self._rated((1, 10.0), (2, 8.0), (3, 6.0), (4, 4.0), (5, 2.0)),
        ]

        assert (9, MediaType.MOVIE) not in disliked_seed_keys(history, 2.0)

    def test_a_zero_rating_is_a_rating(self):
        """0.0 is what Plex writes for the lowest possible rating, and is NOT "unrated"."""
        history = self._rated((1, 0.0), (2, 10.0), (3, 8.0), (4, 6.0), (5, 4.0))

        assert disliked_seed_keys(history, 2.0) == {(1, MediaType.MOVIE)}

    def test_no_threshold_suppresses_nothing(self):
        """The feature switched off — and the state every SHARED row is built in."""
        history = self._rated((1, 0.0), (2, 2.0), (3, 10.0), (4, 8.0), (5, 6.0))

        assert disliked_seed_keys(history, None) == set()

    def test_a_tool_written_rating_is_not_an_opinion(self):
        """A fractional value below the threshold changes nothing — nobody typed it."""
        history = self._rated((1, 1.6), (2, 10.0), (3, 8.0), (4, 6.0), (5, 4.0))

        assert disliked_seed_keys(history, 2.0) == set()

    def test_a_distrusted_account_suppresses_nothing_even_where_the_value_is_whole(self):
        """Both layers. The 2.0 here is indistinguishable from an opinion on its own; the account it
        sits in is what disqualifies it."""
        history = self._rated((1, 2.0), (2, 7.9), (3, 8.8), (4, 6.2), (5, 5.4), (6, 9.1))

        assert disliked_seed_keys(history, 2.0) == set()

    def test_a_title_with_no_tmdb_id_cannot_be_suppressed(self):
        """Seeds are keyed on TMDB id, so a watch without one has nothing to exclude BY. It must be
        skipped rather than contributing a None to the set, which would be silently unmatchable."""
        history = [
            make_watched("No guid", tmdb_id=None, user_rating=2.0),
            *self._rated((1, 10.0), (2, 8.0), (3, 6.0), (4, 4.0)),
        ]

        assert disliked_seed_keys(history, 2.0) == set()


class TestDeriveSeedsHonoursRatings:
    """`derive_seeds` end to end — the suppression has to reach the seed list, not just the helper."""

    def _history(self) -> list:
        return [
            make_watched("Loved It", days_ago=1, tmdb_id=1, user_rating=10.0),
            make_watched("Hated It", days_ago=2, tmdb_id=2, user_rating=2.0),
            make_watched("Unrated", days_ago=3, tmdb_id=3),
            make_watched("Fine", days_ago=4, tmdb_id=4, user_rating=6.0),
            make_watched("Also Fine", days_ago=5, tmdb_id=5, user_rating=8.0),
        ]

    def test_a_disliked_title_does_not_become_a_seed(self):
        seeds = derive_seeds(self._history(), lambda _i: None, disliked=disliked_seed_keys(self._history(), 2.0))

        assert {s.tmdb_id for s in seeds} == {1, 3, 4, 5}

    def test_without_a_threshold_every_watch_still_seeds(self):
        """The default, and what a shared row always gets — one person's rating must never reshape
        a row everyone can see (the same rule `blocked_shared_seeds` exists for)."""
        seeds = derive_seeds(self._history(), lambda _i: None)

        assert {s.tmdb_id for s in seeds} == {1, 2, 3, 4, 5}

    def test_ratings_and_explicit_blocks_both_apply(self):
        """Two independent reasons a title stops seeding; neither may cancel the other."""
        history = self._history()

        seeds = derive_seeds(history, lambda _i: None, blocked={4}, disliked=disliked_seed_keys(history, 2.0))

        assert {s.tmdb_id for s in seeds} == {1, 3, 5}

    def test_a_disliked_title_stays_in_history(self):
        """Suppression is about SEEDING only. The title is still watched, and the already-watched
        rules must keep working on it — otherwise disliking something makes it get recommended."""
        history = self._history()

        derive_seeds(history, lambda _i: None, disliked=disliked_seed_keys(self._history(), 2.0))

        assert any(i.tmdb_id == 2 for i in history), "the caller's history must not be mutated"


class TestTheTmdbNamespaceCollision:
    """TMDB numbers movies and shows separately, so 1399 is both a film and Game of Thrones.

    Found by review: the exclusion was originally a bare `set[int]` unioned into `blocked`, so one
    person's 1-star on a MOVIE silently deleted the identically-numbered SHOW from their seeds — a
    title they had never rated and could not have. Nobody types these ids, so nobody would ever have
    spotted the row and questioned it. Same key-space mismatch class this repo has shipped before.
    """

    def _history(self) -> list:
        return [
            make_watched("Hated Film", days_ago=1, tmdb_id=1399, user_rating=2.0),
            make_watched("Innocent Show", days_ago=2, tmdb_id=1399, media_type=MediaType.SHOW),
            # Filler so the account-level guard has a judgeable sample and does not abstain.
            *[make_watched(f"Fine {n}", days_ago=3 + n, tmdb_id=500 + n, user_rating=10.0) for n in range(5)],
        ]

    def test_the_key_carries_the_media_type(self):
        assert disliked_seed_keys(self._history(), 2.0) == {(1399, MediaType.MOVIE)}

    def test_a_disliked_movie_does_not_suppress_the_show_sharing_its_id(self):
        history = self._history()

        seeds = derive_seeds(history, lambda _i: None, disliked=disliked_seed_keys(history, 2.0))

        kept = {(s.tmdb_id, s.media_type) for s in seeds}
        assert (1399, MediaType.SHOW) in kept, "the show was never rated — it must still seed"
        assert (1399, MediaType.MOVIE) not in kept, "the film they rated 1 star must not seed"
