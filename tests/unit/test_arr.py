"""Radarr/Sonarr client tests.

Response bodies mirror the documented Sonarr/Radarr v3 API shapes (movie/series lookup resources,
qualityprofile, rootfolder, system/status). Per the testing rules we assert the REQUEST payloads —
the fields the client is responsible for (quality profile, root folder, monitored, addOptions) — not
merely that a call happened; a call-count-only assertion is exactly the bug class those rules cite.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from rowarr.engine.clients.arr import ArrError, RadarrClient, SonarrClient
from rowarr.engine.models import ArrTarget

pytestmark = pytest.mark.integration

RADARR = ArrTarget(url="http://radarr.test", api_key="rk", quality_profile_id=4, root_folder="/movies")
SONARR = ArrTarget(url="http://sonarr.test", api_key="sk", quality_profile_id=7, root_folder="/tv")

# A Radarr movie/lookup/tmdb resource for a title NOT yet added (id absent → 0).
MOVIE_LOOKUP = {
    "title": "Sicario",
    "year": 2015,
    "tmdbId": 273481,
    "titleSlug": "sicario-273481",
    "images": [],
}
# A Sonarr series/lookup resource for a show NOT yet added.
SERIES_LOOKUP = {
    "title": "Severance",
    "tvdbId": 371980,
    "titleSlug": "severance",
    "seasons": [],
    "images": [],
}


class TestRadarrAddMovie:
    @respx.mock
    def test_posts_target_profile_folder_and_search_when_missing(self):
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        status, _ = RadarrClient(RADARR).add_movie(273481, dry_run=False)

        assert status == "requested"
        body = json.loads(post.calls.last.request.content)
        # The fields the client controls — not just "a POST happened".
        assert body["qualityProfileId"] == 4
        assert body["rootFolderPath"] == "/movies"
        assert body["monitored"] is True
        assert body["addOptions"] == {"searchForMovie": True}
        assert body["tmdbId"] == 273481  # carried through from the lookup resource
        assert post.calls.last.request.headers["X-Api-Key"] == "rk"

    @respx.mock
    def test_dry_run_makes_no_post(self):
        lookup = respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201))

        status, _ = RadarrClient(RADARR).add_movie(273481, dry_run=True)

        assert status == "would_request"
        assert lookup.called
        assert not post.called  # dry-run must never write

    @respx.mock
    def test_skips_when_already_present(self):
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json={**MOVIE_LOOKUP, "id": 99})
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201))

        status, detail = RadarrClient(RADARR).add_movie(273481, dry_run=False)

        assert status == "skipped_present"
        assert not post.called
        assert "already" in detail

    @respx.mock
    def test_rejected_add_raises_arr_error_with_app_message(self):
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        respx.post("http://radarr.test/api/v3/movie").mock(
            return_value=httpx.Response(400, json=[{"errorMessage": "This movie has already been added"}])
        )
        with pytest.raises(ArrError) as excinfo:
            RadarrClient(RADARR).add_movie(273481, dry_run=False)
        assert "already been added" in str(excinfo.value)


class TestSonarrAddSeries:
    @respx.mock
    def test_posts_target_profile_folder_and_search_when_missing(self):
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[SERIES_LOOKUP])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 3}))

        status, _ = SonarrClient(SONARR).add_series(371980, dry_run=False)

        assert status == "requested"
        body = json.loads(post.calls.last.request.content)
        assert body["qualityProfileId"] == 7
        assert body["rootFolderPath"] == "/tv"
        assert body["monitored"] is True
        assert body["seasonFolder"] is True
        assert body["addOptions"] == {"searchForMissingEpisodes": True, "monitor": "all"}
        assert body["tvdbId"] == 371980

    @respx.mock
    def test_skips_when_already_present(self):
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[{**SERIES_LOOKUP, "id": 12}])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201))

        status, _ = SonarrClient(SONARR).add_series(371980, dry_run=False)

        assert status == "skipped_present"
        assert not post.called

    @respx.mock
    def test_errors_when_tvdb_not_found_in_results(self):
        # A term search can return near-matches; the client must only add the exact tvdbId.
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[{**SERIES_LOOKUP, "tvdbId": 999999}])
        )
        status, _ = SonarrClient(SONARR).add_series(371980, dry_run=False)
        assert status == "error"


class TestArrPlumbing:
    @respx.mock
    def test_ping_reports_app_and_version(self):
        respx.get("http://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"appName": "Radarr", "version": "5.2.6"})
        )
        assert RadarrClient(RADARR).ping() == "Connected to Radarr 5.2.6"

    @respx.mock
    def test_bad_key_raises_clean_error_without_leaking_key(self):
        respx.get("http://radarr.test/api/v3/system/status").mock(return_value=httpx.Response(401))
        with pytest.raises(ArrError) as excinfo:
            RadarrClient(RADARR).ping()
        assert "rejected the API key" in str(excinfo.value)
        assert "rk" not in str(excinfo.value)  # the api key must never appear in the message

    @respx.mock
    def test_options_reduce_to_id_name_and_id_path(self):
        respx.get("http://radarr.test/api/v3/qualityprofile").mock(
            return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p", "items": []}])
        )
        respx.get("http://radarr.test/api/v3/rootfolder").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "path": "/movies", "freeSpace": 123}])
        )
        client = RadarrClient(RADARR)
        assert client.quality_profiles() == [{"id": 4, "name": "HD-1080p"}]
        assert client.root_folders() == [{"id": 1, "path": "/movies"}]
