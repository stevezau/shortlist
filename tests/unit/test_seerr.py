"""Overseerr/Jellyseerr client tests.

Response bodies come from `tests/fixtures/overseerr_*.json`, which are **spec-derived, not
recorded** — see that directory's README. Per the testing rules we assert the REQUEST payloads (the
body fields the client is responsible for: mediaType, mediaId, seasons, userId), not merely that a
call happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from shortlist.engine import requests as requests_mod
from shortlist.engine.clients import http_retry
from shortlist.engine.clients import seerr as seerr_mod
from shortlist.engine.clients.seerr import SeerrClient, SeerrError
from shortlist.engine.models import MediaType, MissingTitle, RequestConfig, SeerrTarget

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

TARGET = SeerrTarget(url="http://overseerr.test", api_key="ok")
BASE = "http://overseerr.test/api/v1"


def _media_page() -> dict:
    return json.loads((FIXTURES / "overseerr_media_page.json").read_text())


def _fixture_ids() -> dict[int, tuple[str, int]]:
    """``{status: (media_type, tmdb_id)}`` from the recorded page — one row per status.

    Derived, never hard-coded: this fixture holds REAL ids from a real server, and re-recording it
    changes every one of them. A test that pins the numbers would have to be rewritten each time,
    which is how a fixture quietly stops being re-recorded.
    """
    return {r["status"]: (r["mediaType"], r["tmdbId"]) for r in _media_page()["results"]}


def _fixture_id(status: int, kind: str) -> int:
    """The recorded tmdb id for one (status, mediaType) pair.

    Keyed on BOTH, because several statuses appear as a movie AND a tv row and a status-only lookup
    silently returns whichever came last — which is how this helper first handed a `tv` id to a
    `MediaType.MOVIE` call and made a "skipped" test assert "requested" instead.
    """
    return next(r["tmdbId"] for r in _media_page()["results"] if r["status"] == status and r["mediaType"] == kind)


def _users_page() -> dict:
    return json.loads((FIXTURES / "overseerr_users_page.json").read_text())


def _client(**kwargs) -> SeerrClient:
    """A client whose throttle never sleeps — the rate limiter is tested by its own case."""
    return SeerrClient(kwargs.pop("target", TARGET), min_write_interval=0.0, **kwargs)


class TestPing:
    def test_ping_names_the_account_the_key_belongs_to(self):
        with respx.mock:
            route = respx.get(f"{BASE}/auth/me").mock(
                return_value=httpx.Response(200, json={"id": 1, "displayName": "serverowner"})
            )
            assert "serverowner" in _client().ping()
        assert route.calls[0].request.headers["X-Api-Key"] == "ok"

    def test_ping_uses_auth_me_not_the_unauthenticated_status_endpoint(self):
        """`/status` declares `security: []`, so it answers 200 to a wrong key.

        Testing against it would call a broken connection healthy — the single reason this client
        pings `/auth/me` instead, and the reason that choice is pinned by a test.
        """
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(401, json={"message": "denied"}))
            status = respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json={"version": "1.33.2"}))
            with pytest.raises(SeerrError, match="rejected the API key"):
                _client().ping()
        assert not status.called

    def test_a_non_json_body_says_to_check_the_url_and_proxy(self):
        """A 200 of HTML is a reverse proxy or SSO page, not the app — say which."""
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(200, text="<html>login</html>"))
            with pytest.raises(SeerrError, match="non-JSON body"):
                _client().ping()

    def test_a_403_blames_the_permission_not_the_key(self):
        """A working key whose account lacks Manage Requests. Calling that "rejected the API key"
        sends the owner off to regenerate a key that was never the problem — and the permission it
        is nearly always about is the one "Request as" needs."""
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(403, json={"message": "no"}))
            with pytest.raises(SeerrError, match="Manage Requests"):
                _client().ping()

    def test_a_401_still_blames_the_key(self):
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(401, json={"message": "no"}))
            with pytest.raises(SeerrError, match="rejected the API key"):
                _client().ping()

    def test_the_error_never_carries_the_api_key(self):
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(side_effect=httpx.ConnectError("nope"))
            with pytest.raises(SeerrError) as excinfo:
                _client().ping()
        assert "ok" not in str(excinfo.value).replace("Overseerr", "")


class TestMediaState:
    def test_maps_overseerr_status_codes_to_the_inbox_vocabulary(self):
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            state = _client().media_state()
        ids = _fixture_ids()

        def status_of(code: int) -> str | None:
            kind, tmdb_id = ids[code]
            return state[("movie" if kind == "movie" else "show", tmdb_id)]

        assert status_of(5) == "downloaded"  # AVAILABLE
        # PENDING gets its OWN word: the inbox renders "queued" as "Searching", which would dress up
        # "a person has not approved this yet" as the machine already working on it.
        assert status_of(2) == "awaiting_approval"
        # PROCESSING and PARTIALLY_AVAILABLE are NOT "downloading". Measured on the server this
        # fixture came from: 76 rows were PROCESSING and exactly ONE was moving — the rest are
        # approved-but-unreleased films and airing series, resting there indefinitely.
        assert status_of(3) == "queued"  # PROCESSING, nothing in the download client
        assert status_of(4) == "queued"  # PARTIALLY_AVAILABLE

    def test_downloadstatus_is_what_makes_a_title_downloading(self):
        """The only honest "moving right now" signal the API offers — it carries the download
        client's own sizeLeft/timeLeft, so a non-empty one is a fact rather than an inference."""
        rows = {
            "pageInfo": {"pages": 1},
            "results": [
                {"mediaType": "movie", "tmdbId": 1, "status": 3, "downloadStatus": []},
                {"mediaType": "movie", "tmdbId": 2, "status": 3, "downloadStatus": [{"sizeLeft": 42}]},
            ],
        }
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=rows))
            state = _client().media_state()
        assert state[("movie", 1)] == "queued"
        assert state[("movie", 2)] == "downloading"

    def test_a_deleted_or_unknown_row_is_not_known_so_the_title_stays_requestable(self):
        """DELETED and UNKNOWN say nothing is on its way.

        Treating them as "present" would make a title the server once held, and no longer has,
        permanently unrequestable — a hole in the library that nothing could ever fill. Between them
        these were 1,828 of the 5,000 sampled rows, so this is the common case, not an edge.

        DELETED is 7 here, which is documented NOWHERE — not in Overseerr's spec and not in the
        server's own shipped seerr-api.yml, both of which say 6 and stop. Only the running code says
        otherwise. Which is why the mapping is an allow-list: an unrecognised code means "unknown",
        and unknown means requestable.
        """
        ids = _fixture_ids()
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            state = _client().media_state()
        for code in (1, 7):
            kind, tmdb_id = ids[code]
            assert ("movie" if kind == "movie" else "show", tmdb_id) not in state

    def test_show_ids_are_keyed_by_tmdb_not_tvdb(self):
        """The reason this target needs no TVDB crossing at all.

        The fixture's Game of Thrones row carries BOTH ids (tmdb 1399, tvdb 121361); reading the
        wrong one would silently key the whole show side against a namespace the inbox never uses.
        """
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            state = _client().media_state()
        row = next(r for r in _media_page()["results"] if r["mediaType"] == "tv" and r.get("tvdbId"))
        assert ("show", row["tmdbId"]) in state
        assert ("show", row["tvdbId"]) not in state

    def test_pages_until_a_short_page(self):
        size = SeerrClient._PAGE_SIZE
        page_one = {
            "pageInfo": {"pages": 2},
            "results": [{"mediaType": "movie", "tmdbId": i, "status": 5} for i in range(size)],
        }
        page_two = {"pageInfo": {"pages": 2}, "results": [{"mediaType": "movie", "tmdbId": 999_999, "status": 5}]}
        with respx.mock:
            route = respx.get(f"{BASE}/media").mock(
                side_effect=[httpx.Response(200, json=page_one), httpx.Response(200, json=page_two)]
            )
            state = _client().media_state()
        assert len(state) == size + 1
        assert ("movie", 999_999) in state
        # Derived from the constant, not hard-coded: the page size is a measured value that has
        # already changed once (100 -> 1000 after walking a real 26,941-row library).
        assert route.calls[1].request.url.params["skip"] == str(size)

    def test_a_page_with_no_usable_mediatype_warns_rather_than_reading_as_an_empty_library(self):
        """The one shape that would fail silently.

        `mediaType` is undocumented in the published MediaInfo schema, so a fork dropping it would
        make every row unusable — and an unusable page is byte-identical to a healthy empty library.
        The run still fails open (a redundant request, never a wrong one), but it must say so.
        """
        rows = {"pageInfo": {"pages": 1}, "results": [{"tmdbId": 273481, "status": 5}]}
        # loguru does not route through stdlib logging, so `caplog` sees nothing — sink pattern.
        lines: list[str] = []
        sink = seerr_mod.logger.add(lines.append, level="WARNING", format="{message}")
        try:
            with respx.mock:
                respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=rows))
                state = _client().media_state()
        finally:
            seerr_mod.logger.remove(sink)
        assert state == {}
        assert any("mediaType" in line for line in lines)

    def test_state_is_fetched_once_per_client(self):
        """The run's reconcile and the send's presence check must not walk the library twice."""
        with respx.mock:
            route = respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            client = _client()
            client.media_state()
            client.media_state()
        assert route.call_count == 1


class TestUsers:
    def test_names_every_account_for_the_request_as_dropdown(self):
        with respx.mock:
            respx.get(f"{BASE}/user").mock(return_value=httpx.Response(200, json=_users_page()))
            users = _client().users()
        # `permissions` comes straight from the recorded fixture; the two auto-approve flags are
        # derived from it with the bits read off a live Seerr (ADMIN=2 implies every permission,
        # which is why the owner row approves without carrying an AUTO_APPROVE bit at all).
        assert users == [
            {"id": 1, "name": "serverowner", "auto_approve_movies": True, "auto_approve_tv": True},
            {"id": 4, "name": "Shortlist", "auto_approve_movies": False, "auto_approve_tv": False},
            {"id": 7, "name": "MooHouse", "auto_approve_movies": False, "auto_approve_tv": False},
        ]

    def test_falls_back_through_the_documented_name_fields(self):
        """`displayName` is what the live API returns but is NOT in the published User schema."""
        payload = {
            "pageInfo": {"pages": 1},
            "results": [
                {"id": 2, "username": "local-user"},
                {"id": 3, "plexUsername": "plex-user"},
                {"id": 5, "email": "only@example.test"},
                {"id": 6},
            ],
        }
        with respx.mock:
            respx.get(f"{BASE}/user").mock(return_value=httpx.Response(200, json=payload))
            users = _client().users()
        assert [u["name"] for u in users] == ["local-user", "plex-user", "only@example.test", "User 6"]
        # No `permissions` at all reads as no auto-approval — the cautious direction, since the
        # screen uses it to promise whether a title starts downloading.
        assert all(not u["auto_approve_movies"] and not u["auto_approve_tv"] for u in users)


class TestRequestTitle:
    def _mock_media(self, payload: dict | None = None):
        return respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=payload or _media_page()))

    def test_a_movie_request_sends_media_type_and_the_tmdb_id(self):
        with respx.mock:
            self._mock_media()
            post = respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 9}))
            status, _, slug = _client().request_title(603, MediaType.MOVIE, dry_run=False)
        assert status == "requested"
        assert slug is None
        assert json.loads(post.calls[0].request.content) == {"mediaType": "movie", "mediaId": 603}

    def test_a_show_request_asks_for_all_seasons(self):
        """Overseerr accepts a show request with no seasons and then never sends it to Sonarr —
        the request sits approved forever with nothing behind it."""
        with respx.mock:
            self._mock_media()
            post = respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 9}))
            _client().request_title(94997, MediaType.SHOW, dry_run=False)
        assert json.loads(post.calls[0].request.content) == {
            "mediaType": "tv",
            "mediaId": 94997,
            "seasons": "all",
        }

    def test_request_as_user_id_is_sent_when_set(self):
        target = SeerrTarget(url="http://overseerr.test", api_key="ok", request_as_user_id=4)
        with respx.mock:
            self._mock_media()
            post = respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 9}))
            _client(target=target).request_title(603, MediaType.MOVIE, dry_run=False)
        assert json.loads(post.calls[0].request.content)["userId"] == 4

    def test_user_id_is_omitted_entirely_when_unset_not_sent_as_null(self):
        """A null `userId` is not the same as no `userId` — omitting it lets the instance apply its
        own default, which is what "Server default" in the UI promises."""
        with respx.mock:
            self._mock_media()
            post = respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 9}))
            _client().request_title(603, MediaType.MOVIE, dry_run=False)
        assert "userId" not in json.loads(post.calls[0].request.content)

    def test_a_title_overseerr_already_knows_is_skipped_not_re_requested(self):
        with respx.mock:
            self._mock_media()
            post = respx.post(f"{BASE}/request")
            tmdb_id = _fixture_id(5, "movie")  # an AVAILABLE film from the recorded page
            status, detail, _ = _client().request_title(tmdb_id, MediaType.MOVIE, dry_run=False)
        assert status == "skipped_present"
        assert "downloaded" in detail
        assert not post.called

    def test_dry_run_writes_nothing(self):
        with respx.mock:
            self._mock_media()
            post = respx.post(f"{BASE}/request")
            status, _, _ = _client().request_title(603, MediaType.MOVIE, dry_run=True)
        assert status == "would_request"
        assert not post.called

    def test_a_refused_request_keeps_the_apps_own_message(self):
        """Any non-2xx, whatever it means. Overseerr's code for a duplicate is deliberately NOT
        special-cased: the API docs do not state it, and guessing one would turn a real failure
        into a silent "already there". The app's own words reach the inbox instead."""
        with respx.mock:
            self._mock_media()
            respx.post(f"{BASE}/request").mock(
                return_value=httpx.Response(409, json={"message": "Request for this media already exists"})
            )
            with pytest.raises(SeerrError, match="already exists"):
                _client().request_title(603, MediaType.MOVIE, dry_run=False)


class TestFailingOpen:
    """What happens when the media walk cannot be done — the contract the run path depends on."""

    def test_a_request_still_goes_out_when_the_library_cannot_be_read(self):
        """The bug this pins: `request_title` raised, so `_apply_seerr_state`'s fail-open was undone
        one step later and EVERY title in the run became an error instead of being requested."""
        with respx.mock:
            respx.get(f"{BASE}/media").mock(side_effect=httpx.ConnectError("down"))
            post = respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 9}))
            status, _, _ = _client().request_title(603, MediaType.MOVIE, dry_run=False)
        assert status == "requested"
        assert json.loads(post.calls[0].request.content) == {"mediaType": "movie", "mediaId": 603}

    def test_a_failed_walk_is_attempted_once_per_client_not_once_per_title(self):
        """Three HTTP retries live inside each attempt, so re-walking per title turns one outage
        into a long stall on every send in the run."""
        with respx.mock:
            route = respx.get(f"{BASE}/media").mock(side_effect=httpx.ConnectError("down"))
            respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 9}))
            client = _client()
            for tmdb_id in (603, 604, 605):
                client.request_title(tmdb_id, MediaType.MOVIE, dry_run=False)
        # One attempt, retried internally by http_retry — never a second walk.
        assert route.call_count == http_retry.DEFAULT_ATTEMPTS

    def test_the_status_endpoint_still_learns_the_walk_failed(self):
        """`request_title` swallows it; `media_state` must NOT, or the inbox could not tell
        "Overseerr tracks none of these" from "Overseerr never answered"."""
        with respx.mock:
            respx.get(f"{BASE}/media").mock(side_effect=httpx.ConnectError("down"))
            client = _client()
            with pytest.raises(SeerrError):
                client.media_state()
            with pytest.raises(SeerrError):
                client.media_state()  # the memoised error, re-raised rather than re-fetched


class TestNaming:
    def test_ping_falls_back_through_the_name_fields_and_never_crashes_on_a_bare_reply(self):
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(200, json={"id": 1}))
            assert _client().ping().endswith("?")

    def test_a_blank_display_name_does_not_win_over_a_real_username(self):
        """`or`-chaining already skipped "", but not "   " — which reads as a nameless account."""
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(
                return_value=httpx.Response(200, json={"id": 1, "displayName": "   ", "username": "real"})
            )
            assert "real" in _client().ping()


class TestTheEngineDrivesTheRealClient:
    """`request_missing` against the REAL `SeerrClient`, with only HTTP faked.

    Every other engine test uses `FakeSeerr`, and a fake can drift from the interface it stands in
    for — it already did: `_send_claims` began keying its client cache by `client.target` and the
    fake, lacking the attribute, AttributeError'd on a path the tests were meant to be covering.
    This is the one case where nothing between the pipeline and the wire is a stand-in.
    """

    def _demand(self, *titles):
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def _cfg(self):
        return RequestConfig(
            enabled=True,
            overseerr=TARGET,
            min_rating=7.0,
            min_votes=100,
            max_per_run=10,
            auto_min_demand=1,
            auto_min_rating=0.0,
        )

    def _mock_blocklist(self, rows=None):
        return respx.get(f"{BASE}/blocklist").mock(
            return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": rows or []})
        )

    def _run(self, demand, *, dry_run=False):
        cfg = self._cfg()
        return requests_mod.request_missing(
            cfg,
            _EngineTmdb(),
            [requests_mod.RowRequest("picked", cfg, demand)],
            dry_run=dry_run,
            min_write_interval=0.0,
        )

    def test_a_movie_and_a_show_reach_the_wire_correctly_shaped(self):
        demand = self._demand(
            MissingTitle(603, "a movie", MediaType.MOVIE, 1999, rating=8.7, vote_count=900, demand=3),
            MissingTitle(94997, "a show", MediaType.SHOW, 2022, rating=8.8, vote_count=900, demand=3),
        )
        with respx.mock:
            self._mock_blocklist()
            respx.get(f"{BASE}/media").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": []})
            )
            post = respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 1}))
            report = self._run(demand)

        bodies = sorted((json.loads(c.request.content) for c in post.calls), key=lambda b: b["mediaId"])
        assert bodies == [
            {"mediaType": "movie", "mediaId": 603},
            {"mediaType": "tv", "mediaId": 94997, "seasons": "all"},
        ]
        assert {o.status for o in report.outcomes} == {"requested"}
        assert len(report.sent) == 2

    def test_the_run_walks_media_once_for_the_reconcile_and_the_send_together(self):
        demand = self._demand(
            *(
                MissingTitle(i, f"title {i}", MediaType.MOVIE, 1999, rating=8.7, vote_count=900, demand=3)
                for i in (603, 604, 605)
            )
        )
        with respx.mock:
            self._mock_blocklist()
            media = respx.get(f"{BASE}/media").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": []})
            )
            respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 1}))
            self._run(demand)
        # The reconcile client is seeded into the send's cache, so three sends add no second walk.
        assert media.call_count == 1

    def test_a_title_overseerr_already_has_never_reaches_the_wire(self):
        present_id = _fixture_id(5, "movie")  # an AVAILABLE film from the recorded page
        demand = self._demand(
            MissingTitle(present_id, "already there", MediaType.MOVIE, 2015, rating=8.7, vote_count=900, demand=3),
        )
        with respx.mock:
            self._mock_blocklist()
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            post = respx.post(f"{BASE}/request")
            report = self._run(demand)
        assert not post.called
        assert report.sent == []

    def test_an_unreachable_overseerr_still_sends_rather_than_erroring_every_title(self):
        """The fail-open contract, proved through the pipeline rather than at the client alone."""
        demand = self._demand(
            MissingTitle(603, "a movie", MediaType.MOVIE, 1999, rating=8.7, vote_count=900, demand=3),
        )
        with respx.mock:
            self._mock_blocklist()
            respx.get(f"{BASE}/media").mock(side_effect=httpx.ConnectError("down"))
            post = respx.post(f"{BASE}/request").mock(return_value=httpx.Response(201, json={"id": 1}))
            report = self._run(demand)
        assert post.called
        assert [o.status for o in report.outcomes] == ["requested"]

    def test_a_dry_run_reaches_no_write_at_all(self):
        demand = self._demand(
            MissingTitle(603, "a movie", MediaType.MOVIE, 1999, rating=8.7, vote_count=900, demand=3),
        )
        with respx.mock:
            self._mock_blocklist()
            respx.get(f"{BASE}/media").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": []})
            )
            post = respx.post(f"{BASE}/request")
            report = self._run(demand, dry_run=True)
        assert not post.called
        assert [o.status for o in report.outcomes] == ["would_request"]


class _EngineTmdb:
    """The TMDB surface `request_missing` touches. None of it is used on the Overseerr route — no
    TVDB crossing exists there — but the enrichment pass still asks for art and a synopsis."""

    def tvdb_id(self, tmdb_id: int, media_type) -> int | None:
        raise AssertionError("the Overseerr route must never need a TVDB id")

    def imdb_id(self, tmdb_id: int, media_type) -> str | None:
        return None

    def poster_path(self, tmdb_id: int, media_type) -> str:
        return ""

    def overview(self, tmdb_id: int, media_type) -> str:
        return ""


class TestTheBlocklist:
    """The exclusion list this route was wrongly documented as lacking."""

    def test_blocklisted_titles_come_back_keyed_by_type_and_id(self):
        rows = {
            "pageInfo": {"pages": 1},
            "results": [
                {"tmdbId": 603, "title": "x", "media": {"mediaType": "movie", "tmdbId": 603}},
                {"tmdbId": 1399, "title": "y", "media": {"mediaType": "tv", "tmdbId": 1399}},
            ],
        }
        with respx.mock:
            respx.get(f"{BASE}/blocklist").mock(return_value=httpx.Response(200, json=rows))
            assert _client().blocklisted() == {("movie", 603), ("show", 1399)}

    def test_a_row_with_no_media_object_blocks_both_types(self):
        """Half-knowing that the owner said never is not a reason to ask."""
        rows = {"pageInfo": {"pages": 1}, "results": [{"tmdbId": 603, "title": "x"}]}
        with respx.mock:
            respx.get(f"{BASE}/blocklist").mock(return_value=httpx.Response(200, json=rows))
            assert _client().blocklisted() == {("movie", 603), ("show", 603)}

    def test_it_falls_back_to_the_deprecated_alias(self):
        with respx.mock:
            respx.get(f"{BASE}/blocklist").mock(return_value=httpx.Response(404))
            respx.get(f"{BASE}/blacklist").mock(
                return_value=httpx.Response(
                    200,
                    json={"pageInfo": {"pages": 1}, "results": [{"tmdbId": 7, "media": {"mediaType": "movie"}}]},
                )
            )
            assert _client().blocklisted() == {("movie", 7)}

    def test_an_instance_serving_neither_simply_has_no_blocklist(self):
        """Classic Overseerr. Fails OPEN — a redundant request, never a suppressed title."""
        with respx.mock:
            respx.get(f"{BASE}/blocklist").mock(return_value=httpx.Response(404))
            respx.get(f"{BASE}/blacklist").mock(return_value=httpx.Response(404))
            assert _client().blocklisted() == set()

    def test_a_successful_empty_read_is_authoritative_and_does_not_try_the_alias(self):
        """An empty blocklist is an answer, not a failure — the real server this was built against
        has exactly that, and re-asking the deprecated alias would be a wasted round trip."""
        with respx.mock:
            respx.get(f"{BASE}/blocklist").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": []})
            )
            alias = respx.get(f"{BASE}/blacklist")
            assert _client().blocklisted() == set()
        assert not alias.called

    def test_it_is_read_once_per_client(self):
        with respx.mock:
            route = respx.get(f"{BASE}/blocklist").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": []})
            )
            client = _client()
            client.blocklisted()
            client.blocklisted()
        assert route.call_count == 1

    def test_a_broken_blocklist_never_fails_the_reconcile(self):
        """It sits inside the same guard as the media walk: a reconcile problem must cost a redundant
        request, never the whole request pass."""
        from shortlist.engine.requests import _apply_seerr_state

        class Boom(SeerrClient):
            def media_state(self):
                return {}

            def blocklisted(self):
                raise SeerrError("Overseerr unreachable (ConnectError)")

        pool = [MissingTitle(603, "a movie", MediaType.MOVIE, 1999, rating=8.7, vote_count=900, demand=3)]
        kept, dropped, present = _apply_seerr_state(pool, Boom(TARGET))
        assert kept == pool and dropped == 0 and present == set()


class TestWhoAutoApproves:
    """Which accounts skip Overseerr's approval queue — bits read off a live Seerr 3.4.1."""

    def _users(self, *perms: int) -> list[dict]:
        rows = [{"id": i, "displayName": f"u{i}", "permissions": p} for i, p in enumerate(perms)]
        with respx.mock:
            respx.get(f"{BASE}/user").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": rows})
            )
            return _client().users()

    def test_admin_approves_everything_without_any_auto_approve_bit(self):
        """The case that matters most: an owner's API key is an admin, and Overseerr's own permission
        check short-circuits on ADMIN — so it approves instantly while carrying none of the
        AUTO_APPROVE bits. Reading only those bits would label it "requests wait", which is the exact
        opposite of what it does."""
        [admin] = self._users(2)
        assert admin["auto_approve_movies"] and admin["auto_approve_tv"]

    def test_the_blanket_bit_covers_both_types(self):
        [u] = self._users(128 | 32)
        assert u["auto_approve_movies"] and u["auto_approve_tv"]

    def test_per_type_bits_are_read_separately(self):
        films, shows = self._users(256 | 32, 512 | 32)
        assert films["auto_approve_movies"] and not films["auto_approve_tv"]
        assert shows["auto_approve_tv"] and not shows["auto_approve_movies"]

    def test_a_plain_requester_approves_nothing(self):
        """32 = REQUEST, which is what every ordinary Plex user on a real instance carries."""
        [u] = self._users(32)
        assert not u["auto_approve_movies"] and not u["auto_approve_tv"]
