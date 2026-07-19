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
from shortlist.engine.clients.trakt import TraktClient, TraktError
from shortlist.engine.models import MediaType, OwnedRow, UserType
from tests.conftest import fake_media_item

FIXTURES = Path(__file__).parent.parent / "fixtures"
USERS_XML = (FIXTURES / "plextv_users.xml").read_text()


class _MemoryCache:
    """Minimal in-memory Cache (get/set) for exercising the client caches without a DB or file."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


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
        assert users[0].filters["filterMovies"] == "label!=Shortlist_mike"
        assert users[1].user_type is UserType.MANAGED
        assert users[1].home is True

    @respx.mock
    def test_update_filters_sends_only_given_fields_with_token_header(self):
        route = respx.put("https://plex.tv/api/users/100").mock(return_value=httpx.Response(200))
        self._client().update_user_filters(100, {"filterMovies": "label!=Shortlist_a"})
        request = route.calls.last.request
        assert request.url.params["filterMovies"] == "label!=Shortlist_a"
        assert "filterTelevision" not in request.url.params
        assert request.headers["X-Plex-Token"] == "tok"

    @respx.mock
    def test_429_slows_the_adaptive_pace_then_succeeds(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(plextv_mod.time, "sleep", sleeps.append)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(429), httpx.Response(200)]
        client = self._client()
        assert client._pace == 0.0  # starts fast — no fixed 1/s
        client.update_user_filters(100, {"filterMovies": "x=y"})
        assert len(route.calls) == 2  # the 429 was retried to success
        # The 429 widened the pace to ~1s and the retry waited that long; the clean write then eased
        # it partway back — so it ends above the floor but below the 1s it jumped to.
        assert max(sleeps, default=0) >= 0.9
        assert 0.0 < client._pace < 1.0

    @respx.mock
    def test_relentless_429_backs_off_then_gives_up_without_looping_forever(self, monkeypatch):
        monkeypatch.setattr(plextv_mod.time, "sleep", lambda _s: None)  # don't actually wait
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(429)] * 8  # plex.tv never relents
        with pytest.raises(RuntimeError, match="throttling"):
            self._client().update_user_filters(100, {"filterMovies": "x=y"})
        assert len(route.calls) == 6  # bounded retries — it gives up, never loops forever

    @respx.mock
    def test_connect_error_resends_the_same_merged_filter(self, monkeypatch):
        # A connect error proves the PUT never landed, so re-sending the SAME pre-merged filter is
        # safe (rule 3: no rebuild) and expected (rule 6: the sync can't strand a user's restriction).
        sleeps = []
        monkeypatch.setattr(plextv_mod.time, "sleep", sleeps.append)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.ConnectError("never landed"), httpx.Response(200)]
        self._client().update_user_filters(100, {"filterMovies": "label!=Shortlist_a"})
        assert len(route.calls) == 2, "a connect error is retried"
        assert route.calls.last.request.url.params["filterMovies"] == "label!=Shortlist_a", "byte-identical resend"
        assert sleeps, "backoff ran before the retry"

    @respx.mock
    def test_read_timeout_on_filter_write_is_not_retried(self):
        # A read timeout MAY mean the write applied server-side; retrying could double-apply a
        # restriction, so it must propagate on the first attempt (the double-apply guard).
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = httpx.ReadTimeout("maybe applied")
        with pytest.raises(httpx.ReadTimeout):
            self._client().update_user_filters(100, {"filterMovies": "x=y"})
        assert len(route.calls) == 1, "no retry on a read timeout for a write"

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
    def test_search_returns_top_match_with_the_query_and_year(self):
        route = respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 42, "title": "Dune"}, {"id": 43}]})
        )
        found = TmdbClient("k").search("Dune", MediaType.MOVIE, year=2021)
        assert found["id"] == 42  # the top result, used to resolve an LLM-proposed title
        params = route.calls.last.request.url.params
        assert params.get("query") == "Dune"
        assert params.get("year") == "2021"  # movies gate on `year`

    @respx.mock
    def test_search_shows_gate_on_first_air_date_year(self):
        route = respx.get("https://api.themoviedb.org/3/search/tv").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 95396, "name": "Severance"}]})
        )
        found = TmdbClient("k").search("Severance", MediaType.SHOW, year=2022)
        assert found["id"] == 95396
        params = route.calls.last.request.url.params
        assert params.get("first_air_date_year") == "2022"  # shows use first_air_date_year, not year

    @respx.mock
    def test_search_returns_none_when_nothing_matches(self):
        respx.get("https://api.themoviedb.org/3/search/tv").mock(return_value=httpx.Response(200, json={"results": []}))
        assert TmdbClient("k").search("Nonexistent Show", MediaType.SHOW) is None

    def test_search_blank_title_makes_no_call(self):
        # An empty proposed title never hits the network (respx.mock not needed — no request).
        assert TmdbClient("k").search("   ", MediaType.MOVIE) is None

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

        client = TmdbClient("k", cache=_MemoryCache())
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


class TestRemovedOmdbClient:
    """OMDb was replaced by MDBList (one call returns IMDb/Trakt/RT/Metacritic, cached). See
    tests/unit/test_mdblist.py."""


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
        index, _episodes = mock_plex.build_library_index(section)
        assert index == {42: 1}

    def test_stored_label_returns_existing_title_cased_form_without_write(self, mock_plex: PlexClient):
        collection = MagicMock()
        collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        assert mock_plex.stored_label(collection, "shortlist_sarah") == "Shortlist_sarah"
        collection.addLabel.assert_not_called()

    def test_stored_label_adds_and_reads_back_when_missing(self, mock_plex: PlexClient):
        collection = MagicMock()
        collection.labels = []

        def add(label):
            collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]  # Plex title-cases on write

        collection.addLabel.side_effect = add
        assert mock_plex.stored_label(collection, "shortlist_sarah") == "Shortlist_sarah"
        collection.reload.assert_called()

    def test_delete_refuses_collections_without_shortlist_label(self, mock_plex: PlexClient):
        foreign = MagicMock()
        foreign.title = "Kometa Collection"
        foreign.labels = [SimpleNamespace(tag="Overlay")]
        with pytest.raises(PermissionError, match="not ours"):
            mock_plex.delete_owned_collection(foreign, "shortlist")
        foreign.delete.assert_not_called()

    def test_delete_accepts_an_unlabelled_orphan_that_carries_our_marker(self, mock_plex: PlexClient):
        # An orphan whose label write never landed still carries the invisible 64-char marker, which
        # proves it's ours even with no label — the sweep must be able to delete it (else it leaks).
        from shortlist.engine.delivery import row_marker

        orphan = MagicMock()
        orphan.title = "✨ Movies Picked for You" + row_marker(202)
        orphan.labels = []
        mock_plex.delete_owned_collection(orphan, "shortlist")
        # Demote off every shelf BEFORE deleting, exactly as the labelled path does.
        assert orphan.visibility.return_value.updateVisibility.call_args.kwargs == {
            "recommended": False,
            "home": False,
            "shared": False,
        }
        orphan.delete.assert_called_once()

    def test_the_marker_predicate_matches_delivery_verbatim(self):
        # The orphan-ownership check is duplicated in plex_pms (to avoid an import cycle); if the two
        # marker definitions ever drift, the sweep would find an orphan but delete_owned_collection
        # would refuse it and abort the run. Pin them together so drift can't ship silently.
        from shortlist.engine.clients.plex_pms import _has_shortlist_marker
        from shortlist.engine.delivery import has_marker, row_marker

        for title in (
            "✨ Movies Picked for You" + row_marker(202),  # marked → ours
            "Kometa: Best of the 90s",  # foreign → not ours
            "x" + "​" * 63,  # 63 trailing marker chars — one short of a marker
            "x" + "‌" * 65,  # 65 — a valid 64 marker preceded by another zero-width char
        ):
            assert _has_shortlist_marker(title) == has_marker(title), title

    def test_delete_demotes_then_deletes_owned(self, mock_plex: PlexClient):
        owned = MagicMock()
        owned.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        mock_plex.delete_owned_collection(owned, "shortlist")
        vis = owned.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": False, "home": False, "shared": False}
        owned.delete.assert_called_once()

    def test_promote_hides_from_library_and_promotes_shared(self, mock_plex: PlexClient):
        collection = MagicMock()
        mock_plex.promote(collection)
        collection.modeUpdate.assert_called_once_with(mode="hide")
        vis = collection.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": True, "home": True, "shared": True}
        vis.reload.return_value.move.assert_not_called()  # not pinned by default

    def test_promote_passes_placement_flags_through(self, mock_plex: PlexClient):
        """A library-only row must be hidden from Home and friends' Home — recommended only."""
        collection = MagicMock()
        mock_plex.promote(collection, recommended=True, home=False, shared=False)
        vis = collection.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": True, "home": False, "shared": False}

    def test_promote_pins_to_top_when_requested(self, mock_plex: PlexClient):
        collection = MagicMock()
        vis = collection.visibility.return_value
        vis.reload.return_value = vis
        mock_plex.promote(collection, pin_top=True)
        # modeUpdate + visibility happen first, THEN the move to the top (after=None).
        vis.move.assert_called_once_with(after=None)

    def test_owned_collections_maps_slug_to_stored_label_and_id(self, mock_plex: PlexClient):
        ours = MagicMock(ratingKey=571285)
        ours.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        kometa = MagicMock(ratingKey=9)
        kometa.labels = [SimpleNamespace(tag="Overlay")]
        section = MagicMock()
        section.type = "movie"
        section.collections.return_value = [ours, kometa]
        mock_plex._server.library.sections.return_value = [section]
        assert mock_plex.owned_collections("shortlist") == {"sarah": OwnedRow("Shortlist_sarah", [571285])}

    def test_owned_collections_collects_a_users_row_from_every_library(self, mock_plex: PlexClient):
        """One user, one collection per library. Collapsing them to a single id once hid a real
        leak: T2 compared only the last collection it saw and passed while another was visible."""
        movie_row = MagicMock(ratingKey=571285)
        movie_row.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        show_row = MagicMock(ratingKey=571290)
        show_row.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        movies, shows = MagicMock(), MagicMock()
        movies.type, shows.type = "movie", "show"
        movies.collections.return_value = [movie_row]
        shows.collections.return_value = [show_row]
        mock_plex._server.library.sections.return_value = [movies, shows]

        assert mock_plex.owned_collections("shortlist") == {"sarah": OwnedRow("Shortlist_sarah", [571285, 571290])}

    def test_section_collections_are_cached_within_a_run(self, mock_plex: PlexClient):
        # The section's collection list is otherwise re-pulled for every owned/find scan. Two reads
        # of the same section fetch it once.
        section = MagicMock(type="movie")
        section.collections.return_value = []
        mock_plex._server.library.sections.return_value = [section]
        mock_plex.owned_collections("shortlist")
        mock_plex.find_owned_collections(section, "Shortlist_sarah")
        assert section.collections.call_count == 1

    def test_create_then_label_is_findable_from_the_warm_cache(self, mock_plex: PlexClient):
        # The rollout fix + its real safety mechanism: create APPENDS the collection to the cached list
        # (no whole-cache wipe -> one section.collections() per run, not O(N^2) per user). The appended
        # object is LABEL-LESS at append time; it only becomes findable because stored_label reloads
        # THAT SAME reference in place. This proves that end-to-end (not just "an already-labeled object
        # is findable"), because a fresh read would never have missed it.
        section = MagicMock(type="movie")
        section.collections.return_value = []
        mock_plex._server.library.sections.return_value = [section]
        mock_plex.find_owned_collections(section, "x")  # populates the cache (one fetch)

        created = MagicMock(labels=[])  # created WITHOUT a shortlist label yet
        created.reload.side_effect = lambda: setattr(created, "labels", [SimpleNamespace(tag="Shortlist_sarah")])
        mock_plex._server.createCollection.return_value = created

        mock_plex.create_collection(section, "New Row", [])
        # Before labelling it is NOT findable (correctly — it has no label yet).
        assert created not in mock_plex.find_owned_collections(section, "Shortlist_sarah")
        # stored_label labels + reloads the SAME cached object in place...
        mock_plex.stored_label(created, "shortlist_sarah")
        # ...so now it IS findable, from the still-warm cache (no second section.collections() read).
        assert created in mock_plex.find_owned_collections(section, "Shortlist_sarah")
        assert section.collections.call_count == 1

    def test_delete_busts_the_collections_cache(self, mock_plex: PlexClient):
        section = MagicMock(type="movie")
        section.collections.return_value = []
        mock_plex._server.library.sections.return_value = [section]
        mock_plex.find_owned_collections(section, "x")  # populates the cache
        doomed = MagicMock(labels=[SimpleNamespace(tag="shortlist_sarah")])
        mock_plex.delete_owned_collection(doomed, "shortlist")
        mock_plex.find_owned_collections(section, "x")  # must re-fetch
        assert section.collections.call_count == 2

    def test_section_signature_uses_count_and_updated_timestamp(self, mock_plex: PlexClient):
        # The index cache invalidates ONLY on this signature, so its shape assumptions matter: a real
        # LibrarySection carries a datetime `updatedAt`, which we key on as an int timestamp.
        from datetime import UTC, datetime

        updated = datetime(2026, 7, 15, tzinfo=UTC)
        section = SimpleNamespace(totalSize=1234, updatedAt=updated)
        assert mock_plex.section_signature(section) == f"1234:{int(updated.timestamp())}"

    def test_section_signature_passes_through_a_non_datetime_updated(self, mock_plex: PlexClient):
        section = SimpleNamespace(totalSize=1234, updatedAt=999)  # already numeric — used as-is
        assert mock_plex.section_signature(section) == "1234:999"

    def test_section_signature_falls_back_to_count_alone(self, mock_plex: PlexClient):
        section = SimpleNamespace(totalSize=1234)  # no updatedAt available
        assert mock_plex.section_signature(section) == "1234:None"

    def test_section_signature_is_none_when_nothing_is_available(self, mock_plex: PlexClient):
        # No signal at all -> the cache is disabled (the pipeline always re-scans), never wrongly reused.
        assert mock_plex.section_signature(SimpleNamespace()) is None

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

    @staticmethod
    def _item(rating_key: int) -> MagicMock:
        it = MagicMock()
        it.ratingKey = rating_key
        return it

    def test_set_items_adds_removes_from_prefetched_membership_without_reading(self, mock_plex: PlexClient):
        """set_items now takes the caller's already-fetched membership + only the items to add, so it
        makes ZERO extra PMS reads (no collection.items() here). It adds/removes + pins custom sort;
        ordering is the deferred order_collection pass, so no moveItem here."""
        item = self._item
        existing = [item(1), item(2), item(3)]  # 2 will be removed
        add_items = [item(4)]  # caller fetched ONLY the delta (item 4)
        wanted_keys = [4, 1, 3]
        collection = MagicMock()

        mock_plex.set_items(collection, existing, add_items, wanted_keys)

        collection.items.assert_not_called()  # no re-read — uses the passed-in membership
        assert [i.ratingKey for i in collection.addItems.call_args.args[0]] == [4]
        assert [i.ratingKey for i in collection.removeItems.call_args.args[0]] == [2]
        collection.sortUpdate.assert_called_once_with(sort="custom")
        collection.moveItem.assert_not_called()  # ordering happens later, in order_collection

    def test_order_collection_moves_only_displaced_items(self, mock_plex: PlexClient):
        """order_collection reorders with the FEWEST moveItem calls: only items out of place move,
        since Plex's moveItem is one PMS round-trip each (the slow part). Live order 1,3,4 -> 4,1,3."""
        item = self._item
        collection = MagicMock()
        collection.items.return_value = [item(1), item(3), item(4)]

        moves_made = mock_plex.order_collection(collection, [4, 1, 3])  # wanted ranked ratingKeys

        collection.reload.assert_called_once()
        moves = collection.moveItem.call_args_list
        assert [c.args[0].ratingKey for c in moves] == [4]  # only 4 is out of place
        assert moves[0].kwargs["after"] is None  # 4 goes to the front
        assert moves_made == 1

    def test_order_collection_reverses_order_with_after_previous_chain(self, mock_plex: PlexClient):
        """The insert-after-previous math the one-move case never exercises. [1,2,3] -> [3,2,1] is two
        moves: 3 to front, 2 after 3."""
        item = self._item
        collection = MagicMock()
        collection.items.return_value = [item(1), item(2), item(3)]

        mock_plex.order_collection(collection, [3, 2, 1])

        moves = [
            (c.args[0].ratingKey, c.kwargs["after"].ratingKey if c.kwargs["after"] else None)
            for c in collection.moveItem.call_args_list
        ]
        assert moves == [(3, None), (2, 3)], f"expected 3→front then 2→after 3, got {moves}"

    def test_order_collection_makes_no_moves_when_already_in_order(self, mock_plex: PlexClient):
        """The steady-state win: a row whose order is unchanged issues ZERO moveItem calls."""
        item = self._item
        collection = MagicMock()
        collection.items.return_value = [item(1), item(2), item(3)]

        assert mock_plex.order_collection(collection, [1, 2, 3]) == 0
        collection.moveItem.assert_not_called()

    def test_order_collection_only_orders_the_visible_top(self, mock_plex: PlexClient):
        """The cap: only the top _REORDER_TOP_N (the visible head) are ordered — the tail below the fold
        is left alone, so a big row doesn't cost a move per item."""
        from shortlist.engine.clients.plex_pms import _REORDER_TOP_N

        item = self._item
        n = _REORDER_TOP_N
        # Live order is fully reversed vs wanted, so every position is out of place.
        live = [item(i) for i in range(2 * n, 0, -1)]
        collection = MagicMock()
        collection.items.return_value = live
        wanted = list(range(1, 2 * n + 1))  # 1..2n in order

        mock_plex.order_collection(collection, wanted)

        # Never more than the cap of moves, and never a move for an item past the visible head.
        moved = [c.args[0].ratingKey for c in collection.moveItem.call_args_list]
        assert len(moved) <= n
        assert all(k <= n for k in moved), f"only the top {n} should move, got {moved}"

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


class TestTraktClient:
    @respx.mock
    def test_related_crosses_tmdb_then_normalizes(self):
        respx.get("https://api.trakt.tv/search/tmdb/550").mock(
            return_value=httpx.Response(200, json=[{"movie": {"ids": {"slug": "fight-club-1999", "tmdb": 550}}}])
        )
        respx.get("https://api.trakt.tv/movies/fight-club-1999/related").mock(
            return_value=httpx.Response(
                200, json=[{"title": "Se7en", "year": 1995, "ids": {"tmdb": 807}, "genres": ["thriller"]}]
            )
        )
        out = TraktClient("cid").related(550, MediaType.MOVIE)
        assert out == [{"tmdb_id": 807, "title": "Se7en", "year": 1995, "genres": ["thriller"]}]

    @respx.mock
    def test_related_uses_the_show_endpoints_for_shows(self):
        search = respx.get("https://api.trakt.tv/search/tmdb/1399").mock(
            return_value=httpx.Response(200, json=[{"show": {"ids": {"slug": "game-of-thrones"}}}])
        )
        related = respx.get("https://api.trakt.tv/shows/game-of-thrones/related").mock(
            return_value=httpx.Response(
                200, json=[{"title": "Rome", "year": 2005, "ids": {"tmdb": 1234}, "genres": ["drama"]}]
            )
        )
        out = TraktClient("cid").related(1399, MediaType.SHOW)
        assert out == [{"tmdb_id": 1234, "title": "Rome", "year": 2005, "genres": ["drama"]}]
        assert related.called  # the /shows/ endpoint (not /movies/) was used
        assert search.calls.last.request.url.params.get("type") == "show"

    @respx.mock
    def test_unknown_seed_returns_empty_not_error(self):
        respx.get("https://api.trakt.tv/search/tmdb/999").mock(return_value=httpx.Response(200, json=[]))
        assert TraktClient("cid").related(999, MediaType.MOVIE) == []

    @respx.mock
    def test_bad_key_raises_clean_error_without_leaking_it(self):
        respx.get("https://api.trakt.tv/movies/trending").mock(return_value=httpx.Response(403))
        with pytest.raises(TraktError) as excinfo:
            TraktClient("secret-cid").ping()
        assert "rejected the API key" in str(excinfo.value)
        assert "secret-cid" not in str(excinfo.value)

    @respx.mock
    def test_related_is_cached_across_calls(self):
        # The related graph depends only on (tmdb_id, media_type), so a second call — a second user,
        # or the next nightly run — must serve from cache without re-hitting Trakt.
        search = respx.get("https://api.trakt.tv/search/tmdb/550").mock(
            return_value=httpx.Response(200, json=[{"movie": {"ids": {"slug": "fight-club-1999", "tmdb": 550}}}])
        )
        related = respx.get("https://api.trakt.tv/movies/fight-club-1999/related").mock(
            return_value=httpx.Response(200, json=[{"title": "Se7en", "ids": {"tmdb": 807}}])
        )
        client = TraktClient("cid", cache=_MemoryCache())
        first = client.related(550, MediaType.MOVIE)
        second = client.related(550, MediaType.MOVIE)
        assert first == second
        assert search.call_count == 1 and related.call_count == 1  # second call served from cache

    @respx.mock
    def test_empty_related_is_cached_too(self):
        # A seed Trakt doesn't know stays unknown for the TTL rather than being re-looked-up every run.
        search = respx.get("https://api.trakt.tv/search/tmdb/999").mock(return_value=httpx.Response(200, json=[]))
        client = TraktClient("cid", cache=_MemoryCache())
        assert client.related(999, MediaType.MOVIE) == []
        assert client.related(999, MediaType.MOVIE) == []
        assert search.call_count == 1  # the miss was cached, not re-attempted

    @respx.mock
    def test_a_trakt_error_is_never_cached(self):
        # A failure must not poison the cache — the next run should retry, not serve []. (403 isn't
        # retried, so this stays fast: no backoff sleeps.)
        respx.get("https://api.trakt.tv/search/tmdb/550").mock(return_value=httpx.Response(403))
        client = TraktClient("cid", cache=_MemoryCache())
        with pytest.raises(TraktError):
            client.related(550, MediaType.MOVIE)
        assert client._cache.get("trakt:related:movie:550:20") is None


class TestPmsPromoteRetry:
    """A promote is idempotent, so a PMS read-timeout must be retried, not fail the user (the shape
    of the SFLIX 48-user rollout, where 42 users died on one un-retried promote timeout)."""

    def test_retries_a_timeout_then_succeeds(self, monkeypatch):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)  # no real backoff waits
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ReadTimeout("slow PMS")

        plex_pms._retry_idempotent(flaky, label="row")
        assert calls["n"] == 3  # failed twice, third try landed

    def test_gives_up_after_the_last_attempt(self, monkeypatch):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)
        calls = {"n": 0}

        def always_times_out():
            calls["n"] += 1
            raise requests.exceptions.ConnectTimeout("dead PMS")

        with pytest.raises(requests.exceptions.ConnectTimeout):
            plex_pms._retry_idempotent(always_times_out, label="row", attempts=4)
        assert calls["n"] == 4  # tried the full budget, then re-raised

    def test_a_non_timeout_error_is_not_retried(self, monkeypatch):
        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ValueError("a real bug, not a timeout")

        with pytest.raises(ValueError):
            plex_pms._retry_idempotent(boom, label="row")
        assert calls["n"] == 1  # surfaced immediately, no retry


class TestTimingHTTPAdapter:
    """Every PMS HTTP call is timed so the delivery path isn't a black hole — but the PMS URL carries
    ``X-Plex-Token`` in its query string, so the log must show the path and NEVER the token (rule 9)."""

    def _real_request(self, method: str):
        # A REAL PreparedRequest, so the redaction is exercised against how `requests` actually shapes
        # path_url (the load-bearing rule-9 assumption), not a hand-built string (review, rule 11).
        import requests

        pr = requests.PreparedRequest()
        pr.prepare(
            method=method,
            url="http://pms:32400/library/collections/9/items?X-Plex-Token=SECRETTOKEN&excludeAllLeaves=1",
        )
        return pr

    def test_logs_the_path_and_status_without_leaking_the_token(self, monkeypatch):
        from requests.adapters import HTTPAdapter

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: SimpleNamespace(status_code=200))
        adapter = plex_pms._TimingHTTPAdapter()
        lines: list[str] = []
        sink = plex_pms.logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            resp = adapter.send(self._real_request("DELETE"))
        finally:
            plex_pms.logger.remove(sink)

        assert resp.status_code == 200
        joined = "\n".join(lines)
        assert "SECRETTOKEN" not in joined  # rule 9: the token must never reach the log
        assert "/library/collections/9/items" in joined
        assert "DELETE" in joined and "200" in joined

    def test_a_slow_call_is_flagged_at_warning(self, monkeypatch):
        from requests.adapters import HTTPAdapter

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: SimpleNamespace(status_code=200))
        ticks = iter([100.0, 100.0 + plex_pms._SLOW_PMS_S + 2.0])  # elapsed > the slow threshold
        monkeypatch.setattr(plex_pms.time, "monotonic", lambda: next(ticks))
        adapter = plex_pms._TimingHTTPAdapter()
        lines: list[str] = []
        sink = plex_pms.logger.add(lines.append, level="DEBUG", format="{level}|{message}")
        try:
            adapter.send(self._real_request("PUT"))
        finally:
            plex_pms.logger.remove(sink)

        joined = "\n".join(lines)
        assert "WARNING|" in joined and "SLOW" in joined

    def test_a_failing_call_still_logs_then_re_raises(self, monkeypatch):
        """The retry/timeout path is load-bearing: if super().send() raises, the adapter must time+log
        the attempt (status ERR) and let the ORIGINAL exception propagate unchanged — never swallow it,
        or a PMS timeout would stop reaching _PMS_TIMEOUTS and the whole-user retry."""
        import requests
        from requests.adapters import HTTPAdapter

        from shortlist.engine.clients import plex_pms

        def boom(self, request, **kw):
            raise requests.exceptions.ConnectionError("dead PMS")

        monkeypatch.setattr(HTTPAdapter, "send", boom)
        adapter = plex_pms._TimingHTTPAdapter()
        lines: list[str] = []
        sink = plex_pms.logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            with pytest.raises(requests.exceptions.ConnectionError):
                adapter.send(self._real_request("GET"))
        finally:
            plex_pms.logger.remove(sink)

        joined = "\n".join(lines)
        assert "ERR" in joined  # the failed attempt is still timed and logged
        assert "SECRETTOKEN" not in joined
