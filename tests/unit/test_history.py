"""History-source matrix: tautulli / plex, plus seed derivation."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.history import (
    FallbackHistorySource,
    PlexHistorySource,
    TautulliSource,
    derive_seeds,
    distinct_recent,
)
from shortlist.engine.models import MediaType, UserType
from tests.conftest import make_profile, make_watched


def tautulli_row(**kw) -> dict:
    base = {
        "media_type": "movie",
        "title": "Heat",
        "grandparent_title": None,
        "percent_complete": 100,
        "date": 1752000000,
        "year": 1995,
        "rating_key": 42,
        "grandparent_rating_key": None,
        "parent_media_index": None,
        "media_index": None,
    }
    return {**base, **kw}


class TestTautulliSource:
    def test_maps_movie_rows(self, mock_tautulli):
        mock_tautulli.get_history.return_value = [tautulli_row()]
        items = TautulliSource(mock_tautulli).fetch(make_profile(), min_completion=0.7)
        assert len(items) == 1
        assert items[0].title == "Heat"
        assert items[0].media_type is MediaType.MOVIE
        assert items[0].completion == 1.0
        # A movie carries no episode detail.
        assert (items[0].season, items[0].episode, items[0].episode_title) == (None, None, None)
        mock_tautulli.get_history.assert_called_once_with(100, since_ts=None)

    def test_episode_rows_collapse_to_show_title(self, mock_tautulli):
        mock_tautulli.get_history.return_value = [
            tautulli_row(
                media_type="episode",
                title="Pilot",
                grandparent_title="Suits",
                grandparent_rating_key=7,
                parent_media_index="2",
                media_index="5",
            ),
        ]
        items = TautulliSource(mock_tautulli).fetch(make_profile(), min_completion=0.7)
        assert items[0].title == "Suits"
        assert items[0].media_type is MediaType.SHOW
        assert items[0].rating_key == 7
        # The episode detail is carried for display (show title stays the seed).
        assert (items[0].season, items[0].episode) == (2, 5)
        assert items[0].episode_title == "Pilot"

    def test_incomplete_watches_filtered_by_threshold(self, mock_tautulli):
        mock_tautulli.get_history.return_value = [tautulli_row(percent_complete=20)]
        assert TautulliSource(mock_tautulli).fetch(make_profile(), min_completion=0.7) == []


class TestPlexHistorySource:
    def test_maps_entries_from_plex_history_api(self, mock_plex):
        entry = SimpleNamespace(
            type="episode",
            grandparentTitle="Suits",
            title="Pilot",
            parentIndex="2",
            index="5",
            viewedAt=datetime(2026, 7, 1),
            grandparentRatingKey="7",
            ratingKey="99",
        )
        mock_plex._server.history.return_value = [entry]
        items = PlexHistorySource(mock_plex).fetch(make_profile(account_id=555000100), min_completion=0.7)
        assert items[0].title == "Suits"
        assert items[0].rating_key == 7
        # PMS parentIndex/index → season/episode, entry.title → episode name.
        assert (items[0].season, items[0].episode) == (2, 5)
        assert items[0].episode_title == "Pilot"
        call = mock_plex._server.history.call_args
        assert call.kwargs["accountID"] == 555000100

    def test_a_shared_user_is_asked_for_by_their_plex_tv_id_without_reading_pms_accounts(self, mock_plex):
        """PMS lists shared users under their plex.tv id, so they cost no extra `/accounts` read."""
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex).fetch(make_profile(account_id=555000100), min_completion=0.7)

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 555000100
        mock_plex._server.systemAccounts.assert_not_called()

    def test_the_owners_history_is_asked_for_by_the_id_pms_files_it_under(self, mock_plex):
        """The owner is not in PMS's account table under their plex.tv id — asking for that id
        returns nothing, and their 'personalized' row would quietly be built from an empty history."""
        mock_plex._server.systemAccounts.return_value = [
            SimpleNamespace(id=0, name=""),
            SimpleNamespace(id=1, name="steve"),  # the owner, as a LOCAL PMS account
            SimpleNamespace(id=555000100, name="sarah"),
        ]
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex).fetch(
            make_profile(username="steve", user_type=UserType.OWNER, account_id=555000001),
            min_completion=0.7,
        )

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 1

    def test_an_owner_pms_already_knows_by_plex_tv_id_is_left_alone(self, mock_plex):
        """An exact id match wins over the name, so a server that does file the owner under their
        plex.tv id keeps working — and two accounts sharing a display name can't cross wires."""
        mock_plex._server.systemAccounts.return_value = [
            SimpleNamespace(id=1, name="steve"),
            SimpleNamespace(id=555000001, name="steve"),
        ]
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex).fetch(
            make_profile(username="steve", user_type=UserType.OWNER, account_id=555000001),
            min_completion=0.7,
        )

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 555000001

    def test_an_unreadable_account_list_raises_rather_than_reporting_an_empty_history(self, mock_plex):
        """Falling back to the plex.tv id here would return ZERO rows — indistinguishable from "this
        person has never watched anything". The incremental sync banks that as a successful empty
        pull and advances the watermark, so one hiccup on the owner's first run would leave their
        history permanently un-backfilled. Raising keeps the watermark (the sync fails soft)."""
        mock_plex._server.systemAccounts.side_effect = RuntimeError("PMS said no")

        with pytest.raises(RuntimeError, match="could not read PMS accounts"):
            PlexHistorySource(mock_plex).fetch(
                make_profile(username="steve", user_type=UserType.OWNER, account_id=555000001),
                min_completion=0.7,
            )

        mock_plex._server.history.assert_not_called()

    def test_a_single_name_match_on_a_known_roster_account_is_refused(self, mock_plex):
        """The residual attribution risk: the owner renames themselves, PMS's local row keeps the old
        name, and ANOTHER account now carries the new one — a single, confident, wrong match. Every
        non-owner is listed under their plex.tv id, so a candidate that IS a known account can't be
        the owner's local row."""
        mock_plex._server.systemAccounts.return_value = [
            SimpleNamespace(id=1, name="old-name"),
            SimpleNamespace(id=555000100, name="steve"),  # sarah, who just renamed herself
        ]
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex, roster_account_ids=frozenset({555000100, 555000200})).fetch(
            make_profile(username="steve", user_type=UserType.OWNER, account_id=555000001),
            min_completion=0.7,
        )

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 555000001

    def test_a_managed_user_is_asked_for_by_their_plex_tv_id(self, mock_plex):
        """The third cell of the user_type matrix. PMS lists Home/managed profiles under their own
        plex.tv id (recorded fixture `pms_accounts.xml.txt`), so they resolve like a shared user —
        and must NOT go near the name match, since a Home profile's local name is owner-chosen."""
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex).fetch(
            make_profile(username="kid", user_type=UserType.MANAGED, account_id=555000200),
            min_completion=0.7,
        )

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 555000200
        mock_plex._server.systemAccounts.assert_not_called()

    def test_two_accounts_sharing_the_owners_name_are_refused_rather_than_guessed(self, mock_plex):
        """A Home profile can be given ANY local name, including the owner's. Two candidates and no
        id match means we cannot tell which is the person — and picking the first would build the
        owner's private row out of a managed user's viewing."""
        mock_plex._server.systemAccounts.return_value = [
            SimpleNamespace(id=1, name="steve"),
            SimpleNamespace(id=555000200, name="Steve"),  # another account that carries the same name
        ]
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex).fetch(
            make_profile(username="steve", user_type=UserType.OWNER, account_id=555000001),
            min_completion=0.7,
        )

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 555000001

    def test_the_nameless_local_bucket_never_wins_the_match(self, mock_plex):
        """PMS id 0 is the 'Local' account: plays from clients that were never signed in, belonging
        to nobody. An owner with no username must not be handed that pile of everyone's viewing."""
        mock_plex._server.systemAccounts.return_value = [
            SimpleNamespace(id=0, name=""),
            SimpleNamespace(id=1, name="steve"),
        ]
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex).fetch(
            make_profile(username="", user_type=UserType.OWNER, account_id=555000001),
            min_completion=0.7,
        )

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 555000001

    def test_an_owner_pms_has_never_heard_of_is_never_mapped_onto_another_account(self, mock_plex):
        """No id match and no name match must mean 'empty history', never 'the nearest account'."""
        mock_plex._server.systemAccounts.return_value = [
            SimpleNamespace(id=1, name="someone-else"),
            SimpleNamespace(id=555000100, name="sarah"),
        ]
        mock_plex._server.history.return_value = []

        PlexHistorySource(mock_plex).fetch(
            make_profile(username="steve", user_type=UserType.OWNER, account_id=555000001),
            min_completion=0.7,
        )

        assert mock_plex._server.history.call_args.kwargs["accountID"] == 555000001


class TestFallbackHistorySource:
    """Phase 1 pilot lesson: Tautulli had 1 row for a user with 11k rows of PMS history."""

    def _source(self, primary_items, fallback_items, min_items=10):
        primary, fallback = MagicMock(), MagicMock()
        primary.fetch.return_value = primary_items
        fallback.fetch.return_value = fallback_items
        return FallbackHistorySource(primary, fallback, min_items=min_items), primary, fallback

    def test_rich_primary_wins_without_touching_fallback(self):
        items = [make_watched(f"m{i}") for i in range(10)]
        source, _, fallback = self._source(items, [])
        assert source.fetch(make_profile(), min_completion=0.7) == items
        fallback.fetch.assert_not_called()

    def test_thin_primary_falls_back_to_richer_source(self):
        thin = [make_watched("only one")]
        rich = [make_watched(f"m{i}") for i in range(20)]
        source, _, _ = self._source(thin, rich)
        assert source.fetch(make_profile(), min_completion=0.7) == rich

    def test_thin_primary_kept_when_fallback_is_no_better(self):
        thin = [make_watched("only one")]
        source, _, _ = self._source(thin, [])
        assert source.fetch(make_profile(), min_completion=0.7) == thin

    def test_primary_error_uses_fallback(self):
        source, primary, _ = self._source([], [make_watched("m")])
        primary.fetch.side_effect = RuntimeError("tautulli down")
        assert len(source.fetch(make_profile(), min_completion=0.7)) == 1


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
