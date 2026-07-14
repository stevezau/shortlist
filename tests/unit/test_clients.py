"""Boundary clients: plex.tv XML/throttle, TMDB pooling+cache, Tautulli, PMS helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import respx

import shortlist.engine.clients.plextv as plextv_mod
from shortlist.engine.clients.plex_pms import MIN_PMS_VERSION, PlexClient, parse_pms_version
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.tautulli import TautulliClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import MediaType, OwnedRow, UserType
from tests.conftest import fake_media_item

FIXTURES = Path(__file__).parent.parent / "fixtures"
USERS_XML = (FIXTURES / "plextv_users.xml").read_text()


class TestPmsVersion:
    def test_parse_strips_build_hash(self):
        assert parse_pms_version("1.43.3.10793-cd55560bb") == (1, 43, 3, 10793)

    def test_min_version_comparison(self):
        assert parse_pms_version("1.43.3.10793-x") >= MIN_PMS_VERSION
        assert parse_pms_version("1.42.1.9999-x") < MIN_PMS_VERSION


class TestPlexTvClient:
    def _client(self) -> PlexTvClient:
        return PlexTvClient("tok", "machine1", min_write_interval=0)

    @respx.mock
    def test_list_users_parses_filters_and_user_types_from_recorded_fixture(self):
        respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=USERS_XML))
        users = self._client().list_users()
        assert users[0].id == 555000100
        assert users[0].user_type is UserType.SHARED
        assert users[0].filters["filterMovies"] == "label!=Rowarr_mike"
        assert users[1].user_type is UserType.MANAGED
        assert users[1].home is True

    @respx.mock
    def test_update_filters_sends_only_given_fields_with_token_header(self):
        route = respx.put("https://plex.tv/api/users/100").mock(return_value=httpx.Response(200))
        self._client().update_user_filters(100, {"filterMovies": "label!=Rowarr_a"})
        request = route.calls.last.request
        assert request.url.params["filterMovies"] == "label!=Rowarr_a"
        assert "filterTelevision" not in request.url.params
        assert request.headers["X-Plex-Token"] == "tok"

    @respx.mock
    def test_429_backs_off_then_succeeds(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(plextv_mod.time, "sleep", sleeps.append)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(429), httpx.Response(200)]
        self._client().update_user_filters(100, {"filterMovies": "x=y"})
        assert 5.0 in sleeps

    @respx.mock
    def test_non_429_error_raises_without_retry(self):
        respx.put("https://plex.tv/api/users/100").mock(return_value=httpx.Response(403))
        with pytest.raises(RuntimeError, match="403"):
            self._client().update_user_filters(100, {"filterMovies": "x=y"})

    @respx.mock
    def test_canary_token_exchange_flow(self):
        respx.get("https://plex.tv/api/v2/home/users").mock(
            return_value=httpx.Response(
                200,
                json={
                    "users": [
                        {"id": 555000100, "uuid": "uu-1", "title": "HomeUser", "protected": False},
                    ]
                },
            )
        )
        respx.post("https://plex.tv/api/v2/home/users/uu-1/switch").mock(
            return_value=httpx.Response(200, json={"authToken": "switch-tok"})
        )
        resources = respx.get("https://plex.tv/api/v2/resources", params={"includeHttps": "1"}).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"clientIdentifier": "other", "accessToken": "wrong"},
                    {"clientIdentifier": "machine1", "accessToken": "server-tok"},
                ],
            )
        )
        token = self._client().canary_server_token(555000100)
        assert token == "server-tok"
        # The resources exchange must run AS the switched user (Phase 0 finding: owner token 401s).
        assert resources.calls.last.request.headers["X-Plex-Token"] == "switch-tok"

    @respx.mock
    def test_pin_protected_canary_refused(self):
        respx.get("https://plex.tv/api/v2/home/users").mock(
            return_value=httpx.Response(
                200,
                json={
                    "users": [
                        {"id": 1, "uuid": "uu", "title": "Kid", "protected": True},
                    ]
                },
            )
        )
        with pytest.raises(PermissionError, match="PIN-protected"):
            self._client().canary_server_token(1)


class TestTmdbClient:
    @respx.mock
    def test_suggestions_pools_recommendations_and_similar(self):
        respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 10}, {"id": 20}]})
        )
        respx.get("https://api.themoviedb.org/3/movie/1/similar").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 20}, {"id": 30}]})
        )
        pooled = TmdbClient("k").suggestions(1, MediaType.MOVIE)
        assert sorted(x["id"] for x in pooled) == [10, 20, 30]

    @respx.mock
    def test_discover_queries_genres_and_returns_results(self):
        route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 7}, {"id": 8}]})
        )
        results = TmdbClient("k").discover(MediaType.MOVIE, [18, 28], min_votes=200)
        assert [r["id"] for r in results] == [7, 8]
        # The genre/sort/vote filters must reach TMDB (they're the whole point of discover).
        params = route.calls.last.request.url.params
        assert params.get("with_genres") == "18,28"
        assert params.get("sort_by") == "popularity.desc"
        assert params.get("vote_count.gte") == "200"

    @respx.mock
    def test_discover_with_no_genres_makes_no_call(self):
        # No genres -> no query at all (respx would raise on any unmocked request).
        assert TmdbClient("k").discover(MediaType.MOVIE, []) == []

    @respx.mock
    def test_tvdb_id_reads_external_ids_for_a_show(self):
        respx.get("https://api.themoviedb.org/3/tv/95396/external_ids").mock(
            return_value=httpx.Response(200, json={"tvdb_id": 371980, "imdb_id": "tt11280740"})
        )
        assert TmdbClient("k").tvdb_id(95396, MediaType.SHOW) == 371980

    @respx.mock
    def test_tvdb_id_is_none_when_tmdb_has_no_mapping(self):
        # TMDB returns the key present but null for titles with no TheTVDB entry.
        respx.get("https://api.themoviedb.org/3/tv/95396/external_ids").mock(
            return_value=httpx.Response(200, json={"tvdb_id": None})
        )
        assert TmdbClient("k").tvdb_id(95396, MediaType.SHOW) is None

    @respx.mock
    def test_cache_prevents_second_fetch(self):
        route = respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.get("https://api.themoviedb.org/3/movie/1/similar").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        class DictCache:
            def __init__(self):
                self.store = {}

            def get(self, key):
                return self.store.get(key)

            def set(self, key, value, ttl_s):
                self.store[key] = value

        client = TmdbClient("k", cache=DictCache())
        client.suggestions(1, MediaType.MOVIE)
        client.suggestions(1, MediaType.MOVIE)
        assert route.call_count == 1

    @respx.mock
    def test_404_returns_empty_not_error(self):
        respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(return_value=httpx.Response(404))
        respx.get("https://api.themoviedb.org/3/movie/1/similar").mock(return_value=httpx.Response(404))
        assert TmdbClient("k").suggestions(1, MediaType.MOVIE) == []

    @respx.mock
    def test_api_key_never_appears_in_error_messages(self):
        respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(return_value=httpx.Response(500))
        with pytest.raises(RuntimeError) as excinfo:
            TmdbClient("SUPERSECRETKEY").suggestions(1, MediaType.MOVIE)
        assert "SUPERSECRETKEY" not in str(excinfo.value)
        assert "500" in str(excinfo.value)


class TestOmdbClient:
    @respx.mock
    def test_rating_parses_score_and_comma_separated_votes(self):
        respx.get("https://www.omdbapi.com/").mock(
            return_value=httpx.Response(200, json={"Response": "True", "imdbRating": "8.3", "imdbVotes": "2,754,113"})
        )
        from shortlist.engine.clients.omdb import OmdbClient

        assert OmdbClient("k").rating("tt0111161") == (8.3, 2754113)

    @respx.mock
    def test_rating_is_none_when_omdb_has_no_data(self):
        respx.get("https://www.omdbapi.com/").mock(
            return_value=httpx.Response(200, json={"Response": "True", "imdbRating": "N/A", "imdbVotes": "N/A"})
        )
        from shortlist.engine.clients.omdb import OmdbClient

        assert OmdbClient("k").rating("tt0111161") is None

    @respx.mock
    def test_rating_is_none_on_error_response(self):
        respx.get("https://www.omdbapi.com/").mock(
            return_value=httpx.Response(200, json={"Response": "False", "Error": "Incorrect IMDb ID."})
        )
        from shortlist.engine.clients.omdb import OmdbClient

        assert OmdbClient("k").rating("ttbad") is None


class TestTautulliClient:
    @respx.mock
    def test_get_history_success(self):
        route = respx.get("http://taut.test/api/v2").mock(
            return_value=httpx.Response(
                200, json={"response": {"result": "success", "data": {"data": [{"title": "Heat"}]}}}
            )
        )
        rows = TautulliClient("http://taut.test", "key").get_history(100)
        assert rows == [{"title": "Heat"}]
        params = route.calls.last.request.url.params
        assert params["cmd"] == "get_history"
        assert params["user_id"] == "100"

    @respx.mock
    def test_api_failure_raises(self):
        respx.get("http://taut.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "error", "message": "bad key"}})
        )
        with pytest.raises(RuntimeError, match="bad key"):
            TautulliClient("http://taut.test", "key").get_history(100)

    @respx.mock
    def test_api_key_never_appears_in_error_messages(self):
        respx.get("http://taut.test/api/v2").mock(return_value=httpx.Response(502))
        with pytest.raises(RuntimeError) as excinfo:
            TautulliClient("http://taut.test", "SUPERSECRETKEY").get_history(100)
        assert "SUPERSECRETKEY" not in str(excinfo.value)
        assert "502" in str(excinfo.value)


class TestPlexClient:
    def test_build_library_index_maps_tmdb_guids(self, mock_plex: PlexClient):
        section = MagicMock()
        section.title = "Movies"
        section.totalSize = 3
        section.all.return_value = [
            fake_media_item(1, "Has Guid", tmdb_id=42),
            fake_media_item(2, "No Guid"),
            SimpleNamespace(ratingKey=3, title="Other Guid", guids=[SimpleNamespace(id="imdb://tt1")]),
        ]
        assert mock_plex.build_library_index(section) == {42: 1}

    def test_stored_label_returns_existing_title_cased_form_without_write(self, mock_plex: PlexClient):
        collection = MagicMock()
        collection.labels = [SimpleNamespace(tag="Rowarr_sarah")]
        assert mock_plex.stored_label(collection, "rowarr_sarah") == "Rowarr_sarah"
        collection.addLabel.assert_not_called()

    def test_stored_label_adds_and_reads_back_when_missing(self, mock_plex: PlexClient):
        collection = MagicMock()
        collection.labels = []

        def add(label):
            collection.labels = [SimpleNamespace(tag="Rowarr_sarah")]  # Plex title-cases on write

        collection.addLabel.side_effect = add
        assert mock_plex.stored_label(collection, "rowarr_sarah") == "Rowarr_sarah"
        collection.reload.assert_called()

    def test_delete_refuses_collections_without_rowarr_label(self, mock_plex: PlexClient):
        foreign = MagicMock()
        foreign.title = "Kometa Collection"
        foreign.labels = [SimpleNamespace(tag="Overlay")]
        with pytest.raises(PermissionError, match="not ours"):
            mock_plex.delete_owned_collection(foreign, "rowarr")
        foreign.delete.assert_not_called()

    def test_delete_demotes_then_deletes_owned(self, mock_plex: PlexClient):
        owned = MagicMock()
        owned.labels = [SimpleNamespace(tag="Rowarr_sarah")]
        mock_plex.delete_owned_collection(owned, "rowarr")
        vis = owned.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": False, "home": False, "shared": False}
        owned.delete.assert_called_once()

    def test_promote_hides_from_library_and_promotes_shared(self, mock_plex: PlexClient):
        collection = MagicMock()
        mock_plex.promote(collection)
        collection.modeUpdate.assert_called_once_with(mode="hide")
        vis = collection.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": True, "home": True, "shared": True}

    def test_owned_collections_maps_slug_to_stored_label_and_id(self, mock_plex: PlexClient):
        ours = MagicMock(ratingKey=571285)
        ours.labels = [SimpleNamespace(tag="Rowarr_sarah")]
        kometa = MagicMock(ratingKey=9)
        kometa.labels = [SimpleNamespace(tag="Overlay")]
        section = MagicMock()
        section.type = "movie"
        section.collections.return_value = [ours, kometa]
        mock_plex._server.library.sections.return_value = [section]
        assert mock_plex.owned_collections("rowarr") == {"sarah": OwnedRow("Rowarr_sarah", [571285])}

    def test_owned_collections_collects_a_users_row_from_every_library(self, mock_plex: PlexClient):
        """One user, one collection per library. Collapsing them to a single id once hid a real
        leak: T2 compared only the last collection it saw and passed while another was visible."""
        movie_row = MagicMock(ratingKey=571285)
        movie_row.labels = [SimpleNamespace(tag="Rowarr_sarah")]
        show_row = MagicMock(ratingKey=571290)
        show_row.labels = [SimpleNamespace(tag="Rowarr_sarah")]
        movies, shows = MagicMock(), MagicMock()
        movies.type, shows.type = "movie", "show"
        movies.collections.return_value = [movie_row]
        shows.collections.return_value = [show_row]
        mock_plex._server.library.sections.return_value = [movies, shows]

        assert mock_plex.owned_collections("rowarr") == {"sarah": OwnedRow("Rowarr_sarah", [571285, 571290])}

    def test_server_name_returns_friendly_name(self, mock_plex: PlexClient):
        mock_plex._server.friendlyName = "SFLIX"
        assert mock_plex.server_name == "SFLIX"

    def test_top_rated_returns_tmdb_pairs_skipping_items_without_guids(self, mock_plex: PlexClient):
        """The cold-start guid parse lives here now; items with no tmdb guid are skipped, and the
        search over-fetches (2x) so a sparse library still fills the request."""
        section = MagicMock()
        section.search.return_value = [
            fake_media_item(1, "A", tmdb_id=50),
            fake_media_item(2, "No Guid"),
            fake_media_item(3, "B", tmdb_id=60),
        ]
        pairs = mock_plex.top_rated(section, 2)
        assert [(tmdb_id, item.title) for tmdb_id, item in pairs] == [(50, "A"), (60, "B")]
        assert section.search.call_args.kwargs == {"sort": "audienceRating:desc", "limit": 4}

    def test_set_items_replaces_membership_and_reorders_via_move_chain(self, mock_plex: PlexClient):
        """set_items must push the exact adds/removes AND lay out custom order with a moveItem
        after= chain — feeding the wrong deltas or order is invisible to a call-count assertion."""

        def item(rating_key: int) -> MagicMock:
            it = MagicMock()
            it.ratingKey = rating_key
            return it

        existing = [item(1), item(2), item(3)]  # 2 will be removed, 4 added
        wanted = [item(4), item(1), item(3)]  # desired order 4, 1, 3
        final = [item(4), item(1), item(3)]  # what the PMS returns after the writes + reload
        collection = MagicMock()
        collection.items.side_effect = [existing, existing, final]

        mock_plex.set_items(collection, wanted)

        assert [i.ratingKey for i in collection.addItems.call_args.args[0]] == [4]
        assert [i.ratingKey for i in collection.removeItems.call_args.args[0]] == [2]
        collection.sortUpdate.assert_called_once_with(sort="custom")
        moves = collection.moveItem.call_args_list
        assert [c.args[0].ratingKey for c in moves] == [4, 1, 3]
        assert moves[0].kwargs["after"] is None
        assert moves[1].kwargs["after"].ratingKey == 4
        assert moves[2].kwargs["after"].ratingKey == 1

    def test_sections_by_type_maps_each_media_type_to_its_library(self, mock_plex: PlexClient):
        movies, shows = MagicMock(), MagicMock()
        movies.type, movies.key = "movie", "1"
        shows.type, shows.key = "show", "2"
        mock_plex._server.library.sections.return_value = [movies, shows]

        assert mock_plex.sections_by_type() == {MediaType.MOVIE: movies, MediaType.SHOW: shows}


class TestSectionsByType:
    def test_the_lowest_keyed_library_of_each_type_wins(self, mock_plex: PlexClient):
        """PMS list order must not decide where rows live: a reordering would silently move
        every user's row into a different library."""
        movies_4k, movies, shows = MagicMock(), MagicMock(), MagicMock()
        movies_4k.type, movies_4k.key = "movie", "3"
        movies.type, movies.key = "movie", "1"
        shows.type, shows.key = "show", "2"
        mock_plex._server.library.sections.return_value = [movies_4k, movies, shows]

        assert mock_plex.sections_by_type() == {MediaType.MOVIE: movies, MediaType.SHOW: shows}
