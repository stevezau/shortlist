"""Watch-history: the share-token source's token matrix + orchestration, plus seed derivation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from shortlist.engine.history import ShareTokenWatchSource, derive_seeds, distinct_recent
from shortlist.engine.models import MediaType, UserType
from tests.conftest import make_profile, make_watched


def _section(key: str, section_type: str) -> SimpleNamespace:
    """A stand-in for a plexapi library section — the source only reads `.key` and `.type`."""
    return SimpleNamespace(key=key, type=section_type)


class TestShareTokenWatchSource:
    """The one watch source: reads each user's COMPLETE watched set from the PMS as that user.

    The value under test is which token it reads AS (the user_type matrix) and that it aggregates
    every library fail-soft — the XML→WatchedItem parsing lives in test_clients (PMS `watched_titles`).
    """

    def _source(self, mock_plex, mock_plextv, *, owner_token: str = "OWNER-TOK") -> ShareTokenWatchSource:
        # Two libraries, one of each type, so fetch() must iterate both and pick the media type per one.
        mock_plex._server.library.sections.return_value = [_section("1", "movie"), _section("2", "show")]
        mock_plex.watched_titles = MagicMock(return_value=[])
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

    def test_selects_the_media_type_per_library_and_aggregates_across_them(self, mock_plex, mock_plextv):
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {100: "SARAH-TOK"}
        mock_plex.watched_titles.side_effect = lambda key, mt, tok: (
            [make_watched("Heat", media_type=MediaType.MOVIE)]
            if mt is MediaType.MOVIE
            else [make_watched("Suits", media_type=MediaType.SHOW)]
        )

        items = source.fetch(make_profile(account_id=100), min_completion=0.7)

        # Movie library read as type=movie, show library as type=show, results merged.
        by_key = {call.args[0]: call.args[1] for call in mock_plex.watched_titles.call_args_list}
        assert by_key == {"1": MediaType.MOVIE, "2": MediaType.SHOW}
        assert {(i.title, i.media_type) for i in items} == {
            ("Heat", MediaType.MOVIE),
            ("Suits", MediaType.SHOW),
        }

    def test_one_unreadable_library_degrades_to_empty_without_failing_the_others(self, mock_plex, mock_plextv):
        """A single library erroring must not lose the user's whole history — the other library's
        titles still come back (fail-soft per library, matching the old sources' stance)."""
        source = self._source(mock_plex, mock_plextv)
        mock_plextv.shared_server_tokens.return_value = {100: "SARAH-TOK"}

        def read(key, media_type, token):
            if media_type is MediaType.MOVIE:
                raise RuntimeError("section unreadable")
            return [make_watched("Suits", media_type=MediaType.SHOW)]

        mock_plex.watched_titles.side_effect = read
        items = source.fetch(make_profile(account_id=100), min_completion=0.7)

        assert [i.title for i in items] == ["Suits"]


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
    def test_frequency_and_recency_weighting(self):
        history = [
            make_watched("Old Favorite", days_ago=80),
            make_watched("Binged Show", days_ago=2, media_type=MediaType.SHOW),
            make_watched("Binged Show", days_ago=3, media_type=MediaType.SHOW),
            make_watched("Binged Show", days_ago=4, media_type=MediaType.SHOW),
        ]
        ids = {("Binged Show", MediaType.SHOW): 1, ("Old Favorite", MediaType.MOVIE): 2}
        seeds = derive_seeds(history, lambda w: ids.get((w.title, w.media_type)))
        assert seeds[0].title == "Binged Show"
        assert seeds[0].weight > seeds[1].weight

    def test_a_shows_episode_count_drives_its_weight_not_its_row_count(self):
        # The share-token source returns ONE row per show carrying watch_count = episodes watched, so
        # a 50-episode binge must weigh like 50 plays even though it's a single WatchedItem — the seed
        # weight reads watch_count, not len(rows) (the old per-play sources emitted one row per play).
        history = [
            make_watched("Binge", days_ago=1, media_type=MediaType.SHOW, watch_count=50),
            make_watched("One Movie", days_ago=1, media_type=MediaType.MOVIE, watch_count=1),
        ]
        ids = {("Binge", MediaType.SHOW): 1, ("One Movie", MediaType.MOVIE): 2}
        seeds = derive_seeds(history, lambda w: ids[(w.title, w.media_type)])
        binge = next(s for s in seeds if s.title == "Binge")
        assert binge.watch_count == 50
        assert binge.weight > next(s for s in seeds if s.title == "One Movie").weight

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
