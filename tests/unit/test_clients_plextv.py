"""PlexTvClient: users, share filters, pins and the recorded plex.tv responses."""

from __future__ import annotations

import re

import httpx
import pytest
import respx

import shortlist.engine.clients.plextv as plextv_mod
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.models import UserType
from tests.unit.clients_support import FIXTURES

USERS_XML = (FIXTURES / "plextv_users.xml.txt").read_text()


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

        marked = "✨ Movies Picked for You" + row_marker(1000001)
        assert len(marked) == len("✨ Movies Picked for You") + 64

        rendered = log_title(marked)
        assert rendered == "✨ Movies Picked for You [acct 1000001]"
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
    def test_home_user_token_exchange_flow(self):
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
        token = self._client().home_user_server_token(555000100)
        assert token == "server-tok"
        # The resources exchange must run AS the switched user (Phase 0 finding: owner token 401s).
        assert resources.calls.last.request.headers["X-Plex-Token"] == "switch-tok"

    @respx.mock
    def test_pin_protected_home_user_refused(self):
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
            self._client().home_user_server_token(1)

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
