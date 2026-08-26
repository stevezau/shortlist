"""Radarr/Sonarr client tests.

Response bodies mirror the documented Sonarr/Radarr v3 API shapes (movie/series lookup resources,
qualityprofile, rootfolder, system/status). Per the testing rules we assert the REQUEST payloads —
the fields the client is responsible for (quality profile, root folder, monitored, addOptions) — not
merely that a call happened; a call-count-only assertion is exactly the bug class those rules cite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from shortlist.engine.clients import arr as arr_mod
from shortlist.engine.clients.arr import ArrError, RadarrClient, SonarrClient
from shortlist.engine.models import SONARR_MONITOR_MODES, ArrTarget

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

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
# A REAL /api/v3/series/lookup response (Sonarr 4.0.19), for a show that instance does not track.
#
# Recorded rather than hand-built because of one field: a real lookup returns every season already
# `monitored: true`, and `add_series` posts `{**resource, ...}` — so the body says "all seasons
# monitored" while `addOptions.monitor` says "firstSeason". The hand-built stub had `seasons: []`,
# which quietly removed the only interesting thing about the payload (testing rule: the fake must be
# no easier than the real server).
SERIES_LOOKUP = json.loads((FIXTURES / "sonarr_series_lookup.json").read_text())[0]
SERIES_TVDB = SERIES_LOOKUP["tvdbId"]


class TestRadarrAddMovie:
    @respx.mock
    def test_posts_target_profile_folder_and_search_when_missing(self):
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        status, _, _ = RadarrClient(RADARR).add_movie(273481, dry_run=False)

        assert status == "requested"
        body = json.loads(post.calls.last.request.content)
        # The fields the client controls — not just "a POST happened".
        assert body["qualityProfileId"] == 4
        assert body["rootFolderPath"] == "/movies"
        assert body["monitored"] is True
        assert body["addOptions"] == {"searchForMovie": True}
        assert body["tmdbId"] == 273481  # carried through from the lookup resource
        assert body["tags"] == []  # no tag configured on this target -> no tag call, empty list
        assert post.calls.last.request.headers["X-Api-Key"] == "rk"

    @respx.mock
    def test_returns_the_titleslug_for_the_inbox_deep_link(self):
        # The send log deep-links straight to the arr page; Sonarr has no id URL, so the titleSlug
        # from the lookup resource must be returned to the caller.
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))
        status, _, slug = RadarrClient(RADARR).add_movie(273481, dry_run=False)
        assert (status, slug) == ("requested", "sicario-273481")

    @respx.mock
    def test_lookup_actually_sends_the_tmdb_id_in_the_query(self):
        # Regression: httpx >=0.28 REPLACES a URL's query with the `params` arg, so a bare params={}
        # alongside `?tmdbId=…` in the path silently dropped the id — Radarr then 500s on a lookup with
        # no tmdbId and EVERY request failed. respx matches by path, so the old test passed blind to
        # this; assert the id is really on the wire.
        route = respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        RadarrClient(RADARR).add_movie(273481, dry_run=True)
        assert route.calls.last.request.url.params.get("tmdbId") == "273481"
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=4, root_folder="/movies", tag="shortlist"
        )
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        respx.get("http://radarr.test/api/v3/tag").mock(return_value=httpx.Response(200, json=[]))
        create = respx.post("http://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(201, json={"id": 7, "label": "shortlist"})
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        RadarrClient(tagged).add_movie(273481, dry_run=False)

        assert create.called  # the tag didn't exist, so it was created
        assert json.loads(post.calls.last.request.content)["tags"] == [7]

    @respx.mock
    def test_tags_are_sanitized_to_the_arr_charset(self):
        # Radarr/Sonarr tags allow only a-z 0-9 - ; a capital/space/dot/+ 400s the WHOLE add (the
        # Evangelion send that failed live). Every tag is normalised before it's created.
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        respx.get("http://radarr.test/api/v3/tag").mock(return_value=httpx.Response(200, json=[]))
        created: list[str] = []

        def make_tag(request):
            label = json.loads(request.content)["label"]
            created.append(label)
            return httpx.Response(201, json={"id": len(created), "label": label})

        respx.post("http://radarr.test/api/v3/tag").mock(side_effect=make_tag)
        respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        # Includes an all-punctuation label that reduces to "" — it must be DROPPED, never POSTed as
        # an empty tag (which the Arr also 400s).
        RadarrClient(RADARR).add_movie(273481, dry_run=False, extra_tags={"J.FM", "Sci-Fi Night!", "kids", "!!!"})

        assert all(re.fullmatch(r"[a-z0-9-]+", t) for t in created)  # nothing the Arr would reject
        assert set(created) == {"j-fm", "sci-fi-night", "kids"}  # the "!!!" label produced no tag

    @respx.mock
    def test_reuses_an_existing_tag_case_insensitively(self):
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=4, root_folder="/movies", tag="shortlist"
        )
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        respx.get("http://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(200, json=[{"id": 3, "label": "Shortlist"}])
        )
        create = respx.post("http://radarr.test/api/v3/tag").mock(return_value=httpx.Response(201))
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        RadarrClient(tagged).add_movie(273481, dry_run=False)

        assert not create.called  # an existing tag (any case) is reused, never duplicated
        assert json.loads(post.calls.last.request.content)["tags"] == [3]

    @respx.mock
    def test_extra_tags_merge_with_the_global_tag_reusing_and_creating(self):
        # Global tag "shortlist" already exists; the per-user "sarah" tag doesn't. The add applies
        # BOTH: the existing id is reused, the new one is created — and the tag list is fetched once.
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=4, root_folder="/movies", tag="shortlist"
        )
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        tag_get = respx.get("http://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(200, json=[{"id": 3, "label": "shortlist"}])
        )
        respx.post("http://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(201, json={"id": 9, "label": "sarah"})
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        RadarrClient(tagged).add_movie(273481, dry_run=False, extra_tags={"sarah"})

        assert json.loads(post.calls.last.request.content)["tags"] == [3, 9]  # global + per-user, sorted
        assert tag_get.call_count == 1  # the existing-tag list is fetched once, not per label

    @respx.mock
    def test_global_and_extra_tag_dedupe_case_insensitively(self):
        # Global "shortlist" and an extra "Shortlist" are the same tag — one id, no duplicate create.
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=4, root_folder="/movies", tag="shortlist"
        )
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        respx.get("http://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(200, json=[{"id": 3, "label": "Shortlist"}])
        )
        create = respx.post("http://radarr.test/api/v3/tag").mock(return_value=httpx.Response(201))
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        RadarrClient(tagged).add_movie(273481, dry_run=False, extra_tags={"Shortlist"})

        assert json.loads(post.calls.last.request.content)["tags"] == [3]  # one id, not two
        assert not create.called  # the case-variant duplicate is never created

    @respx.mock
    def test_tag_list_is_fetched_once_across_multiple_adds(self):
        # The docstring promise: N titles on one client never re-query the tag list.
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=4, root_folder="/movies", tag="shortlist"
        )
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        tag_get = respx.get("http://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(200, json=[{"id": 3, "label": "shortlist"}])
        )
        respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 5}))

        client = RadarrClient(tagged)
        client.add_movie(273481, dry_run=False)
        client.add_movie(273481, dry_run=False)

        assert tag_get.call_count == 1  # cached on the client, not re-fetched per add

    @respx.mock
    def test_dry_run_with_a_tag_configured_creates_no_tag(self):
        # Creating a tag is a write; plex-safety rule 8 says a dry-run must make none.
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=4, root_folder="/movies", tag="shortlist"
        )
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        tag_get = respx.get("http://radarr.test/api/v3/tag").mock(return_value=httpx.Response(200, json=[]))
        tag_post = respx.post("http://radarr.test/api/v3/tag").mock(return_value=httpx.Response(201, json={"id": 7}))

        status, _, _ = RadarrClient(tagged).add_movie(273481, dry_run=True)

        assert status == "would_request"
        assert not tag_get.called and not tag_post.called  # no tag lookup or creation in a dry run

    @respx.mock
    def test_dry_run_makes_no_post(self):
        lookup = respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json=MOVIE_LOOKUP)
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201))

        status, _, _ = RadarrClient(RADARR).add_movie(273481, dry_run=True)

        assert status == "would_request"
        assert lookup.called
        assert not post.called  # dry-run must never write

    @respx.mock
    def test_skips_when_already_present(self):
        respx.get("http://radarr.test/api/v3/movie/lookup/tmdb").mock(
            return_value=httpx.Response(200, json={**MOVIE_LOOKUP, "id": 99})
        )
        post = respx.post("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(201))

        status, detail, _ = RadarrClient(RADARR).add_movie(273481, dry_run=False)

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
    def test_lookup_actually_sends_the_tvdb_term_in_the_query(self):
        # Same httpx-0.28 query-drop regression as Radarr: a bare params={} stripped `?term=tvdb:…`,
        # so Sonarr 503'd on a lookup with no term. Assert the term is really on the wire.
        route = respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[SERIES_LOOKUP])
        )
        SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=True)
        assert route.calls.last.request.url.params.get("term") == f"tvdb:{SERIES_TVDB}"

    @respx.mock
    def test_posts_target_profile_folder_and_search_when_missing(self):
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[SERIES_LOOKUP])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 3}))

        status, _, _ = SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=False)

        assert status == "requested"
        body = json.loads(post.calls.last.request.content)
        assert body["qualityProfileId"] == 7
        assert body["rootFolderPath"] == "/tv"
        assert body["monitored"] is True
        assert body["seasonFolder"] is True
        assert body["addOptions"] == {"searchForMissingEpisodes": True, "monitor": "all"}
        assert body["tvdbId"] == SERIES_TVDB
        # tags==[] here (no tag configured). The create/reuse tag path lives in the shared
        # _ArrClient._tag_ids() and is exercised by the Radarr tag tests above, so it isn't
        # duplicated for Sonarr; add_series wires it in the same way (body["tags"]).
        assert body["tags"] == []

    @pytest.mark.parametrize("mode", ["all", "firstSeason", "lastSeason", "pilot"])
    @respx.mock
    def test_posts_the_monitor_mode_it_was_given(self, mode):
        # The whole of issue #100: "all" backfills every season of a 12-season show the night it is
        # added. The mode is what the owner chose, so assert it reaches the wire — not merely that
        # a POST happened. Parametrised rather than one case plus a note: the four are the same
        # passthrough today, and the parametrise is what keeps that true if one ever stops being.
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[SERIES_LOOKUP])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 3}))

        SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=False, monitor=mode)

        body = json.loads(post.calls.last.request.content)
        assert body["addOptions"] == {"searchForMissingEpisodes": True, "monitor": mode}
        assert body["monitored"] is True

    @respx.mock
    def test_the_posted_seasons_stay_as_the_lookup_returned_them(self):
        """The body carries `seasons` with EVERY season monitored — straight out of the lookup — while
        `addOptions.monitor` asks for one. Sonarr resolves that in favour of the monitor option on its
        post-add scan, which is the single assumption this feature rests on and cannot be asserted
        against a stub. Measured on a real Sonarr 4.0.19: adding with `firstSeason` left 26 of 162
        episodes monitored (season 1 only), `pilot` left 1, `none` left 0.

        So this test does not claim Sonarr's behaviour. It pins OURS: we send the lookup's seasons
        untouched. If a future change starts hand-building that array, this fails and the measurement
        above has to be redone rather than assumed.
        """
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[SERIES_LOOKUP])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 3}))

        SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=False, monitor="firstSeason")

        body = json.loads(post.calls.last.request.content)
        assert [s["monitored"] for s in SERIES_LOOKUP["seasons"] if s["seasonNumber"] > 0] == [True] * 6, (
            "the recorded fixture must keep a real lookup's all-seasons-monitored shape"
        )
        assert body["seasons"] == SERIES_LOOKUP["seasons"]

    @respx.mock
    def test_none_adds_the_show_unmonitored_and_searches_for_nothing(self):
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[SERIES_LOOKUP])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 3}))

        SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=False, monitor="none")

        body = json.loads(post.calls.last.request.content)
        assert body["monitored"] is False
        assert body["addOptions"] == {"searchForMissingEpisodes": False, "monitor": "none"}

    @respx.mock
    def test_an_unknown_mode_falls_back_to_all_rather_than_failing_the_add(self):
        # Sonarr 400s on a monitor value it doesn't know. The API validates what is SAVED, so this is
        # the stored-value case: a setting written by a newer build and read back by an older one.
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[SERIES_LOOKUP])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 3}))

        status, _, _ = SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=False, monitor="seasonsIWant")

        assert status == "requested"
        assert json.loads(post.calls.last.request.content)["addOptions"]["monitor"] == "all"

    @respx.mock
    def test_skips_when_already_present(self):
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[{**SERIES_LOOKUP, "id": 12}])
        )
        post = respx.post("http://sonarr.test/api/v3/series").mock(return_value=httpx.Response(201))

        status, _, _ = SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=False)

        assert status == "skipped_present"
        assert not post.called

    @respx.mock
    def test_errors_when_tvdb_not_found_in_results(self):
        # A term search can return near-matches; the client must only add the exact tvdbId.
        respx.get("http://sonarr.test/api/v3/series/lookup").mock(
            return_value=httpx.Response(200, json=[{**SERIES_LOOKUP, "tvdbId": 999999}])
        )
        status, _, _ = SonarrClient(SONARR).add_series(SERIES_TVDB, dry_run=False)
        assert status == "error"


class TestTheMonitorModeTable:
    def test_every_offered_mode_has_a_plain_english_detail(self):
        # `add_series` indexes this table by the resolved mode on every successful add, so a mode
        # added to the list without a detail is a KeyError on a real request, not a missing string.
        assert set(arr_mod._MONITOR_DETAIL) == set(SONARR_MONITOR_MODES)


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


class TestArrStateIdSets:
    """The bulk id-set fetches that let the request pass reconcile against what an Arr already knows
    (already-tracked → drop; on an exclusion list → flag), without a per-title call."""

    @respx.mock
    def test_radarr_library_and_exclusion_tmdb_ids(self):
        respx.get("http://radarr.test/api/v3/movie").mock(
            # A row with no tmdbId (0) and a malformed one must be tolerated, not poison the set.
            return_value=httpx.Response(200, json=[{"tmdbId": 11}, {"tmdbId": 22}, {"tmdbId": 0}, {"title": "x"}])
        )
        respx.get("http://radarr.test/api/v3/exclusions").mock(
            return_value=httpx.Response(200, json=[{"tmdbId": 99, "movieTitle": "Old"}])
        )
        client = RadarrClient(RADARR)
        assert client.library_tmdb_ids() == {11, 22}
        assert client.excluded_tmdb_ids() == {99}

    @respx.mock
    def test_sonarr_exclusion_tvdb_ids(self):
        respx.get("http://sonarr.test/api/v3/importlistexclusion").mock(
            return_value=httpx.Response(200, json=[{"tvdbId": 12345, "title": "Gone"}])
        )
        client = SonarrClient(SONARR)
        assert client.excluded_tvdb_ids() == {12345}


class TestArrDownloadStatus:
    """The whole-library status maps behind the request inbox's Downloaded/Downloading badges.

    Covers the matrix each app branches on: on disk / partly on disk / in the download queue /
    monitored-but-empty / not monitored. The count of HTTP calls is asserted too — the point of the
    map form is that it does NOT scale with the number of rows on screen.
    """

    @respx.mock
    def test_radarr_status_per_state(self):
        library = respx.get("http://radarr.test/api/v3/movie").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 1, "tmdbId": 11, "hasFile": True, "monitored": True},
                    {"id": 2, "tmdbId": 22, "hasFile": False, "monitored": True},  # in the queue below
                    {"id": 3, "tmdbId": 33, "hasFile": False, "monitored": True},
                    {"id": 4, "tmdbId": 44, "hasFile": False, "monitored": False},
                    {"id": 5, "hasFile": True, "monitored": True},  # no tmdbId — unkeyable, skipped
                ],
            )
        )
        queue = respx.get("http://radarr.test/api/v3/queue").mock(
            return_value=httpx.Response(200, json={"records": [{"movieId": 2}]})
        )

        assert RadarrClient(RADARR).status_by_tmdb() == {
            11: "downloaded",
            22: "downloading",
            33: "queued",
            44: "unmonitored",
        }
        # Two calls for the whole library, however many titles the inbox is asking about.
        assert library.call_count == 1
        assert queue.call_count == 1
        # The queue is paginated and defaults to 20 records — without this a busy server would report
        # older downloads as merely "queued".
        assert queue.calls.last.request.url.params["pageSize"] == "1000"

    @respx.mock
    def test_sonarr_status_per_state_keyed_by_both_ids(self):
        respx.get("http://sonarr.test/api/v3/series").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "tvdbId": 100,
                        "tmdbId": 900,
                        "monitored": True,
                        "statistics": {"episodeCount": 10, "episodeFileCount": 10},
                    },
                    {
                        "id": 2,
                        "tvdbId": 200,
                        "tmdbId": 901,
                        "monitored": True,
                        "statistics": {"episodeCount": 10, "episodeFileCount": 3},
                    },
                    {
                        "id": 3,
                        "tvdbId": 300,
                        "monitored": True,  # v3: no tmdbId, and nothing on disk yet
                        "statistics": {"episodeCount": 10, "episodeFileCount": 0},
                    },
                    {"id": 4, "tvdbId": 400, "monitored": False, "statistics": {}},
                ],
            )
        )
        respx.get("http://sonarr.test/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))

        by_tvdb, by_tmdb = SonarrClient(SONARR).status_by_ids()

        # A part-way season reads as "downloading" — that is what the person waiting on it sees.
        assert by_tvdb == {100: "downloaded", 200: "downloading", 300: "queued", 400: "unmonitored"}
        # Only the v4 rows carry a tmdbId, so the tmdb-keyed inbox can answer without a TVDB lookup.
        assert by_tmdb == {900: "downloaded", 901: "downloading"}

    @respx.mock
    def test_zero_episode_series_is_not_downloaded(self):
        """A show Sonarr has added but not yet populated has 0 of 0 episodes — that is 'searching',
        not 'complete'. A naive `on_disk >= episodes` would call it downloaded."""
        respx.get("http://sonarr.test/api/v3/series").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "tvdbId": 100,
                        "monitored": True,
                        "statistics": {"episodeCount": 0, "episodeFileCount": 0},
                    }
                ],
            )
        )
        respx.get("http://sonarr.test/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))

        by_tvdb, _ = SonarrClient(SONARR).status_by_ids()
        assert by_tvdb == {100: "queued"}

    @respx.mock
    def test_unavailable_queue_does_not_fail_the_whole_map(self):
        """A queue the app won't serve costs the downloading/queued distinction, never every status."""
        respx.get("http://radarr.test/api/v3/movie").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "tmdbId": 11, "hasFile": False, "monitored": True}])
        )
        respx.get("http://radarr.test/api/v3/queue").mock(return_value=httpx.Response(500))

        assert RadarrClient(RADARR).status_by_tmdb() == {11: "queued"}


class TestArrQueuePaging:
    """`_queued_ids` used to read only the first 1000-record page with no check against
    `totalRecords`, so a server with a bigger active queue silently under-reported which titles were
    downloading. Exercised directly through `status_by_tmdb`, the one production caller."""

    @staticmethod
    def _movie(tmdb_id: int) -> dict:
        return {"id": tmdb_id, "tmdbId": tmdb_id, "hasFile": False, "monitored": True}

    @respx.mock
    def test_pages_past_the_first_1000_records(self):
        respx.get("http://radarr.test/api/v3/movie").mock(
            return_value=httpx.Response(200, json=[self._movie(11), self._movie(22)])
        )
        full_page = [{"movieId": i} for i in range(2000, 3000)]  # none of ours
        short_page = [{"movieId": 11}]  # movieId 11 is on the second page only

        def respond(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            body = full_page if page == 1 else short_page
            return httpx.Response(200, json={"records": body, "totalRecords": 1001})

        queue = respx.get("http://radarr.test/api/v3/queue").mock(side_effect=respond)

        # movie id 11's Radarr `id` is 11 (see `_movie`), and it only appears on page 2 of the queue —
        # a client that stopped after page 1 would report it merely "queued", not "downloading".
        assert RadarrClient(RADARR).status_by_tmdb()[11] == "downloading"
        assert queue.call_count == 2
        assert queue.calls[0].request.url.params["page"] == "1"
        assert queue.calls[1].request.url.params["page"] == "2"

    @respx.mock
    def test_a_short_page_below_totalRecords_logs_a_warning_but_still_returns_what_it_read(self):
        """A short page usually means the end. If the app also reports a bigger `totalRecords`, the
        two disagree — this must not raise (rule: fails OPEN), but it must say so."""
        respx.get("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(200, json=[self._movie(11)]))
        respx.get("http://radarr.test/api/v3/queue").mock(
            return_value=httpx.Response(200, json={"records": [{"movieId": 11}], "totalRecords": 500})
        )
        lines: list[str] = []
        from shortlist.engine.clients import arr as arr_mod

        sink = arr_mod.logger.add(lines.append, level="WARNING", format="{message}")
        try:
            status = RadarrClient(RADARR).status_by_tmdb()
        finally:
            arr_mod.logger.remove(sink)

        assert status[11] == "downloading"  # what WAS read is still used
        assert any("read only" in line and "500" in line for line in lines)

    @respx.mock
    def test_a_server_that_never_returns_a_short_page_stops_at_the_safety_cap(self):
        """If paging never legitimately ends (the app ignores `page` and always answers a full page),
        this must still terminate rather than loop forever."""
        respx.get("http://radarr.test/api/v3/movie").mock(return_value=httpx.Response(200, json=[self._movie(11)]))
        full_page = [{"movieId": i} for i in range(2000, 3000)]  # none of ours
        respx.get("http://radarr.test/api/v3/queue").mock(
            return_value=httpx.Response(200, json={"records": full_page})  # no totalRecords, always full
        )

        status = RadarrClient(RADARR).status_by_tmdb()

        assert status[11] == "queued"  # 11 never appeared in any page, so no crash — just not found
        assert RadarrClient(RADARR)._MAX_QUEUE_PAGES == 50


class TestTheWriteClockBelongsToTheServer:
    """Audit round 6, 2026-08-18: per-row request settings mean several clients can point at ONE
    Radarr with different root folders. A rate limiter each would multiply the write rate to that
    server by the number of rows — the opposite of what plex-safety rule 6 asks for."""

    def test_two_clients_on_one_server_share_a_clock(self, monkeypatch):
        waits: list[float] = []
        monkeypatch.setattr(
            arr_mod.http_retry,
            "throttle",
            lambda last, interval, on_wait=None: (waits.append(interval - last), 10.0)[1],
        )
        clock = [0.0]
        kids = ArrTarget(url="http://radarr", api_key="k", quality_profile_id=9, root_folder="/kids")
        main = ArrTarget(url="http://radarr", api_key="k", quality_profile_id=1, root_folder="/movies")

        a = RadarrClient(kids, min_write_interval=1.0, write_clock=clock)
        b = RadarrClient(main, min_write_interval=1.0, write_clock=clock)
        a._throttle()
        b._throttle()

        # The second client saw the first one's write, so it spaced itself against it.
        assert clock == [10.0]
        assert waits[1] == 1.0 - 10.0, "the second client throttled against the shared clock"

    def test_a_lone_client_still_owns_its_clock(self):
        """Nothing else in the codebase passes one, so the default must keep working unchanged."""
        target = ArrTarget(url="http://radarr", api_key="k", quality_profile_id=1, root_folder="/m")
        assert RadarrClient(target)._write_clock == [0.0]

    def test_clients_on_different_servers_do_not_share(self):
        one = RadarrClient(ArrTarget(url="http://a", api_key="k", quality_profile_id=1, root_folder="/m"))
        two = RadarrClient(ArrTarget(url="http://b", api_key="k", quality_profile_id=1, root_folder="/m"))
        assert one._write_clock is not two._write_clock
