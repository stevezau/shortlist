"""Boundary clients: plex.tv XML/throttle, TMDB pooling+cache, Tautulli, PMS helpers."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
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
USERS_XML = (FIXTURES / "plextv_users.xml.txt").read_text()


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
    def test_the_roster_read_outlasts_a_container_whose_network_is_merely_late(self, monkeypatch):
        """The default three attempts (~3s of backoff) are not enough for the one read whose failure
        aborts the entire run. A user's first run died on `ConnectError: [Errno -3] Temporary failure
        in name resolution` — the container had started before its DNS had — and the identical manual
        re-run seconds later succeeded. Four straight connect failures must still resolve to a roster,
        not to a server-wide "nothing written, nothing promoted"."""
        monkeypatch.setattr(plextv_mod.http_retry.time, "sleep", lambda _: None)
        calls = {"n": 0}

        def dns_is_not_up_yet(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] <= 4:
                raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
            return httpx.Response(200, text=USERS_XML)

        route = respx.get("https://plex.tv/api/users").mock(side_effect=dns_is_not_up_yet)
        respx.get("https://plex.tv/api/home/users").mock(return_value=httpx.Response(500))

        users = self._client().list_users()

        assert [u.id for u in users] == [555000100, 555000200, 555000300]
        assert route.call_count == 5, "the 4th retry is past the default ladder — that is the point"

    @respx.mock
    def test_a_roster_read_that_never_recovers_still_raises(self, monkeypatch):
        """The longer ladder must not become an infinite one. When plex.tv is genuinely unreachable the
        run has to fail loudly — the pipeline's abort is what stops it promoting rows it cannot prove
        are hidden (rule 1)."""
        monkeypatch.setattr(plextv_mod.http_retry.time, "sleep", lambda _: None)
        route = respx.get("https://plex.tv/api/users").mock(side_effect=httpx.ConnectError("no DNS, ever"))

        with pytest.raises(httpx.ConnectError):
            self._client().list_users()

        # The literal, not the constant: `== _ROSTER_ATTEMPTS` passes for ANY value including 50, so
        # the one test named for keeping the ladder bounded could never fail on it.
        assert route.call_count == 6

    @respx.mock
    def test_only_user_elements_become_users(self):
        """Any other child of the container — a `<Server>` block, an error node — used to become an
        account with `id=0` and no filters. That matters beyond a junk row: the user sync compares its
        own roster against this list to decide who has LEFT the share, so a response-shape change could
        read as "everybody departed" and switch every account off (rule 11)."""
        injected = re.sub(r"(<MediaContainer[^>]*>)", r'\1<Server name="something-new" />', USERS_XML, count=1)
        respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=injected))

        users = self._client().list_users()

        assert [u.id for u in users] == [555000100, 555000200, 555000300]

    @respx.mock
    def test_home_restriction_profiles_separates_managed_users_plex_cannot(self):
        """`/api/users` says `restricted="1"` for EVERY managed account. Only `/api/home/users` says
        which of them actually has a parental preset — the distinction issue #20 turns on, and the one
        that decides whether Plex will even accept a label restriction."""
        home_xml = (FIXTURES / "plextv_home_users.xml.txt").read_text()
        respx.get("https://plex.tv/api/home/users").mock(return_value=httpx.Response(200, text=home_xml))

        profiles = self._client().home_restriction_profiles()

        assert profiles[555000200] == "little_kid"  # has a preset -> Plex refuses label filters
        assert profiles[555000300] == ""  # ATTRIBUTE ABSENT entirely -> no preset, filters are accepted
        assert profiles[555000001] == ""  # the owner

    @respx.mock
    def test_the_profile_lands_on_the_right_user_through_list_users(self):
        """The JOIN is the load-bearing step, and it is invisible if the two endpoints disagree about
        the id space. With disjoint ids every other test still passes while the enrichment matches
        NOTHING — a feature that is a silent no-op in production behind a green suite."""
        respx.get("https://plex.tv/api/users").mock(
            return_value=httpx.Response(200, text=(FIXTURES / "plextv_users.xml.txt").read_text())
        )
        respx.get("https://plex.tv/api/home/users").mock(
            return_value=httpx.Response(200, text=(FIXTURES / "plextv_home_users.xml.txt").read_text())
        )

        by_id = {u.id: u for u in self._client().list_users()}

        assert by_id[555000200].restriction_profile == "little_kid", "the join matched nothing"
        assert by_id[555000100].restriction_profile == ""  # an ordinary shared user
        # The #20 cell itself: restricted="1" on /api/users, but NO profile on /api/home/users. It is
        # the account that never got its excludes, so it has to survive the join as profile-less
        # rather than being lumped in with the parental-controlled ones.
        assert by_id[555000300].restricted is True
        assert by_id[555000300].restriction_profile == ""

    def test_a_log_title_is_legible_and_says_whose_row_it_is(self):
        """Every delivery/promote/ordering line printed the raw title — which carries a 64-character
        zero-width per-account marker. The log looked corrupted, wrapped absurdly, and two users'
        rows were impossible to tell apart by eye, because the ONLY thing distinguishing them is
        invisible. That is the log an operator reads to debug a user's report."""
        from shortlist.engine.clients.plex_pms import log_title
        from shortlist.engine.delivery import row_marker

        marked = "✨ Movies Picked for You" + row_marker(218833834)
        assert len(marked) == len("✨ Movies Picked for You") + 64

        rendered = log_title(marked)
        assert rendered == "✨ Movies Picked for You [acct 218833834]"
        # No invisible characters survive into the log line.
        assert not any(c in ("\u200b", "\u200c") for c in rendered)

    def test_a_log_title_leaves_an_unmarked_title_alone(self):
        """Kometa's collections and anything else on the server must pass through untouched."""
        from shortlist.engine.clients.plex_pms import log_title

        assert log_title("Christmas Favourites") == "Christmas Favourites"

    @respx.mock
    def test_an_omitted_account_is_unknown_not_unprofiled(self):
        """A 200 is not the same as a complete answer.

        `home_profile_known` used to be a single global "the read succeeded" flag, so an empty or
        partial `<MediaContainer>` counted as knowledge about everybody in it AND everybody not.
        A genuinely profiled child then read as having no profile, their share-filter 422 looked
        unexpected, and the pipeline blocked promotion for EVERY user on the server, nightly, behind
        a green suite — #14's shape re-created by the guard added to prevent it.
        """
        partial = '<MediaContainer><User id="555000200" restrictionProfile="little_kid"/></MediaContainer>'
        respx.get("https://plex.tv/api/home/users").mock(return_value=httpx.Response(200, text=partial))

        client = self._client()
        client.home_restriction_profiles()  # a successful read that simply does not mention 555000999

        assert client.home_profile_known(555000200) is True
        assert client.home_profile_known(555000999) is False, "the roster never mentioned them"

    @respx.mock
    def test_an_empty_but_successful_roster_is_knowledge_about_nobody(self):
        """The starkest case: HTTP 200, well-formed, zero users."""
        respx.get("https://plex.tv/api/home/users").mock(
            return_value=httpx.Response(200, text="<MediaContainer></MediaContainer>")
        )

        client = self._client()
        assert client.home_restriction_profiles() == {}
        assert client.home_profile_known(555000200) is False

    @respx.mock
    def test_a_failed_read_is_knowledge_about_nobody_either(self):
        respx.get("https://plex.tv/api/home/users").mock(return_value=httpx.Response(500))

        client = self._client()
        assert client.home_restriction_profiles() == {}
        assert client.home_profile_known(555000200) is False

    @respx.mock
    def test_a_malformed_home_user_id_does_not_sink_the_whole_roster(self):
        """This parse used to sit outside the try. One junk id raised out of `list_users()`, which the
        pipeline reads as "could not read the plex.tv user list" — no filters written for ANYONE and
        nothing promoted, server-wide, over a bad character on a secondary endpoint."""
        junk = '<MediaContainer><User id="not-a-number" restrictionProfile="teen"/>'
        junk += '<User id="555000200" restrictionProfile="little_kid"/></MediaContainer>'
        respx.get("https://plex.tv/api/home/users").mock(return_value=httpx.Response(200, text=junk))

        profiles = self._client().home_restriction_profiles()

        assert profiles == {555000200: "little_kid"}, "the good row must survive the bad one"

    @respx.mock
    def test_the_profile_lookup_is_fetched_once_per_client(self):
        """`list_users()` is called several times per run — privacy sync, the read-back verification,
        uninstall's per-user restore. Without caching, each paid a second plex.tv GET for a value most
        of them never read (rule 6: plex.tv is shared infrastructure)."""
        respx.get("https://plex.tv/api/users").mock(
            return_value=httpx.Response(200, text=(FIXTURES / "plextv_users.xml.txt").read_text())
        )
        route = respx.get("https://plex.tv/api/home/users").mock(
            return_value=httpx.Response(200, text=(FIXTURES / "plextv_home_users.xml.txt").read_text())
        )
        client = self._client()

        client.list_users()
        client.list_users()
        client.list_users()

        assert route.call_count == 1

    @respx.mock
    def test_a_home_users_failure_leaves_profiles_blank_rather_than_failing_the_roster(self):
        """Blank reads as "no preset", so the caller ATTEMPTS the write and plex.tv gets the final say
        (a 422 is already handled). Failing the whole roster read over an enrichment would strand every
        user's excludes over a hiccup on a secondary endpoint."""
        users_xml = (FIXTURES / "plextv_users.xml.txt").read_text()
        respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=users_xml))
        respx.get("https://plex.tv/api/home/users").mock(return_value=httpx.Response(500))

        users = self._client().list_users()

        assert users, "the roster must still be returned"
        assert all(u.restriction_profile == "" for u in users)

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
        # The CLOCK is frozen too, not just sleep. `throttle()` waits `pace - elapsed`, so with a
        # live clock this asserted on how long the test itself took to get here: it wanted >= 0.9
        # and got 0.74 on a loaded CI runner that had spent 0.26s between the two writes. Freezing
        # monotonic makes the wait exactly the pace, which is the thing under test — the old version
        # was passing by luck on fast machines.
        monkeypatch.setattr(plextv_mod.time, "monotonic", lambda: 0.0)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(429), httpx.Response(200)]
        client = self._client()
        assert client._pace == 0.0  # starts fast — no fixed 1/s
        client.update_user_filters(100, {"filterMovies": "x=y"})
        assert len(route.calls) == 2  # the 429 was retried to success
        # The 429 widened the pace to >= 1s (plex-safety rule 6) and the retry waited exactly that;
        # the clean write then eased it partway back, so it ends above the floor but below the jump.
        assert max(sleeps, default=0) >= 1.0
        assert 0.0 < client._pace < 1.0

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    @respx.mock
    def test_a_transient_5xx_is_retried_because_the_filter_PUT_is_idempotent(self, status, monkeypatch):
        """Losing a filter write is a PRIVACY problem, not a missing feature.

        The `label!=shortlist_*` exclusion is what hides one person's row from everyone else (rule
        1), so a dropped write leaves a row unhidden until the next run. This PUT carries the full
        pre-merged value rather than a delta (rule 3's merge happened upstream), so re-sending it
        either applies the same value or re-applies it as a no-op — which is what makes retrying a
        5xx safe here, where it would not be on a Radarr add.
        """
        monkeypatch.setattr(plextv_mod.time, "sleep", lambda _s: None)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(status), httpx.Response(200)]
        self._client().update_user_filters(100, {"filterMovies": "label!=Shortlist_a"})
        assert len(route.calls) == 2
        # The RETRY must carry the same value — a retry that sent something else would be a
        # different write, and rule 3 forbids rebuilding a filter.
        assert route.calls.last.request.url.params["filterMovies"] == "label!=Shortlist_a"

    @respx.mock
    def test_a_4xx_verdict_is_not_retried(self, monkeypatch):
        """A 400 is plex.tv's answer about this account, not a blip — retrying only wastes the run."""
        monkeypatch.setattr(plextv_mod.time, "sleep", lambda _s: None)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(400, text="nope"), httpx.Response(200)]
        with pytest.raises(RuntimeError):
            self._client().update_user_filters(100, {"filterMovies": "x=y"})
        assert len(route.calls) == 1

    @respx.mock
    def test_relentless_5xx_gives_up_fast_because_every_account_pays_this(self, monkeypatch):
        """Asserts the ladder's COST, not just its length.

        The privacy phase writes a filter for every account in the audience, so a per-account wait is
        paid ~46 times over on a bad night — and after the first hard failure the run cannot promote
        anything anyway. Sharing the connect-error ladder cost 90s per account (~69 minutes across a
        real roster) and no test could see it, because they all patch `sleep` away.
        """
        sleeps: list[float] = []
        monkeypatch.setattr(plextv_mod.time, "sleep", sleeps.append)
        route = respx.put("https://plex.tv/api/users/100").mock(return_value=httpx.Response(503))
        with pytest.raises(RuntimeError, match="503"):
            self._client().update_user_filters(100, {"filterMovies": "x=y"})
        assert 1 < len(route.calls) <= 4
        assert sum(sleeps) <= 20, f"{sum(sleeps)}s per account is too long to pay 46 times"

    @respx.mock
    def test_a_5xx_give_up_still_carries_plex_tvs_own_words(self, monkeypatch):
        """Issue #1: "HTTP 500" alone leaves an operator guessing WHICH account and why. That string
        reaches them through `report.promotion_blockers`, so dropping the body makes a permanently
        failing account undiagnosable from the UI."""
        monkeypatch.setattr(plextv_mod.time, "sleep", lambda _s: None)
        respx.put("https://plex.tv/api/users/100").mock(
            return_value=httpx.Response(503, text="account is not eligible for label filters")
        )
        with pytest.raises(RuntimeError, match="not eligible for label filters"):
            self._client().update_user_filters(100, {"filterMovies": "x=y"})

    @respx.mock
    def test_a_5xx_does_not_slow_the_adaptive_pace(self, monkeypatch):
        """A distinct matrix cell from the 429 test above: 429 means "you are going too fast" and
        must widen the pace (rule 6); a 5xx means plex.tv is unwell and must not."""
        monkeypatch.setattr(plextv_mod.time, "sleep", lambda _s: None)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(503), httpx.Response(200)]
        client = self._client()
        client.update_user_filters(100, {"filterMovies": "x=y"})
        assert client._pace == 0.0

    @respx.mock
    def test_relentless_429_backs_off_then_gives_up_without_looping_forever(self, monkeypatch):
        monkeypatch.setattr(plextv_mod.time, "sleep", lambda _s: None)  # don't actually wait
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = [httpx.Response(429)] * 8  # plex.tv never relents
        with pytest.raises(RuntimeError, match="rate-limiting"):
            self._client().update_user_filters(100, {"filterMovies": "x=y"})
        assert len(route.calls) == 6  # bounded retries — it gives up, never loops forever

    @respx.mock
    def test_relentless_connect_failure_gives_up_with_the_real_reason_not_throttling(self, monkeypatch):
        """A run of pure connect failures used to raise 'plex.tv still throttling filter update…',
        which sends the operator to the wrong diagnosis on the most privacy-sensitive write path
        (never a single 429). The final error must name what actually happened."""
        monkeypatch.setattr(plextv_mod.time, "sleep", lambda _s: None)
        route = respx.put("https://plex.tv/api/users/100")
        route.side_effect = httpx.ConnectError("never landed")
        with pytest.raises(RuntimeError, match="unreachable") as excinfo:
            self._client().update_user_filters(100, {"filterMovies": "x=y"})
        assert "throttl" not in str(excinfo.value).lower()
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

    @respx.mock
    def test_shared_server_tokens_maps_each_users_id_to_their_server_token(self):
        # The share-token watched read hinges on this: plex.tv mints a per-user accessToken for every
        # shared invite, keyed by their plex.tv userID. Home users appear here too; entries missing
        # either attribute are skipped rather than crashing the parse.
        xml = (
            '<MediaContainer size="3">'
            '<SharedServer userID="100" username="sarah" accessToken="SARAH-TOK"/>'
            '<SharedServer userID="200" username="kid" accessToken="KID-TOK"/>'
            '<SharedServer username="pending-invite"/>'  # no userID/accessToken yet — skipped
            "</MediaContainer>"
        )
        respx.get("https://plex.tv/api/servers/machine1/shared_servers").mock(
            return_value=httpx.Response(200, text=xml)
        )
        tokens = self._client().shared_server_tokens()
        assert tokens == {100: "SARAH-TOK", 200: "KID-TOK"}


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
        assert sorted(item["id"] for item, _affinity in pooled) == [10, 20, 30]
        affinities = {item["id"]: affinity for item, affinity in pooled}
        assert affinities[10] > affinities[30], "/recommendations vouches harder than /similar"
        # id 20 is second in /recommendations and FIRST in /similar. With lists this short the
        # /similar claim (0.6, top of its list) actually beats the /recommendations one (0.5,
        # bottom of its list) — and taking the max is the point: a title keeps the best case made
        # for it, whichever endpoint made it.
        assert affinities[20] == 0.6
        assert affinities[10] == 1.0 and affinities[30] == pytest.approx(0.3)

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
    def test_search_sends_only_the_query_and_ranks_the_year_locally(self):
        """The year ranks, it no longer filters — and that is the point of the change.

        Sending `year=` (or `first_air_date_year=`) made TMDB exclude everything else, so a proposal
        whose year was one out returned NOTHING and the title was lost entirely. Sources disagree
        about years constantly: a series gets dated by its premiere, a film by its festival run.
        Ranking keeps the near-miss and still puts the right release first.
        """
        route = respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 42, "title": "Dune"}, {"id": 43}]})
        )
        found = TmdbClient("k").search("Dune", MediaType.MOVIE, year=2021)
        assert found["id"] == 42
        params = route.calls.last.request.url.params
        assert params.get("query") == "Dune"
        assert params.get("year") is None
        assert params.get("first_air_date_year") is None

    @respx.mock
    def test_search_prefers_an_exact_title_over_a_more_popular_one(self):
        """TMDB's own order is popularity, which is quietly wrong for shared and remade titles —
        exactly the case that puts an unrelated film in someone's row."""
        respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 1, "title": "Poor Things: The Making Of", "release_date": "2024-01-01"},
                        {"id": 2, "title": "Poor Things", "release_date": "2023-12-07"},
                    ]
                },
            )
        )
        assert TmdbClient("k").search("Poor Things", MediaType.MOVIE, year=2023)["id"] == 2

    @respx.mock
    def test_search_uses_the_year_to_separate_two_exact_titles(self):
        """A remake and its original share a title exactly, so only the year can tell them apart."""
        respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 1, "title": "Dune", "release_date": "1984-12-14"},
                        {"id": 2, "title": "Dune", "release_date": "2021-09-15"},
                    ]
                },
            )
        )
        assert TmdbClient("k").search("Dune", MediaType.MOVIE, year=2021)["id"] == 2
        assert TmdbClient("k").search("Dune", MediaType.MOVIE, year=1984)["id"] == 1

    @respx.mock
    def test_search_keeps_a_title_whose_year_is_one_out(self):
        """Half of what web extraction produces has no year at all, and plenty of the rest is off by
        one. Neither may cost us the title — under the old filter, both did."""
        respx.get("https://api.themoviedb.org/3/search/tv").mock(
            return_value=httpx.Response(
                200, json={"results": [{"id": 95396, "name": "Severance", "first_air_date": "2022-02-17"}]}
            )
        )
        assert TmdbClient("k").search("Severance", MediaType.SHOW, year=2023)["id"] == 95396
        assert TmdbClient("k").search("Severance", MediaType.SHOW)["id"] == 95396

    @respx.mock
    def test_search_ignores_punctuation_differences_in_the_title(self):
        """A title copied out of an article carries a curly apostrophe; TMDB stores a straight one."""
        respx.get("https://api.themoviedb.org/3/search/tv").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 1, "name": "Daredevil", "first_air_date": "2015-04-10"},
                        {"id": 2, "name": "Marvel's Daredevil", "first_air_date": "2015-04-10"},
                    ]
                },
            )
        )
        # The curly apostrophe is the point of the test, not a typo — hence the noqa.
        assert TmdbClient("k").search("Marvel’s Daredevil", MediaType.SHOW)["id"] == 2  # noqa: RUF001

    @respx.mock
    def test_search_falls_back_to_tmdb_order_when_nothing_matches_well(self):
        """No title or year signal to go on — keep the old behaviour rather than invent a preference."""
        respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 7}, {"id": 8}]})
        )
        assert TmdbClient("k").search("Something Else", MediaType.MOVIE)["id"] == 7

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
    def test_poster_path_reads_the_movie_detail_endpoint(self):
        respx.get("https://api.themoviedb.org/3/movie/603").mock(
            return_value=httpx.Response(200, json={"id": 603, "poster_path": "/matrix.jpg"})
        )
        assert TmdbClient("k").poster_path(603, MediaType.MOVIE) == "/matrix.jpg"

    @respx.mock
    def test_poster_path_reads_the_tv_detail_endpoint(self):
        # The movie/tv split is the whole branch in this method — a show must not be asked for at
        # /movie/{id}, which is a DIFFERENT title (ids are unique only within their namespace).
        respx.get("https://api.themoviedb.org/3/tv/95396").mock(
            return_value=httpx.Response(200, json={"id": 95396, "poster_path": "/severance.jpg"})
        )
        assert TmdbClient("k").poster_path(95396, MediaType.SHOW) == "/severance.jpg"

    @respx.mock
    def test_poster_path_is_empty_when_tmdb_has_no_artwork(self):
        # TMDB returns the key present but null for a title with no poster. This MUST become "" and
        # never None: it is written to a NOT NULL column, so a None would fail the whole persist.
        respx.get("https://api.themoviedb.org/3/movie/603").mock(
            return_value=httpx.Response(200, json={"id": 603, "poster_path": None})
        )
        assert TmdbClient("k").poster_path(603, MediaType.MOVIE) == ""

    @respx.mock
    def test_poster_path_is_empty_when_the_title_is_unknown(self):
        respx.get("https://api.themoviedb.org/3/movie/999999").mock(return_value=httpx.Response(404, json={}))
        assert TmdbClient("k").poster_path(999999, MediaType.MOVIE) == ""

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
    def test_a_404_miss_is_cached_like_trakts_related_deliberately_does(self):
        """Without this, a title TMDB 404s on gets re-fetched every run for every user who has it as
        a seed — trakt.py's `related()` already caches its own misses, with a comment saying why."""
        route = respx.get("https://api.themoviedb.org/3/tv/95396/external_ids").mock(return_value=httpx.Response(404))
        client = TmdbClient("k", cache=_MemoryCache())

        first = client.tvdb_id(95396, MediaType.SHOW)
        second = client.tvdb_id(95396, MediaType.SHOW)

        assert first is None and second is None
        assert route.call_count == 1, "the second call must be a cache hit, not a second 404"

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
    def test_ping_success(self):
        route = respx.get("http://taut.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "success", "data": {}}})
        )
        assert TautulliClient("http://taut.test", "key").ping() is True
        assert route.calls.last.request.url.params["cmd"] == "status"

    @respx.mock
    def test_api_failure_raises(self):
        respx.get("http://taut.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "error", "message": "bad key"}})
        )
        with pytest.raises(RuntimeError, match="bad key"):
            TautulliClient("http://taut.test", "key").friendly_names()

    @respx.mock
    def test_api_key_never_appears_in_error_messages(self):
        respx.get("http://taut.test/api/v2").mock(return_value=httpx.Response(502))
        with pytest.raises(RuntimeError) as excinfo:
            TautulliClient("http://taut.test", "SUPERSECRETKEY").friendly_names()
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
        index = mock_plex.build_library_index(section)
        assert index == {42: 1}

    def test_build_library_index_skips_a_malformed_tmdb_guid(self, mock_plex: PlexClient):
        """A guid whose id isn't a real integer (a bad scrape, a corrupted agent match) must not raise
        out of the whole section scan — every other tolerant spot in this file skips a bad row rather
        than failing the caller, and this was the one place that didn't."""
        section = MagicMock()
        section.title = "Movies"
        section.totalSize = 2
        section.all.return_value = [
            SimpleNamespace(ratingKey=1, title="Malformed", guids=[SimpleNamespace(id="tmdb://not-a-number")]),
            fake_media_item(2, "Good", tmdb_id=99),
        ]
        index = mock_plex.build_library_index(section)
        assert index == {99: 2}

    def test_stored_label_returns_existing_title_cased_form_without_write(self, mock_plex: PlexClient):
        collection = MagicMock()
        collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        assert mock_plex.stored_label(collection, "shortlist_sarah") == "Shortlist_sarah"
        collection.addLabel.assert_not_called()

    def test_stored_label_keeps_the_labels_already_there(self, mock_plex: PlexClient):
        """Adding the constant `shortlist` label must not cost a row its OWNER label.

        `addLabel` is not additive on the wire: plexapi builds the new tag list as
        `collection.labels + [new]` (mixins/edit.py:294) and PUTs it as an ABSOLUTE set. So what
        protects `Shortlist_sarah` is that it is re-sent from the in-memory list — and a row that
        lost it would match no `label!=shortlist_sarah` exclude and be visible to every shared
        account. This asserts the surviving set, which is the thing that actually matters.
        """
        collection = MagicMock()
        collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        added: list[str] = []

        def add(label):
            added.append(label)
            # What a real PUT does: the union, written back as the whole set.
            collection.labels = [*collection.labels, SimpleNamespace(tag=label.replace("s", "S", 1))]

        collection.addLabel.side_effect = add

        stored = mock_plex.stored_label(collection, "shortlist")

        assert stored == "Shortlist"
        assert added == ["shortlist"]
        assert [t.tag for t in collection.labels] == ["Shortlist_sarah", "Shortlist"], (
            "the owner label must still be on the collection — it is the only thing hiding this row"
        )

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
        from shortlist.engine.clients.plex_pms import has_shortlist_marker
        from shortlist.engine.delivery import has_marker, row_marker

        for title in (
            "✨ Movies Picked for You" + row_marker(202),  # marked → ours
            "Kometa: Best of the 90s",  # foreign → not ours
            "x" + "​" * 63,  # 63 trailing marker chars — one short of a marker
            "x" + "‌" * 65,  # 65 — a valid 64 marker preceded by another zero-width char
        ):
            assert has_shortlist_marker(title) == has_marker(title), title

    def test_delete_demotes_then_deletes_owned(self, mock_plex: PlexClient):
        owned = MagicMock()
        owned.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        mock_plex.delete_owned_collection(owned, "shortlist")
        vis = owned.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": False, "home": False, "shared": False}
        owned.delete.assert_called_once()

    def test_promote_hides_from_library_and_never_defaults_onto_the_owners_home(self, mock_plex: PlexClient):
        """`home` defaults OFF. It is promotedToOwnHome — the SERVER OWNER's Home shelf — and the
        owner has no share filter, so anything that lands there is visible to them with nothing able
        to hide it. Defaulting it on is how every user's row ended up on the owner's Home."""
        collection = MagicMock()
        mock_plex.promote(collection)
        collection.modeUpdate.assert_called_once_with(mode="hide")
        vis = collection.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": True, "home": False, "shared": True}
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

    def test_order_collection_orders_the_whole_row_not_just_the_head(self, mock_plex: PlexClient):
        """The WHOLE row is ordered, tail included. This ordered only the top 15, so a 30-pick row read
        as ranked-then-alphabetical and the ranking looked broken to the person it was built for.

        40 items — the largest a row may be (``row.size`` is validated 5..40) — fully reversed, so every
        position is out of place.

        Replays each ``moveItem(item, after=...)`` against a model of the collection and asserts the
        ORDER that results, because the order is the entire point. Asserting only WHICH items moved
        cannot see it: a SUT degraded to ``after=None`` on every call produces a byte-identical move
        list (39 moves, all of them past the old cap) and leaves the row in exactly the wrong order.
        ``after`` is the one kwarg this function is responsible for, so it is the one to assert
        (tests/testing.md: if removing a parameter wouldn't break the test, it isn't covered).
        """
        size = 40
        live = [self._item(i) for i in range(size, 0, -1)]
        collection = MagicMock()
        collection.items.return_value = live
        wanted = list(range(1, size + 1))

        mock_plex.order_collection(collection, wanted)

        order = [i.ratingKey for i in live]
        for call in collection.moveItem.call_args_list:
            key = call.args[0].ratingKey
            after = call.kwargs.get("after")
            order.remove(key)
            order.insert(0 if after is None else order.index(after.ratingKey) + 1, key)

        assert order == wanted, f"the collection must end up in ranked order, got {order}"
        moved = [c.args[0].ratingKey for c in collection.moveItem.call_args_list]
        assert [k for k in moved if k > 15], "items past the old cap of 15 must move, not sit in the tail"

    def test_sections_by_type_maps_each_media_type_to_its_library(self, mock_plex: PlexClient):
        movies, shows = MagicMock(), MagicMock()
        movies.type, movies.key = "movie", "1"
        shows.type, shows.key = "show", "2"
        mock_plex._server.library.sections.return_value = [movies, shows]

        assert mock_plex.sections_by_type() == {MediaType.MOVIE: movies, MediaType.SHOW: shows}


class TestUserHubs:
    """Fetch hubs AS another user (a canary token) — the visibility-check read."""

    _URL = "http://pms:32400/hubs"

    @respx.mock
    def test_reads_hubs_as_the_canary_user(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, json={"MediaContainer": {"Hub": [{"title": "Home"}]}})
        )

        hubs = mock_plex.user_hubs("CANARY-TOK")

        assert hubs == [{"title": "Home"}]
        request = respx.calls.last.request
        assert request.headers["X-Plex-Token"] == "CANARY-TOK"

    @respx.mock
    def test_a_missing_hub_container_is_an_empty_list_not_an_error(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, json={"MediaContainer": {}}))
        assert mock_plex.user_hubs("CANARY-TOK") == []

    def test_the_configured_timeout_reaches_the_raw_read(self, mock_plex: PlexClient, monkeypatch):
        """This used to hardcode `timeout=30`, ignoring the operator's configured `plex.timeout_s`."""
        from shortlist.engine.clients import plex_pms

        mock_plex._server.url.return_value = self._URL
        mock_plex._timeout = 77
        seen: list[object] = []

        def fake_get(*_args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return httpx.Response(200, json={"MediaContainer": {}}, request=httpx.Request("GET", self._URL))

        monkeypatch.setattr(plex_pms.http_retry, "get", fake_get)
        mock_plex.user_hubs("CANARY-TOK")
        assert seen == [77]


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


class TestWatchedTitles:
    """Parsing one library's watched set, read from the PMS AS a user. The value under test is what a
    real ``/library/sections/{k}/all?unwatched=0&includeGuids=1`` response maps to (recorded shapes),
    and that paging walks the whole set — a silent cap here would re-recommend already-watched titles."""

    _URL = "http://pms:32400/library/sections/1/all"

    def _mock_url(self, mock_plex: PlexClient) -> None:
        # PlexClient builds the read URL via plexapi's server.url(); pin it so respx can intercept the
        # real http_retry.get (includeToken=False keeps the owner token out of the URL — rule 9).
        mock_plex._server.url.return_value = self._URL

    @respx.mock
    def test_maps_a_watched_movie_with_its_inline_tmdb_guid_and_viewcount(self, mock_plex: PlexClient):
        self._mock_url(mock_plex)
        xml = (
            '<MediaContainer size="1" totalSize="1">'
            '<Video ratingKey="42" title="Heat" year="1995" viewCount="3" lastViewedAt="1752000000">'
            '<Guid id="imdb://tt0113277"/><Guid id="tmdb://949"/>'
            "</Video>"
            "</MediaContainer>"
        )
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=xml))

        items = mock_plex.watched_titles("1", MediaType.MOVIE, "SARAH-TOK").items

        assert len(items) == 1
        item = items[0]
        assert (item.title, item.tmdb_id, item.year, item.media_type) == ("Heat", 949, 1995, MediaType.MOVIE)
        assert item.rating_key == 42
        assert item.watch_count == 3  # viewCount — the frequency signal for a movie
        # The per-user token rides in the header, never the URL (rule 9).
        request = respx.calls.last.request
        assert request.headers["X-Plex-Token"] == "SARAH-TOK"
        assert "X-Plex-Token" not in str(request.url)
        assert request.url.params["unwatched"] == "0"  # Plex's binary watched flag: viewCount>0, marks included
        assert request.url.params["type"] == "1"  # movie

    @staticmethod
    def _watched_xml(*rows: tuple[int, str, int]) -> str:
        """`(ratingKey, title, lastViewedAt)` rows, in the order the server would return them."""
        videos = "".join(
            f'<Video ratingKey="{key}" title="{title}" year="2000" viewCount="1" lastViewedAt="{seen}">'
            f'<Guid id="tmdb://{key}"/></Video>'
            for key, title, seen in rows
        )
        return f'<MediaContainer size="{len(rows)}" totalSize="{len(rows)}">{videos}</MediaContainer>'

    @staticmethod
    def _watched_xml_raw(body: str, size: int) -> str:
        return f'<MediaContainer size="{size}" totalSize="{size}">{body}</MediaContainer>'

    @respx.mock
    def test_a_title_with_no_lastViewedAt_does_not_end_the_walk(self, mock_plex: PlexClient):
        """A missing `lastViewedAt` is stamped 1970, so it looks older than any cutoff. Ending the
        walk on it would drop every title BEHIND it and return a truncated history that looks exactly
        like a quiet night — the cache would then advance its cursor past titles it never read."""
        from datetime import UTC, datetime

        self._mock_url(mock_plex)
        # DESCENDING on purpose, so the order guard is satisfied and this test isolates the gap. With
        # ascending data the order guard rescues the read and the test would pass either way.
        recent = '<Video ratingKey="1" title="Recent" year="2000" viewCount="1" lastViewedAt="1785000002"><Guid id="tmdb://1"/></Video>'
        # No lastViewedAt at all — the data gap.
        gap = '<Video ratingKey="2" title="No Timestamp" year="2000" viewCount="1"><Guid id="tmdb://2"/></Video>'
        behind = '<Video ratingKey="3" title="Also Recent" year="2000" viewCount="1" lastViewedAt="1785000001"><Guid id="tmdb://3"/></Video>'
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, text=self._watched_xml_raw(recent + gap + behind, 3))
        )

        items = mock_plex.watched_titles(
            "1", MediaType.MOVIE, token="SARAH-TOK", since=datetime(2026, 7, 1, tzinfo=UTC)
        ).items

        titles = [i.title for i in items]
        assert "Also Recent" in titles, f"the walk stopped on a missing timestamp: {titles}"

    @respx.mock
    def test_an_out_of_order_page_abandons_the_early_stop(self, mock_plex: PlexClient):
        """The early stop is only sound while the server honours `sort=lastViewedAt:desc`.

        `lastViewedAt>=` was also documented as supported and is silently ignored by this PMS, so the
        sort earns the same suspicion: if it stops being honoured, a truncated read would look like a
        quiet night for up to a week (until the next full read).
        """
        from datetime import UTC, datetime

        self._mock_url(mock_plex)
        # Ascending — the opposite of what the sort promises.
        respx.get(self._URL).mock(
            return_value=httpx.Response(
                200,
                text=self._watched_xml((1, "Older", 1784000000), (2, "Newer", 1785000000), (3, "Newest", 1785000002)),
            )
        )

        items = mock_plex.watched_titles(
            "1", MediaType.MOVIE, token="SARAH-TOK", since=datetime(2026, 7, 20, tzinfo=UTC)
        ).items

        # "Older" is before the cutoff and legitimately dropped; the point is the walk did not STOP
        # there and still returned the two newer titles behind it.
        assert {i.title for i in items} >= {"Newer", "Newest"}, [i.title for i in items]

    @respx.mock
    def test_an_incremental_read_works_for_SHOWS_not_just_movies(self, mock_plex: PlexClient):
        """`media_type` is a branch variable with two shapes — `<Video>` vs `<Directory>`, type=1 vs
        type=2 — and every other incremental test covers only movies. Shows are also where
        `lastViewedAt` is most likely to be absent or populated differently."""
        from datetime import UTC, datetime

        self._mock_url(mock_plex)
        shows = (
            '<Directory ratingKey="10" title="Recent Show" year="2020" viewedLeafCount="3" '
            'leafCount="10" lastViewedAt="1785000000"><Guid id="tmdb://10"/></Directory>'
            '<Directory ratingKey="11" title="Old Show" year="2001" viewedLeafCount="1" '
            'leafCount="10" lastViewedAt="1700000000"><Guid id="tmdb://11"/></Directory>'
        )
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._watched_xml_raw(shows, 2)))

        items = mock_plex.watched_titles(
            "2", MediaType.SHOW, token="SARAH-TOK", since=datetime(2026, 7, 1, tzinfo=UTC)
        ).items

        assert [i.title for i in items] == ["Recent Show"], "the cutoff must work for shows too"
        assert items[0].media_type is MediaType.SHOW
        assert items[0].viewed_leaf_count == 3 and items[0].leaf_count == 10
        assert respx.calls.last.request.url.params["type"] == "2"

    @respx.mock
    def test_an_incremental_read_sorts_newest_first_and_never_sends_a_filter(self, mock_plex: PlexClient):
        """The saving comes from ORDERING plus an early stop, not from a server-side filter.

        `lastViewedAt>=` (and `>>=`) are SILENTLY IGNORED by PMS 1.43.3 — live-probed 2026-07-30
        against a real server: unfiltered, `>=` and `>>=` all returned the same totalSize of 1077, as
        did a `year>>=` control. Ignoring a filter is the worst failure mode available, because the
        read looks like it worked and quietly returns everything. Sorting IS honoured, so that is what
        we rely on; sending the dead filter anyway would be cargo cult.
        """
        from datetime import UTC, datetime

        self._mock_url(mock_plex)
        # 1785000000 = 2026-07-25, i.e. INSIDE the cutoff below, so it survives the early stop.
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._watched_xml((42, "Heat", 1785000000))))
        since = datetime(2026, 7, 1, tzinfo=UTC)

        items = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK", since=since).items

        assert [i.title for i in items] == ["Heat"]
        params = respx.calls.last.request.url.params
        assert params["sort"] == "lastViewedAt:desc"
        assert "lastViewedAt>=" not in params, "a filter this PMS ignores must not be sent"
        # The filters that DO work still apply — incremental narrows the read, it does not replace it.
        assert params["unwatched"] == "0" and params["includeGuids"] == "1"

    @respx.mock
    def test_an_incremental_read_stops_at_the_first_title_older_than_the_cutoff(self, mock_plex: PlexClient):
        """This early stop IS the optimisation. Without it the incremental path reads every watched
        title and throws most away — all of the cost, none of the benefit."""
        from datetime import UTC, datetime

        self._mock_url(mock_plex)
        # Newest-first, as the sort guarantees. Only the first two are inside the cutoff.
        respx.get(self._URL).mock(
            return_value=httpx.Response(
                200,
                text=self._watched_xml(
                    (1, "Watched today", 1785000000),
                    (2, "Watched yesterday", 1784900000),
                    (3, "Watched years ago", 1500000000),
                    (4, "Older still", 1400000000),
                ),
            )
        )
        since = datetime.fromtimestamp(1784000000, tz=UTC)

        items = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK", since=since).items

        assert [i.title for i in items] == ["Watched today", "Watched yesterday"]

    @respx.mock
    def test_it_parses_the_recorded_sorted_response_from_a_real_server(self, mock_plex: PlexClient):
        """Replays the recorded PMS 1.43.3 response (plex-safety rule 11).

        The header of `pms_watched_incremental.xml.txt` carries the measurements that decided this
        design: on a real 9,897-item section, `unwatched=0` and `sort=lastViewedAt:desc` are honoured
        while every cutoff-filter form is silently ignored. This test pins the PARSE against that
        exact shape — the ordering, the mark-as-watched with no viewCount, the multiple `<Guid>`
        children — so a future refactor cannot quietly stop understanding it.
        """
        from datetime import UTC, datetime

        self._mock_url(mock_plex)
        recorded = (FIXTURES / "pms_watched_incremental.xml.txt").read_text()
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=recorded))

        # A cutoff older than all three, so nothing is stopped early and the whole page is parsed.
        items = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK", since=datetime(2025, 1, 1, tzinfo=UTC)).items

        assert [i.tmdb_id for i in items] == [100001, 100002, 100003]
        # Newest first, exactly as the recorded response is ordered.
        assert [i.watched_at.timestamp() for i in items] == [1779572385, 1774305861, 1765677185]
        assert items[1].watch_count == 3  # viewCount, the frequency signal for a movie
        assert items[2].watch_count == 1  # a mark-as-watched carries none; it floors at 1

    @respx.mock
    def test_a_complete_read_asks_for_everything_unsorted(self, mock_plex: PlexClient):
        """A full read must not narrow OR reorder: it is the only thing that notices an un-watch, and
        the already-watched filter depends on it being the whole set."""
        self._mock_url(mock_plex)
        respx.get(self._URL).mock(return_value=httpx.Response(200, text='<MediaContainer size="0" totalSize="0"/>'))

        mock_plex.watched_titles("1", MediaType.MOVIE, "TOK")

        params = respx.calls.last.request.url.params
        assert "lastViewedAt>=" not in params
        assert "sort" not in params

    @respx.mock
    def test_a_marked_movie_with_no_playback_still_counts_once(self, mock_plex: PlexClient):
        # A mark-as-watched: unwatched=0 returns it (the whole point — the history API never would),
        # but it carries no viewCount. watch_count floors at 1 so it still weighs as one watch.
        self._mock_url(mock_plex)
        xml = (
            '<MediaContainer size="1" totalSize="1">'
            '<Video ratingKey="7" title="Marked" year="2020"><Guid id="tmdb://500"/></Video>'
            "</MediaContainer>"
        )
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=xml))

        items = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK").items
        assert items[0].watch_count == 1

    @respx.mock
    def test_maps_a_show_with_plex_own_viewed_leaf_counts(self, mock_plex: PlexClient):
        # A show comes back once, at the show level, carrying Plex's OWN viewedLeafCount/leafCount —
        # so "finished" is Plex's fraction, not a reconstruction, and a bulk-marked season counts.
        self._mock_url(mock_plex)
        xml = (
            '<MediaContainer size="1" totalSize="1">'
            '<Directory ratingKey="55" title="Suits" year="2011" viewedLeafCount="30" leafCount="134" '
            'lastViewedAt="1752000000"><Guid id="tmdb://37680"/></Directory>'
            "</MediaContainer>"
        )
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=xml))

        items = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items

        item = items[0]
        assert (item.title, item.tmdb_id, item.media_type) == ("Suits", 37680, MediaType.SHOW)
        assert (item.viewed_leaf_count, item.leaf_count) == (30, 134)
        assert item.watch_count == 30  # episodes watched drives a show's frequency weight
        params = respx.calls.last.request.url.params
        assert params["type"] == "2"  # show
        # `viewedLeafCount!=0`, never `unwatched=0` — see issue #108.
        assert params["viewedLeafCount!"] == "0"
        assert "unwatched" not in params

    @respx.mock
    def test_a_title_with_no_tmdb_guid_is_dropped(self, mock_plex: PlexClient):
        # No tmdb:// GUID means it can never match a candidate, so it's dropped rather than kept as a
        # useless, unmatchable entry.
        self._mock_url(mock_plex)
        xml = (
            '<MediaContainer size="1" totalSize="1">'
            '<Video ratingKey="9" title="No Guid" viewCount="1"><Guid id="imdb://tt99"/></Video>'
            "</MediaContainer>"
        )
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=xml))
        assert mock_plex.watched_titles("1", MediaType.MOVIE, "TOK").items == []

    @respx.mock
    def test_a_403_raises_section_not_shared_not_a_generic_http_error(self, mock_plex: PlexClient):
        """403 = this token cannot see this library, which callers must tell apart from a real error.

        As a plain `HTTPStatusError` it read as "unreadable section", which invalidated the person's
        entire watch cache on every sync and forced an uncached complete re-read for ever.
        """
        from shortlist.engine.clients.plex_pms import SectionNotShared

        self._mock_url(mock_plex)
        respx.get(self._URL).mock(return_value=httpx.Response(403, text="<html>Forbidden</html>"))
        with pytest.raises(SectionNotShared):
            mock_plex.watched_titles("12", MediaType.SHOW, "TOK")

    @respx.mock
    def test_a_500_still_raises_a_generic_http_error(self, mock_plex: PlexClient):
        """The other side of the 403 split: a real server fault must STAY a failure, so the cache
        still invalidates and the complete-read fallback still fires."""
        from shortlist.engine.clients.plex_pms import SectionNotShared

        self._mock_url(mock_plex)
        respx.get(self._URL).mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            mock_plex.watched_titles("12", MediaType.SHOW, "TOK")
        assert not isinstance(excinfo.value, SectionNotShared)

    @respx.mock
    def test_a_malformed_tmdb_guid_is_dropped_not_raised(self, mock_plex: PlexClient):
        """A guid id that isn't a real integer must be treated like no guid at all — dropped, not a
        crash that ends the whole watched-titles read for this user (see the same tolerance in
        `build_library_index`/`_tmdb_guid`)."""
        self._mock_url(mock_plex)
        xml = (
            '<MediaContainer size="2" totalSize="2">'
            '<Video ratingKey="9" title="Malformed" viewCount="1"><Guid id="tmdb://not-a-number"/></Video>'
            '<Video ratingKey="10" title="Good" viewCount="1"><Guid id="tmdb://949"/></Video>'
            "</MediaContainer>"
        )
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=xml))
        items = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK").items
        assert [i.title for i in items] == ["Good"]

    def test_the_configured_timeout_reaches_the_raw_watched_read(self, mock_plex: PlexClient, monkeypatch):
        """`_read_watched_page` used to hardcode `timeout=45`, ignoring the operator's configured
        `plex.timeout_s` on the heaviest raw PMS read in the file."""
        from shortlist.engine.clients import plex_pms

        self._mock_url(mock_plex)
        mock_plex._timeout = 99
        seen: list[object] = []

        def fake_get(*_args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return httpx.Response(200, text=self._watched_xml(), request=httpx.Request("GET", self._URL))

        monkeypatch.setattr(plex_pms.http_retry, "get", fake_get)
        mock_plex.watched_titles("1", MediaType.MOVIE, "TOK")
        assert seen == [99]

    @respx.mock
    def test_pages_until_the_reported_total_is_reached(self, mock_plex: PlexClient):
        # A heavy watcher has thousands of watched titles; the read must page past the first response
        # or a silent cap would hide older watches from the already-watched filter (the 200-row bug).
        self._mock_url(mock_plex)

        def page(request: httpx.Request) -> httpx.Response:
            # One title per page, totalSize=2, so the loop MUST issue a second request to reach both —
            # and must then STOP (start >= total). Every page returns a title, so an over-read would
            # surface a third tmdb id, not be masked by an empty page.
            rk = int(request.headers["X-Plex-Container-Start"]) + 1
            return httpx.Response(
                200,
                text=(
                    f'<MediaContainer size="1" totalSize="2">'
                    f'<Video ratingKey="{rk}" title="Movie {rk}" viewCount="1"><Guid id="tmdb://{rk}"/></Video>'
                    f"</MediaContainer>"
                ),
            )

        respx.get(self._URL).mock(side_effect=page)
        items = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK").items

        assert {i.tmdb_id for i in items} == {1, 2}
        assert len(respx.calls) == 2  # paged: first page short of total, second fetched the rest, then stop


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
        # Named as the likely cause, because Trakt made API keys VIP-only and a lapsed subscription
        # takes a working key down with it — "rejected" alone sent a reporter checking a good key.
        assert "VIP" in str(excinfo.value)

    @respx.mock
    def test_trakt_saying_not_vip_is_reported_as_that_and_not_as_a_bad_key(self):
        """Issue #73. 426 is Trakt's own "you are not a VIP" signal (docs.trakt.tv/docs/vip-methods),
        and it is the ONLY authoritative answer available to us: the endpoint that reports VIP status
        needs an OAuth user token, and Shortlist is deliberately client-id-only. Reporting it as a bad
        key would send someone to regenerate a key that is perfectly valid."""
        respx.get("https://api.trakt.tv/movies/trending").mock(return_value=httpx.Response(426))
        with pytest.raises(TraktError) as excinfo:
            TraktClient("secret-cid").ping()
        assert "426" in str(excinfo.value)
        assert "VIP" in str(excinfo.value)
        assert "rejected the API key" not in str(excinfo.value)
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


class TestWatchedPagingWithoutTotalSize:
    """`size` on a paged Plex response is the PAGE size, not the library total."""

    _URL = "http://plex.local:32400/library/sections/1/all"

    def _mock_url(self, mock_plex: PlexClient) -> None:
        mock_plex._server.url.return_value = self._URL

    @staticmethod
    def _page(start: int, count: int, *, total_size: bool) -> str:
        videos = "".join(
            f'<Video ratingKey="{start + i}" title="T{start + i}" year="2000" viewCount="1" '
            f'lastViewedAt="{1785000000 - start - i}"><Guid id="tmdb://{start + i}"/></Video>'
            for i in range(count)
        )
        attrs = f'size="{count}"'
        if total_size:
            attrs += ' totalSize="1200"'
        return f"<MediaContainer {attrs}>{videos}</MediaContainer>"

    @respx.mock
    def test_a_response_without_totalSize_still_reads_every_page(self, mock_plex: PlexClient):
        """Falling back to `size` made the total equal the page length, so the walk stopped after one
        page and returned 500 of 1200 titles with no warning — a partial watched set reported as
        complete, which is how already-watched titles get recommended."""
        self._mock_url(mock_plex)
        pages = [
            self._page(0, 500, total_size=False),
            self._page(500, 500, total_size=False),
            self._page(1000, 200, total_size=False),  # short page = the end
        ]
        calls = {"n": 0}

        def respond(request):
            body = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return httpx.Response(200, text=body)

        respx.get(self._URL).mock(side_effect=respond)

        items = mock_plex.watched_titles("1", MediaType.MOVIE, token="TOK").items

        assert len(items) == 1200, f"read stopped early: {len(items)} of 1200"


class TestWatchedWindowCoverage:
    """The contract between the PMS walk and the watched-title CACHE, tested across the seam.

    The cache deletes cached titles an incremental read did not return, which is only safe while the
    read really did return everything at or after the cutoff. `watched_titles` has three ways to stop
    and only two of them prove that, so it says so explicitly via `WatchedRead.covers_window` rather
    than leaving the caller to assume it.

    Tested here rather than in `test_watch_cache.py` because both halves have to be REAL: the cache's
    own tests mock the reader, and a mocked reader is free to implement the very assumption under
    test. This drives the real client over real HTTP into the real cache.
    """

    _URL = "http://plex.local:32400/library/sections/1/all"
    _NOW = 1785000000

    def _mock_url(self, mock_plex: PlexClient) -> None:
        mock_plex._server.url.return_value = self._URL

    def _page(self, rows: list[tuple[int, str, int]], *, size: int, total: int | None, ascending: bool = False) -> str:
        ordered = sorted(rows, key=lambda r: r[2], reverse=not ascending)
        videos = "".join(
            f'<Video ratingKey="{key}" title="{title}" year="2000" viewCount="1" lastViewedAt="{seen}">'
            f'<Guid id="tmdb://{key}"/></Video>'
            for key, title, seen in ordered
        )
        attrs = f'size="{size}"'
        if total is not None:
            attrs += f' totalSize="{total}"'
        return f"<MediaContainer {attrs}>{videos}</MediaContainer>"

    def _read(self, mock_plex: PlexClient, body: str, *, since_ago: int = 100000):
        from datetime import UTC, datetime

        self._mock_url(mock_plex)
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=body))
        since = datetime.fromtimestamp(self._NOW - since_ago, tz=UTC)
        return mock_plex.watched_titles("1", MediaType.MOVIE, "TOK", since=since)

    @respx.mock
    def test_coverage_is_claimed_when_the_walk_saw_a_title_older_than_the_cutoff(self, mock_plex: PlexClient):
        """The healthy case: stopping ON a real older timestamp proves everything newer was emitted.

        `total=99` on purpose, well above the three rows returned, so `read_whole_library` stays False
        and `reached_cutoff` is the SOLE prover. With a matching total both provers fire and this test
        passes even if `reached_cutoff` is deleted from the expression — while the real-server shape
        (a 1077-title library where the walk stops on page 1 and never reaches the total) would
        silently stop claiming coverage, disabling un-watch detection everywhere with a green suite.
        """
        rows = [(1, "Today", self._NOW), (2, "Yesterday", self._NOW - 1000), (3, "Ages ago", self._NOW - 9_000_000)]

        read = self._read(mock_plex, self._page(rows, size=3, total=99))

        assert [i.title for i in read.items] == ["Today", "Yesterday"]
        assert read.covers_window is True

    @respx.mock
    def test_coverage_is_claimed_when_the_walk_reached_the_reported_total(self, mock_plex: PlexClient):
        """No title old enough to trip the cutoff, but the server said how many there were and we
        read them all — so there was nothing left to miss."""
        rows = [(1, "Today", self._NOW), (2, "Yesterday", self._NOW - 1000)]

        read = self._read(mock_plex, self._page(rows, size=2, total=2))

        assert [i.title for i in read.items] == ["Today", "Yesterday"]
        assert read.covers_window is True

    @respx.mock
    def test_coverage_is_REFUSED_when_a_short_page_ends_a_walk_with_no_total(self, mock_plex: PlexClient):
        """The bug this flag exists for. A server that omits `totalSize` AND caps the container ends
        the walk on a short page having read only part of the window — indistinguishable, from the
        cache's side, from the user un-watching everything it did not send."""
        rows = [(1, "Today", self._NOW), (2, "Yesterday", self._NOW - 1000)]

        read = self._read(mock_plex, self._page(rows, size=2, total=None))

        assert [i.title for i in read.items] == ["Today", "Yesterday"]
        assert read.covers_window is False, "a short page with no total proves nothing about the window"

    @respx.mock
    def test_coverage_is_REFUSED_when_the_sort_was_not_honoured(self, mock_plex: PlexClient):
        """The fallback abandons the sort MID-WALK; pages already read keep whatever order they came
        in, so one failure taints the whole read rather than just the page that failed."""
        rows = [(1, "Oldest", self._NOW - 9_000_000), (2, "Middle", self._NOW - 1000), (3, "Newest", self._NOW)]

        read = self._read(mock_plex, self._page(rows, size=3, total=3, ascending=True))

        assert read.covers_window is False

    @respx.mock
    def test_coverage_is_REFUSED_when_the_order_was_never_actually_observed(self, mock_plex: PlexClient):
        """A page with one comparable stamp passes the order check without demonstrating anything —
        `all(pairwise([x]))` is vacuously true. Paired with a capped container and no `totalSize`
        (the same server shape behind the original bug), the cutoff stop would otherwise claim
        coverage on the strength of a sort nobody ever saw working."""
        one_old_stamp = [(1, "Ages ago", self._NOW - 9_000_000)]

        read = self._read(mock_plex, self._page(one_old_stamp, size=1, total=None))

        assert read.items == []
        assert read.covers_window is False

    @respx.mock
    def test_a_complete_read_claims_coverage_when_it_reached_the_servers_own_total(self, mock_plex: PlexClient):
        """A complete read's window is the whole library, and reaching `totalSize` proves it saw it.

        This is what lets the cache replace the section. It used to be hardcoded False, so the
        DESTRUCTIVE path — delete the section, reinsert what came back — ran on no proof at all.
        """
        self._mock_url(mock_plex)
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, text=self._page([(1, "Heat", self._NOW)], size=1, total=1))
        )

        read = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK")

        assert [i.title for i in read.items] == ["Heat"]
        assert read.covers_window is True

    @respx.mock
    def test_a_complete_read_REFUSES_coverage_when_the_server_reported_no_total(self, mock_plex: PlexClient):
        """The dangerous shape: a server that omits `totalSize` and caps the container answers a
        SHORT page with a 200. Indistinguishable from a small library — so the walk cannot prove it
        saw everything, and must not let the cache delete what it did not read."""
        self._mock_url(mock_plex)
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, text=self._page([(1, "Heat", self._NOW)], size=1, total=None))
        )

        read = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK")

        assert [i.title for i in read.items] == ["Heat"]
        assert read.covers_window is False

    @respx.mock
    def test_a_truncated_walk_does_not_delete_the_cache_it_could_not_read(self, mock_plex, tmp_path):
        """End to end, both halves real: the exact server shape above, driven into `WatchCache`.

        Before `covers_window` existed this deleted `Older` — a title nobody un-watched — because the
        walk simply never reached it. That is the failure the flag prevents, and it is invisible to
        any test that mocks the reader.
        """
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import User, WatchedTitle
        from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
        from shortlist.server.services.watch_cache import WatchCache

        run_migrations(tmp_path)
        sessions = make_session_factory(make_engine(tmp_path))
        with sessions() as session:
            user = User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True)
            session.add(user)
            session.commit()
            user_id = user.id

        cache = WatchCache(sessions)
        self._mock_url(mock_plex)
        # 100s apart, well inside CURSOR_OVERLAP (5 min) — so `Older` really is in the window the
        # next read covers, and is therefore a genuine deletion candidate. Space them further and the
        # cursor moves past `Older`, the delete can never reach it, and this test proves nothing.
        everything = [(1, "Newest", self._NOW), (2, "Older", self._NOW - 100)]
        person = SimpleNamespace(username="sarah", slug="sarah")

        # Seed: a healthy full read caches both titles.
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._page(everything, size=2, total=2)))
        with sessions() as session:
            cache.sync_section(
                session,
                person,
                user_id,
                "1",
                MediaType.MOVIE,
                lambda since: mock_plex.watched_titles("1", MediaType.MOVIE, "TOK", since=since),
                force_full=True,
            )
            session.commit()
        with sessions() as session:
            assert {r.title for r in session.query(WatchedTitle).all()} == {"Newest", "Older"}

        # Now the server truncates: one capped page, no totalSize. `Older` is inside the window the
        # cursor asks for, but the walk stops before reaching it.
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._page(everything[:1], size=1, total=None)))
        with sessions() as session:
            cache.sync_section(
                session,
                person,
                user_id,
                "1",
                MediaType.MOVIE,
                lambda since: mock_plex.watched_titles("1", MediaType.MOVIE, "TOK", since=since),
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            session.commit()

        with sessions() as session:
            titles = {r.title for r in session.query(WatchedTitle).all()}
        assert titles == {"Newest", "Older"}, "a title the walk never reached was deleted as an un-watch"

    @respx.mock
    def test_a_truncated_COMPLETE_read_does_not_wipe_the_section(self, mock_plex, tmp_path):
        """The twin of the test above, on the more destructive path.

        A complete read DELETES the section and reinserts what came back, so a short page answered
        with a 200 — the shape a server that omits `totalSize` and caps the container produces — used
        to erase every title behind it and stamp the sync a success. Now the delete waits for proof,
        and an unproven read tops up instead.
        """
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import User, WatchedTitle, WatchSyncState
        from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
        from shortlist.server.services.watch_cache import WatchCache

        run_migrations(tmp_path)
        sessions = make_session_factory(make_engine(tmp_path))
        with sessions() as session:
            user = User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True)
            session.add(user)
            session.commit()
            user_id = user.id

        cache = WatchCache(sessions)
        person = SimpleNamespace(username="sarah", slug="sarah")
        everything = [(1, "Newest", self._NOW), (2, "Older", self._NOW - 100)]
        self._mock_url(mock_plex)

        def complete_read(now=None):
            with sessions() as session:
                cache.sync_section(
                    session,
                    person,
                    user_id,
                    "1",
                    MediaType.MOVIE,
                    lambda since: mock_plex.watched_titles("1", MediaType.MOVIE, "TOK", since=since),
                    force_full=True,
                    now=now,
                )
                session.commit()

        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._page(everything, size=2, total=2)))
        complete_read()
        with sessions() as session:
            assert {r.title for r in session.query(WatchedTitle).all()} == {"Newest", "Older"}
            stamped = session.query(WatchSyncState).one().last_full_at

        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._page(everything[:1], size=1, total=None)))
        complete_read(now=datetime.now(UTC) + timedelta(seconds=1))

        with sessions() as session:
            assert {r.title for r in session.query(WatchedTitle).all()} == {"Newest", "Older"}, (
                "an unproven complete read wiped a title nobody un-watched"
            )
            assert session.query(WatchSyncState).one().last_full_at == stamped, (
                "an unproven complete read reset the clock on the reconcile it never did"
            )

    @respx.mock
    def test_an_incremental_read_LOSES_a_series_whose_show_date_lagged_its_episodes(self, mock_plex):
        """Issue #108, at the seam that causes it — the reason the sync now always reads complete.

        A show's own `lastViewedAt` can be OLDER than the episodes it counts (measured on a live
        server: 2 of the 25 most recent). Marking a series watched changes every episode; if the show
        row's date does not move with them, the row sorts behind the cursor, the walk stops at the
        cutoff, and the finished series is never returned. It then stayed invisible until the weekly
        complete read. Movies cannot drift this way — there is no second level — which is exactly
        what the reporter saw.
        """
        self._mock_url(mock_plex)
        # `Just Finished` is 20/20 watched but still carries last month's date, because its episodes
        # moved and it did not. `Watched Normally` is newer, so the cursor sits past the stale row.
        stale = (
            f'<Directory ratingKey="5002" type="show" title="Just Finished" year="2021" '
            f'leafCount="20" viewedLeafCount="20" lastViewedAt="{self._NOW - 2_600_000}">'
            '<Guid id="tmdb://222"/></Directory>'
        )
        recent = (
            f'<Directory ratingKey="5001" type="show" title="Watched Normally" year="2020" '
            f'leafCount="10" viewedLeafCount="4" lastViewedAt="{self._NOW}">'
            '<Guid id="tmdb://111"/></Directory>'
        )
        body = f'<MediaContainer size="2" totalSize="2">{recent}{stale}</MediaContainer>'
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=body))

        complete = mock_plex.watched_titles("2", MediaType.SHOW, "TOK")
        incremental = self._read(mock_plex, body, since_ago=1000)

        assert {i.tmdb_id for i in complete.items} == {111, 222}, "a complete read sees the finished series"
        assert 222 not in {i.tmdb_id for i in incremental.items}, "an incremental read stops short of it"

    @respx.mock
    def test_an_incremental_read_RETURNS_a_show_with_no_date_at_all(self, mock_plex):
        """The same failure from the other direction, kept because the code guards it explicitly.

        A row with no `lastViewedAt` is dated 1970 by `_watched_item`, so it can never clear a cutoff.
        Two things follow, and they are separate. It must not END the walk — a data gap behind which
        everything is silently dropped reads exactly like a quiet night. And it must still be
        RETURNED: the full read dates such a show from its newest watched episode and caches that
        recent date, so a later incremental read that omitted the row would put it inside
        `_drop_vanished_since`'s window and absent from the answer, which is the definition of an
        un-watch. The cache would delete a series the person had just marked watched — #108 again,
        by a different route. `viewedLeafCount!=0` already proved it watched; there is nothing to
        weigh up.
        """
        self._mock_url(mock_plex)
        undated = (
            '<Directory ratingKey="5002" type="show" title="No Date" year="2021" '
            'leafCount="20" viewedLeafCount="20"><Guid id="tmdb://222"/></Directory>'
        )
        recent = (
            f'<Directory ratingKey="5001" type="show" title="Watched Normally" year="2020" '
            f'leafCount="10" viewedLeafCount="4" lastViewedAt="{self._NOW}">'
            '<Guid id="tmdb://111"/></Directory>'
        )
        body = f'<MediaContainer size="2" totalSize="2">{recent}{undated}</MediaContainer>'
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=body))

        complete = mock_plex.watched_titles("2", MediaType.SHOW, "TOK")
        incremental = self._read(mock_plex, body, since_ago=1000)

        assert {i.tmdb_id for i in complete.items} == {111, 222}
        assert 222 in {i.tmdb_id for i in incremental.items}, (
            "the undated show was dropped — a later reconcile reads that as an un-watch and deletes it"
        )
        assert incremental.covers_window is True, "the gap must not make the walk claim a truncated read"

    @respx.mock
    def test_a_watched_title_with_no_tmdb_guid_is_COUNTED_not_silently_dropped(self, mock_plex):
        """A title Plex returns that carries no `tmdb://` guid can never be matched, so it is skipped
        — and until now that happened in total silence.

        It is the failure mode with no symptom: the person really has watched the thing, Shortlist
        goes on recommending it back to them, and the log reads "1 titles" rather than "1 of 2". A
        library matched with the legacy TheTVDB agent yields `tvdb://` for EVERY title, so this is a
        whole library disappearing, not a stray row. Reported on issue #108 by someone whose TV shows
        never appeared while their movies did.
        """
        self._mock_url(mock_plex)
        matched = (
            f'<Directory ratingKey="1" type="show" title="Matched" year="2020" leafCount="4" '
            f'viewedLeafCount="4" lastViewedAt="{self._NOW}"><Guid id="tmdb://111"/></Directory>'
        )
        tvdb_only = (
            f'<Directory ratingKey="2" type="show" title="TVDB Only" year="2019" leafCount="6" '
            f'viewedLeafCount="6" lastViewedAt="{self._NOW - 500}"><Guid id="tvdb://999"/></Directory>'
        )
        respx.get(self._URL).mock(
            return_value=httpx.Response(
                200, text=f'<MediaContainer size="2" totalSize="2">{matched}{tvdb_only}</MediaContainer>'
            )
        )

        read = mock_plex.watched_titles("2", MediaType.SHOW, "TOK")

        assert [i.tmdb_id for i in read.items] == [111]
        assert read.dropped_no_guid == 1, "the unmatched title was dropped without being counted"

    @respx.mock
    def test_a_healthy_library_reports_no_drops(self, mock_plex):
        """So the count means something when it is non-zero."""
        self._mock_url(mock_plex)
        row = (
            f'<Directory ratingKey="1" type="show" title="Matched" year="2020" leafCount="4" '
            f'viewedLeafCount="4" lastViewedAt="{self._NOW}"><Guid id="tmdb://111"/></Directory>'
        )
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, text=f'<MediaContainer size="1" totalSize="1">{row}</MediaContainer>')
        )

        assert mock_plex.watched_titles("2", MediaType.SHOW, "TOK").dropped_no_guid == 0

    @respx.mock
    def test_the_show_read_asks_for_viewedLeafCount_not_unwatched(self, mock_plex):
        """Issue #108, at the seam that causes it.

        `unwatched=0` filters on the SHOW's own watch-state row, which marking a series or a season
        never establishes — so a series someone has finished is absent from that read while its
        episode counts are perfectly correct. Measured on two independent servers: `unwatched=0`
        returned 533 shows where `viewedLeafCount!=0` returned 491, matching the episode-level truth
        exactly in both directions, with 20 shows missing from `unwatched=0` altogether.

        A MOVIE library still uses `unwatched=0` — a film has no episodes beneath it, so there is no
        second record to go missing, which is exactly why movies were never affected.
        """
        self._mock_url(mock_plex)
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._page([], size=0, total=0)))

        mock_plex.watched_titles("2", MediaType.SHOW, "TOK")
        show_params = respx.calls.last.request.url.params
        mock_plex.watched_titles("1", MediaType.MOVIE, "TOK")
        movie_params = respx.calls.last.request.url.params

        assert show_params["viewedLeafCount!"] == "0" and "unwatched" not in show_params
        assert movie_params["unwatched"] == "0" and "viewedLeafCount!" not in movie_params

    @respx.mock
    def test_the_show_filter_reaches_the_wire_UNENCODED(self, mock_plex):
        """Plex's filter OPERATOR lives in the key, and httpx percent-encodes keys.

        `params={"viewedLeafCount!": 0}` goes out as `viewedLeafCount%21=0`. The maintainer's server
        decodes that and answers identically (measured, 491 either way), but plexapi's own `joinArgs`
        encodes only the VALUE for exactly this reason, and a server that did not decode it would
        ignore the filter and return the WHOLE library — 4,880 rows against 491, per person, per
        library, per sync, silently.

        Asserts the RAW query, not `url.params`: that view percent-DECODES, so it reports
        `viewedLeafCount%21=0` as `{"viewedLeafCount!": "0"}` and cannot tell the two apart. Every
        other test here, and the fake, are blind to this for the same reason.
        """
        self._mock_url(mock_plex)
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._page([], size=0, total=0)))

        mock_plex.watched_titles("2", MediaType.SHOW, "TOK")

        raw = respx.calls.last.request.url.query.decode()
        assert "viewedLeafCount!=0" in raw, f"the filter was mangled on the wire: {raw}"
        assert "%21" not in raw

    @respx.mock
    def test_a_server_that_IGNORES_the_filter_still_gives_the_right_answer(self, mock_plex):
        """The hazard this endpoint is known for: a query param silently ignored, answered with a 200
        carrying the FULL library. Here that is the worst failure available — every show in the
        library would read as watched and nothing would ever be recommended again.

        So the filter is applied client-side as well. An honoured filter just means fewer rows crossed
        the wire; an ignored one costs bandwidth and nothing else.
        """
        watched = (
            f'<Directory ratingKey="5001" type="show" title="Watched" year="2020" leafCount="10" '
            f'viewedLeafCount="4" lastViewedAt="{self._NOW}"><Guid id="tmdb://111"/></Directory>'
        )
        never_touched = (
            '<Directory ratingKey="5002" type="show" title="Never Touched" year="2019" '
            'leafCount="8" viewedLeafCount="0"><Guid id="tmdb://222"/></Directory>'
        )
        no_attribute_at_all = (
            '<Directory ratingKey="5003" type="show" title="No Counts" year="2018" leafCount="6">'
            '<Guid id="tmdb://333"/></Directory>'
        )
        self._mock_url(mock_plex)
        body = watched + never_touched + no_attribute_at_all
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, text=f'<MediaContainer size="3" totalSize="3">{body}</MediaContainer>')
        )

        items = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items

        assert [i.tmdb_id for i in items] == [111], "a show with no watched episodes was counted as watched"

    @respx.mock
    def test_a_series_marked_watched_comes_back_even_with_no_show_level_stamp(self, mock_plex):
        """The reporter's exact case, as the server actually reports it: episode counts complete,
        no `lastViewedAt` on the show at all. Verified live — MooHouse/Rabbit Hole read
        `viewedLeafCount=8 leafCount=8 lastViewedAt=None` and was absent from `unwatched=0`."""
        marked = (
            '<Directory ratingKey="5001" type="show" title="Rabbit Hole" year="2023" '
            'leafCount="8" viewedLeafCount="8"><Guid id="tmdb://156819"/></Directory>'
        )
        self._mock_url(mock_plex)
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, text=f'<MediaContainer size="1" totalSize="1">{marked}</MediaContainer>')
        )

        item = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items[0]

        assert (item.title, item.tmdb_id) == ("Rabbit Hole", 156819)
        assert (item.viewed_leaf_count, item.leaf_count) == (8, 8)
        assert item.watch_count == 8

    @respx.mock
    def test_a_show_with_no_watch_date_takes_its_newest_EPISODE_date(self, mock_plex):
        """Marking a series watched sets the episodes and leaves the show with no `lastViewedAt`, so
        `_watched_item` has to date it 1970 — and that is not a cosmetic wrong.

        `watched_at` drives seed recency (a 1970 date weighs zero, so the show never seeds again) and
        the effectiveness report, which showed a series finished minutes ago as "finished 20697d
        ago". Reported on #108 after the episode roll-up was removed.
        """
        marked = (
            '<Directory ratingKey="5001" type="show" title="Just Marked" year="2023" '
            'leafCount="8" viewedLeafCount="8"><Guid id="tmdb://111"/></Directory>'
        )
        eps = "".join(
            f'<Video ratingKey="{900 + n}" type="episode" title="Ep{n}" viewCount="1" '
            f'grandparentRatingKey="5001" lastViewedAt="{self._NOW - n * 60}"/>'
            for n in range(3)
        )
        self._mock_url(mock_plex)

        def answer(request):
            body, size = (eps, 3) if request.url.params.get("type") == "4" else (marked, 1)
            return httpx.Response(200, text=f'<MediaContainer size="{size}" totalSize="{size}">{body}</MediaContainer>')

        respx.get(self._URL).mock(side_effect=answer)

        item = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items[0]

        assert int(item.watched_at.timestamp()) == self._NOW, "the show kept the epoch instead of its episode date"

    @respx.mock
    def test_a_show_that_ALREADY_has_a_date_costs_no_episode_read(self, mock_plex):
        """The episode read is a repair, not a routine second call. A library whose shows all carry a
        date must not pay for it — on a real server that is 472 of 491 shows."""
        dated = (
            f'<Directory ratingKey="5001" type="show" title="Watched Normally" year="2020" '
            f'leafCount="8" viewedLeafCount="8" lastViewedAt="{self._NOW}"><Guid id="tmdb://111"/></Directory>'
        )
        self._mock_url(mock_plex)
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, text=f'<MediaContainer size="1" totalSize="1">{dated}</MediaContainer>')
        )

        mock_plex.watched_titles("2", MediaType.SHOW, "TOK")

        types = [c.request.url.params.get("type") for c in respx.calls]
        assert "4" not in types, f"an episode read was made for a library that needed none: {types}"

    @respx.mock
    def test_a_show_no_episode_can_date_keeps_the_epoch_rather_than_a_guess(self, mock_plex):
        """17 of 19 undated shows on a real server had no watched episodes either — nothing anywhere
        knows when they were watched. Saying so beats inventing a date."""
        marked = (
            '<Directory ratingKey="5001" type="show" title="No Date Anywhere" year="2023" '
            'leafCount="8" viewedLeafCount="8"><Guid id="tmdb://111"/></Directory>'
        )
        self._mock_url(mock_plex)

        def answer(request):
            body, size = ("", 0) if request.url.params.get("type") == "4" else (marked, 1)
            return httpx.Response(200, text=f'<MediaContainer size="{size}" totalSize="{size}">{body}</MediaContainer>')

        respx.get(self._URL).mock(side_effect=answer)

        item = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items[0]
        assert item.watched_at == datetime(1970, 1, 1, tzinfo=UTC)

    @respx.mock
    def test_the_recorded_episode_shape_dates_the_show_it_rolls_up_to(self, mock_plex):
        """Replayed from the real recording rather than hand-built XML (testing.md).

        `pms_watched_episodes_rollup.xml.txt` is the response this read actually gets — the fold has
        to survive its real attribute set, not a three-attribute stand-in.
        """
        raw = (FIXTURES / "pms_watched_episodes_rollup.xml.txt").read_text()
        eps_root = ET.fromstring(raw[raw.index("<MediaContainer") :])
        # The recording keeps the real server's totalSize (9563) above a SAMPLE of its rows. Left as
        # recorded, the walk would page for a total that never arrives; the rows are the point here,
        # not the count, so make the container describe what it actually carries.
        eps_root.set("size", str(len(list(eps_root))))
        eps_root.set("totalSize", str(len(list(eps_root))))
        episodes = ET.tostring(eps_root, encoding="unicode")
        leaves = [el for el in eps_root if el.get("grandparentRatingKey") and el.get("lastViewedAt")]
        assert leaves, "the fixture no longer carries dated episodes — this test proves nothing"
        show_key = leaves[0].get("grandparentRatingKey")
        newest = max(int(el.get("lastViewedAt")) for el in leaves if el.get("grandparentRatingKey") == show_key)
        marked = (
            f'<Directory ratingKey="{show_key}" type="show" title="From The Fixture" year="2023" '
            f'leafCount="8" viewedLeafCount="8"><Guid id="tmdb://111"/></Directory>'
        )
        self._mock_url(mock_plex)

        def answer(request):
            if request.url.params.get("type") == "4":
                return httpx.Response(200, text=episodes)
            return httpx.Response(200, text=f'<MediaContainer size="1" totalSize="1">{marked}</MediaContainer>')

        respx.get(self._URL).mock(side_effect=answer)

        item = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items[0]

        assert int(item.watched_at.timestamp()) == newest

    @respx.mock
    def test_a_failed_episode_read_leaves_the_epoch_rather_than_losing_the_show(self, mock_plex):
        """The repair is best-effort: losing it must cost the DATE, never the row or the coverage.

        `covers_window` gates deletion, so a 404 on this secondary read must not make the primary
        read look incomplete — and the item must still come back, or the show vanishes from the
        watched set and is recommended straight back.
        """
        marked = (
            '<Directory ratingKey="5001" type="show" title="Just Marked" year="2023" '
            'leafCount="8" viewedLeafCount="8"><Guid id="tmdb://111"/></Directory>'
        )
        self._mock_url(mock_plex)

        def answer(request):
            if request.url.params.get("type") == "4":
                return httpx.Response(404)
            return httpx.Response(200, text=f'<MediaContainer size="1" totalSize="1">{marked}</MediaContainer>')

        respx.get(self._URL).mock(side_effect=answer)

        read = mock_plex.watched_titles("2", MediaType.SHOW, "TOK")

        assert [i.title for i in read.items] == ["Just Marked"], "a failed date repair lost the row itself"
        assert read.covers_window is True, "a failed date repair made the primary read look incomplete"
        assert read.items[0].watched_at == datetime(1970, 1, 1, tzinfo=UTC)

    @respx.mock
    def test_the_newest_episode_date_is_found_on_a_LATER_page(self, mock_plex):
        """The episode list is not ordered by `lastViewedAt`, so the answer can be on any page.

        Stopping early here does not fail loudly — it produces an older date that looks perfectly
        plausible, which is why the walk pages on the reported total rather than on a full page.
        """
        marked = (
            '<Directory ratingKey="5001" type="show" title="Just Marked" year="2023" '
            'leafCount="8" viewedLeafCount="8"><Guid id="tmdb://111"/></Directory>'
        )
        self._mock_url(mock_plex)
        page_size = mock_plex._WATCHED_PAGE

        def answer(request):
            if request.url.params.get("type") != "4":
                return httpx.Response(200, text=f'<MediaContainer size="1" totalSize="1">{marked}</MediaContainer>')
            start = int(request.headers["X-Plex-Container-Start"])
            # Page 1 is full and OLD; the newest stamp is the single row on page 2.
            if start == 0:
                rows = "".join(
                    f'<Video ratingKey="{900 + n}" type="episode" title="Ep{n}" viewCount="1" '
                    f'grandparentRatingKey="5001" lastViewedAt="{self._NOW - 99999}"/>'
                    for n in range(page_size)
                )
                return httpx.Response(
                    200, text=f'<MediaContainer size="{page_size}" totalSize="{page_size + 1}">{rows}</MediaContainer>'
                )
            row = (
                f'<Video ratingKey="9999" type="episode" title="Newest" viewCount="1" '
                f'grandparentRatingKey="5001" lastViewedAt="{self._NOW}"/>'
            )
            return httpx.Response(
                200, text=f'<MediaContainer size="1" totalSize="{page_size + 1}">{row}</MediaContainer>'
            )

        respx.get(self._URL).mock(side_effect=answer)

        item = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items[0]

        assert int(item.watched_at.timestamp()) == self._NOW, "the walk stopped before the newest episode"

    @respx.mock
    def test_a_server_that_caps_the_page_and_reports_no_total_gets_no_date_at_all(self, mock_plex):
        """The truncation case, and the reason a short page cannot mean "the end" here.

        A server that omits `totalSize` and caps the container below what we asked for answers EVERY
        page short. Reading a short page as the end stops after one, and since the episode list is
        unordered that first slice dates every show arbitrarily far in the past — a wrong date that
        looks entirely plausible. An absent date says "unknown" and weighs zero; that is the honest
        answer, so the walk keeps going until the server actually returns nothing.
        """
        marked = (
            '<Directory ratingKey="5001" type="show" title="Just Marked" year="2023" '
            'leafCount="8" viewedLeafCount="8"><Guid id="tmdb://111"/></Directory>'
        )
        self._mock_url(mock_plex)
        cap = 200

        def answer(request):
            if request.url.params.get("type") != "4":
                return httpx.Response(200, text=f'<MediaContainer size="1" totalSize="1">{marked}</MediaContainer>')
            start = int(request.headers["X-Plex-Container-Start"])
            # Caps at 200 however much is asked for, and NEVER reports a total — so no page is ever
            # empty and no page is ever full. Nothing in the response can prove the end.
            rows = "".join(
                f'<Video ratingKey="{start + n}" type="episode" title="Ep" viewCount="1" '
                f'grandparentRatingKey="5001" lastViewedAt="{self._NOW - 99999}"/>'
                for n in range(cap)
            )
            return httpx.Response(200, text=f'<MediaContainer size="{cap}">{rows}</MediaContainer>')

        respx.get(self._URL).mock(side_effect=answer)

        read = mock_plex.watched_titles("2", MediaType.SHOW, "TOK")

        assert read.items[0].watched_at == datetime(1970, 1, 1, tzinfo=UTC), (
            "dated the show from a truncated, unordered slice of its episodes"
        )
        episode_calls = [c for c in respx.calls if c.request.url.params.get("type") == "4"]
        assert len(episode_calls) == mock_plex._EPISODE_PAGE_LIMIT, "the safety stop did not bound the walk"


class TestDatingAShowFromItsEpisodes:
    """`newest_episode_dates` — the repair for a show Plex re-counted without re-dating (#108).

    Everything here is pinned to `pms_all_leaves.xml.txt`, recorded off a real server, because the
    two behaviours that make this hard are both invisible from the code: the endpoint ignores
    `unwatched=0`, and a part-watched episode carries a `lastViewedAt` with no `viewCount`.
    """

    _URL = "http://pms:32400/library/metadata/460767/allLeaves"

    @staticmethod
    def _fixture() -> str:
        raw = (FIXTURES / "pms_all_leaves.xml.txt").read_text()
        return raw[raw.index("<MediaContainer") :]

    @respx.mock
    def test_a_part_watched_episode_does_not_date_the_show(self, mock_plex):
        """The trap the recording caught on the FIRST real show it was tried against.

        Episode 2 was started and abandoned: `viewOffset`, `lastViewedAt`, no `viewCount`. Its stamp
        is NEWER than the only episode actually watched, and it is not counted in the show's
        `viewedLeafCount` either — so `max(lastViewedAt)` across all episodes dates the show from an
        episode nobody finished.
        """
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))

        dates = mock_plex.newest_episode_dates("2", "TOK", {460767})

        watched, abandoned = 1637199585, 1637560154
        assert int(dates[460767].timestamp()) == watched, (
            "dated the show from a part-watched episode — the newer stamp belongs to one nobody finished"
        )
        assert int(dates[460767].timestamp()) != abandoned

    @respx.mock
    def test_it_sends_page_headers_so_the_server_reports_a_total(self, mock_plex):
        """`totalSize` is absent unless a container size is asked for, and without it a 1,175-episode
        show is one 3.6MB response with nothing to prove the walk finished (both measured)."""
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))

        mock_plex.newest_episode_dates("2", "TOK", {460767})

        headers = respx.calls.last.request.headers
        assert headers["X-Plex-Container-Size"] == str(mock_plex._WATCHED_PAGE)
        assert headers["X-Plex-Container-Start"] == "0"

    @respx.mock
    def test_no_shows_asked_for_means_no_request_at_all(self, mock_plex):
        """The whole point of detecting WHICH shows are stale: a quiet night must cost nothing."""
        mock_plex._server.url.return_value = self._URL
        route = respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))

        assert mock_plex.newest_episode_dates("2", "TOK", set()) == {}
        assert not route.called

    @respx.mock
    def test_many_stale_shows_switch_to_one_library_wide_read(self, mock_plex):
        """Past a dozen shows, one library read beats a call each — 2.8s against 1.1s for the show
        read on a 9,563-episode library, so a call per show overtakes it quickly."""
        section_url = "http://pms:32400/library/sections/2/all"
        mock_plex._server.url.return_value = section_url
        wanted = set(range(700, 700 + mock_plex._PER_SHOW_DATE_LIMIT + 1))
        rows = "".join(
            f'<Video ratingKey="{9000 + n}" type="episode" viewCount="1" '
            f'grandparentRatingKey="{key}" lastViewedAt="{1_700_000_000 + n}"/>'
            for n, key in enumerate(sorted(wanted))
        )
        route = respx.get(section_url).mock(
            return_value=httpx.Response(
                200, text=f'<MediaContainer size="{len(wanted)}" totalSize="{len(wanted)}">{rows}</MediaContainer>'
            )
        )

        dates = mock_plex.newest_episode_dates("2", "TOK", wanted)

        assert len(route.calls) == 1, "made a request per show instead of one library-wide read"
        assert route.calls.last.request.url.params.get("type") == "4"
        assert set(dates) == wanted

    @respx.mock
    def test_both_paths_agree_about_the_same_show(self, mock_plex):
        """The per-show and library-wide paths must never date one show differently.

        The per-show path filters on `viewCount` client-side because this endpoint family cannot be
        trusted to filter. The bulk path used to lean on the server's `unwatched=0` instead — so the
        two disagreed by four days on the commit's own recorded rows, and the bulk path picked the
        episode nobody finished. This server does exclude those, but `viewedLeafCount!=0` and
        `lastViewedAt>=` are both silently ignored by it, so the guard belongs in our code.
        """
        watched, abandoned = 1637199585, 1637560154
        section_url = "http://pms:32400/library/sections/2/all"
        mock_plex._server.url.return_value = section_url
        # The server hands back BOTH, as an ignored filter would.
        rows = (
            f'<Video ratingKey="1" type="episode" grandparentRatingKey="460767" viewCount="1" '
            f'lastViewedAt="{watched}"/>'
            f'<Video ratingKey="2" type="episode" grandparentRatingKey="460767" viewOffset="1058389" '
            f'lastViewedAt="{abandoned}"/>'
        )
        respx.get(section_url).mock(
            return_value=httpx.Response(200, text=f'<MediaContainer size="2" totalSize="2">{rows}</MediaContainer>')
        )

        bulk = mock_plex._newest_episode_stamps("2", "TOK")

        assert bulk[460767] == watched, "the bulk fold dated a show from a part-watched episode"

    @respx.mock
    def test_one_unreadable_show_does_not_cost_the_others_their_dates(self, mock_plex):
        mock_plex._server.url.side_effect = lambda path, **k: f"http://pms:32400{path}"
        respx.get("http://pms:32400/library/metadata/1/allLeaves").mock(return_value=httpx.Response(500))
        respx.get("http://pms:32400/library/metadata/460767/allLeaves").mock(
            return_value=httpx.Response(200, text=self._fixture())
        )

        dates = mock_plex.newest_episode_dates("2", "TOK", {1, 460767})

        assert set(dates) == {460767}, "one failing show took the others' dates with it"


class TestScrobbleAs:
    """Marking a title played AS another account — the write behind the watch-history transfer.

    Two things must hold or the transfer is unsafe: it uses the TARGET's token (not the owner's, or
    it marks the title watched for the wrong person), and a title that account cannot see is skipped
    rather than raised (that is the normal case for an unshared library, and one of them must not
    abandon the other two thousand).
    """

    _URL = "http://pms:32400/:/scrobble"

    @respx.mock
    def test_sends_the_targets_token_and_the_library_identifier(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = self._URL
        route = respx.get(self._URL).mock(return_value=httpx.Response(200, text=""))

        assert mock_plex.scrobble_as(4242, "TARGET-TOKEN") is True

        request = route.calls[0].request
        assert request.headers["X-Plex-Token"] == "TARGET-TOKEN"
        assert request.url.params["key"] == "4242"
        # Without the identifier the PMS ignores the scrobble entirely.
        assert request.url.params["identifier"] == "com.plexapp.plugins.library"

    @respx.mock
    def test_an_invisible_title_is_skipped_not_raised(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(404, text=""))

        assert mock_plex.scrobble_as(4242, "TARGET-TOKEN") is False

    @respx.mock
    def test_a_real_server_error_still_raises(self, mock_plex: PlexClient):
        """403/404 mean "not visible to them"; a 500 means the PMS is unwell and the caller should
        hear about it rather than silently record thousands of skips."""
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(500, text=""))

        with pytest.raises(httpx.HTTPStatusError):
            mock_plex.scrobble_as(4242, "TARGET-TOKEN")

    def test_dry_run_writes_nothing_at_all(self, mock_plex: PlexClient, monkeypatch):
        """Rule 8. No respx route registered, so any HTTP call would fail the test outright."""
        from shortlist.engine.clients import plex_pms

        def explode(*_a, **_k):
            raise AssertionError("dry run must not touch the PMS")

        monkeypatch.setattr(plex_pms.http_retry, "get", explode)

        assert mock_plex.scrobble_as(4242, "TARGET-TOKEN", dry_run=True) is True


class TestTheRecordedShowLibraryResponse:
    """Replays `tests/fixtures/pms_watched_shows.xml.txt` through the real parser.

    A fixture nothing reads is documentation, not a fixture (rule 11). This one exists because the
    already-watched rule turns entirely on what `?type=2&unwatched=0` returns, and that was assumed
    rather than measured until a live probe on 2026-08-05.
    """

    _URL = "http://pms:32400/library/sections/2/all"

    @staticmethod
    def _fixture() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "fixtures" / "pms_watched_shows.xml.txt").read_text()

    @respx.mock
    def test_a_real_show_library_read_returns_barely_started_series(self, mock_plex: PlexClient):
        """The finding that drove the 1.2 rule change: Plex's watched filter is "more than zero
        episodes", not "finished". A show 2 of 176 in — 1.1% — comes back from this endpoint."""
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))

        items = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items

        seen = {(i.viewed_leaf_count, i.leaf_count) for i in items}
        assert (2, 176) in seen, "a 1.1%-watched show is returned by unwatched=0"
        assert (2, 23) in seen and (4, 142) in seen
        # ...and fully-watched ones come back through the same read, undistinguished.
        assert (47, 47) in seen and (100, 100) in seen

    @respx.mock
    def test_most_of_what_it_returns_is_not_finished(self, mock_plex: PlexClient):
        """Half the recorded rows sit under the OLD `min(80%, max(3, 15%))` bar, so under the old
        rule they stayed eligible to be recommended back to the person watching them."""
        from shortlist.engine.rows import _watched_titles

        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))

        items = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items
        shows = {i.tmdb_id: (i.viewed_leaf_count, i.leaf_count) for i in items}
        finished = _watched_titles(set(), shows, 0.8)

        assert len(items) == 10
        assert len(finished) == 5, "five of ten started shows did not count as watched"

    @respx.mock
    def test_a_finished_show_can_carry_NO_watched_stamp_at_all(self, mock_plex: PlexClient):
        """Issue #108's shape, measured 20 times on a live server.

        A show can have complete, correct episode counts and no `lastViewedAt` on its own row —
        which is what happens when a series or season is marked watched rather than an episode
        played. `?type=2&unwatched=0` filters on that stamp, so such a show never comes back from it
        at all; the episode-level read is what recovers it.

        This test previously asserted the opposite, after a probe "disproved" the shape by asking
        `?type=2&unwatched=0` which of its rows lacked the stamp — the one query that excludes them.
        """
        from datetime import UTC, datetime

        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))

        items = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items

        finished = next(i for i in items if i.tmdb_id == 300006)
        assert (finished.viewed_leaf_count, finished.leaf_count) == (100, 100), "the show is finished"
        assert finished.watched_at == datetime(1970, 1, 1, tzinfo=UTC), "no stamp of its own"


class TestTheRecordedUserRatingResponse:
    """Replays `tests/fixtures/pms_watched_user_rating.xml.txt` through the real parser.

    Issue #69 turns on `userRating` arriving free on the watched read we already make. That is now
    measured rather than assumed (rule 11), and the fixture also records the trap: on a server
    running Kometa's rating sync, most of the OWNER's ratings were written by a tool, not typed.
    """

    _URL = "http://pms:32400/library/sections/1/all"

    @staticmethod
    def _fixture() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "fixtures" / "pms_watched_user_rating.xml.txt").read_text()

    def _items(self, mock_plex: PlexClient) -> list:
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))
        return mock_plex.watched_titles("1", MediaType.MOVIE, "TOK").items

    @respx.mock
    def test_user_rating_is_parsed_off_the_watched_read(self, mock_plex: PlexClient):
        """The whole feasibility claim in one assertion: no second request, no new endpoint."""
        by_id = {i.tmdb_id: i for i in self._items(mock_plex)}

        assert by_id[333371].user_rating == 7.9
        assert by_id[1364939].user_rating == 2.0

    @respx.mock
    def test_a_title_nobody_rated_carries_no_rating_rather_than_a_zero(self, mock_plex: PlexClient):
        """0.0 is a rating someone can give. "Never rated" has to stay distinguishable from it, or
        every unrated title in the library reads as universally hated and stops seeding."""
        by_id = {i.tmdb_id: i for i in self._items(mock_plex)}

        assert by_id[1332077].user_rating is None
        assert by_id[1332077].is_human_rating is False

    @respx.mock
    def test_a_fractional_rating_is_recognised_as_tool_written(self, mock_plex: PlexClient):
        """The guard that keeps Kometa's IMDb scores from reading as the owner's opinion. Plex's own
        control cannot write 7.9, so nothing that did was typed by a person."""
        by_id = {i.tmdb_id: i for i in self._items(mock_plex)}

        assert by_id[333371].is_human_rating is False, "7.9 cannot come from Plex's star control"
        assert by_id[63].is_human_rating is False, "8.8 — and identical to the title's `rating`"
        assert by_id[1248753].is_human_rating is True, "8.0, from a real viewer"

    @respx.mock
    def test_the_tool_also_writes_some_whole_numbers(self, mock_plex: PlexClient):
        """Why one guard is not enough. Row 3 is tool-written and lands on 6.0, which the per-value
        check cannot tell from an opinion — the account-level check in `history` is what catches it.
        If this ever fails because the value changed, the account-level guard is what still holds."""
        by_id = {i.tmdb_id: i for i in self._items(mock_plex)}

        assert by_id[509967].user_rating == 6.0
        assert by_id[509967].is_human_rating is True, "indistinguishable per-value — hence two layers"

    @respx.mock
    def test_the_owner_rows_in_this_fixture_fail_the_account_level_guard(self, mock_plex: PlexClient):
        """The two layers composed, over the real recording: taken as one account, these ratings are
        mostly fractional, so NONE of them are believed — including the whole-numbered 6.0."""
        from shortlist.engine.history import disliked_seed_keys, ratings_are_trustworthy

        owner_rows = [i for i in self._items(mock_plex) if i.tmdb_id in {333371, 63, 509967}]
        # The recording holds three owner rows and the account guard abstains under five, so the
        # sample is doubled to reach a judgeable size while keeping the recorded RATIO (1 whole in 3)
        # — which is close to the real account's 9.3%. Doubling rows rather than bare values because
        # `disliked_seed_keys` judges the account from the same list it then filters; handing it a
        # short list would have it abstain and suppress on the 6.0, which is exactly what an earlier
        # version of this test did.
        doubled = owner_rows * 2

        assert ratings_are_trustworthy([i.user_rating for i in doubled]) is False
        assert disliked_seed_keys(doubled, 6.0) == set(), "a distrusted account suppresses nothing"
        # ...and the same six rows, believed, WOULD have suppressed the tool's 6.0. That contrast is
        # the point: the account guard is the only thing standing between Kometa's IMDb scores and a
        # silently shrunken seed list.
        assert disliked_seed_keys([i for i in doubled if i.is_human_rating], 6.0) == {(509967, MediaType.MOVIE)}
