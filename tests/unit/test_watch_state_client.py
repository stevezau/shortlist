"""Reading an account's exact watch state, and applying one planned write to it.

The PMS half of the watching-account transfer. The plan itself is pure and lives in
`test_watch_replica.py`; this file is about the four reads that build a `WatchState` and the three
endpoints that change one.

Every response shape here was taken from a live probe against SFLIX (PMS 1.43.3.10896) on
2026-08-25 — see `.claude/docs/watching-account-transfer-design.md`.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.models import MediaType
from shortlist.engine.watch_replica import ItemState, OpKind, WriteOp

MOVIES_URL = "http://pms:32400/library/sections/1/all"
SHOWS_URL = "http://pms:32400/library/sections/2/all"


def container(*rows: str) -> str:
    return f'<MediaContainer size="{len(rows)}">{"".join(rows)}</MediaContainer>'


MOVIE_WATCHED = (
    '<Video ratingKey="60632" type="movie" title="10 Cloverfield Lane" '
    'viewCount="1" viewOffset="490509" lastViewedAt="1658467759" duration="6214816"/>'
)
MOVIE_PARTIAL_ONLY = (
    '<Video ratingKey="228943" type="movie" title="752 Is Not a Number" viewOffset="1139347" duration="6029857"/>'
)
EPISODE_WATCHED = (
    '<Video ratingKey="641877" type="episode" title="The Goddamn Brownies" '
    'grandparentRatingKey="613965" parentIndex="1" index="1" viewCount="2" lastViewedAt="1658467759"/>'
)
EPISODE_PARTIAL = (
    '<Video ratingKey="700001" type="episode" title="Pilot" grandparentRatingKey="613965" viewOffset="1294833"/>'
)


def route_for(url: str, watched: str, partial: str):
    """Two routes on one path, split by the filter — the reads differ only by query param."""
    respx.get(url, params__contains={"unwatched": "0"}).mock(return_value=httpx.Response(200, text=watched))
    respx.get(url, params__contains={"viewOffset>": "0"}).mock(return_value=httpx.Response(200, text=partial))


class TestReadingWatchState:
    @respx.mock
    def test_a_movie_both_watched_and_in_progress_keeps_both_facts(self, mock_plex: PlexClient):
        """The two reads overlap on this film, and each is given ONLY its own field — the pessimistic
        shape. Served the same complete row twice, this test passes even with the merge deleted, which
        is how it was written first.
        """
        mock_plex._server.url.return_value = MOVIES_URL
        watched_only = '<Video ratingKey="60632" type="movie" title="10 Cloverfield Lane" viewCount="1"/>'
        offset_only = '<Video ratingKey="60632" type="movie" title="10 Cloverfield Lane" viewOffset="490509"/>'
        route_for(MOVIES_URL, container(watched_only), container(offset_only))

        state = mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        assert state.items[60632].view_count == 1
        assert state.items[60632].view_offset_ms == 490509

    @respx.mock
    def test_the_merge_does_not_depend_on_which_read_ran_first(self, mock_plex: PlexClient):
        """Same two rows, swapped between the reads. Overwriting rather than merging would keep
        whichever landed last, so this is the assertion that pins the direction-independence."""
        mock_plex._server.url.return_value = MOVIES_URL
        watched_only = '<Video ratingKey="60632" type="movie" viewCount="1"/>'
        offset_only = '<Video ratingKey="60632" type="movie" viewOffset="490509"/>'
        route_for(MOVIES_URL, container(offset_only), container(watched_only))

        state = mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        assert (state.items[60632].view_count, state.items[60632].view_offset_ms) == (1, 490509)

    @respx.mock
    def test_a_film_started_and_never_finished_is_returned_with_no_view_count(self, mock_plex: PlexClient):
        """`unwatched=0` cannot see this row at all — it is exactly what the old transfer dropped."""
        mock_plex._server.url.return_value = MOVIES_URL
        route_for(MOVIES_URL, container(), container(MOVIE_PARTIAL_ONLY))

        state = mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        assert state.items[228943].view_count == 0
        assert state.items[228943].view_offset_ms == 1139347

    @respx.mock
    def test_an_episode_carries_its_show_key_and_its_own_view_count(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = SHOWS_URL
        route_for(SHOWS_URL, container(EPISODE_WATCHED), container(EPISODE_PARTIAL))

        state = mock_plex.read_watch_state([("2", MediaType.SHOW)], "TARGET-TOKEN")

        assert state.items[641877].show_rating_key == 613965
        assert state.items[641877].view_count == 2
        assert state.items[641877].media_type == "episode"
        assert state.items[700001].view_offset_ms == 1294833

    @respx.mock
    def test_a_show_library_is_read_at_episode_level_never_show_level(self, mock_plex: PlexClient):
        """`type=4`, not `type=2`. A show row is derived state Plex can disagree with: a show-key
        scrobble leaves it reading 47/47 while the show-level query cannot see it at all."""
        mock_plex._server.url.return_value = SHOWS_URL
        route_for(SHOWS_URL, container(EPISODE_WATCHED), container())

        mock_plex.read_watch_state([("2", MediaType.SHOW)], "TARGET-TOKEN")

        assert {c.request.url.params["type"] for c in respx.calls} == {"4"}

    @respx.mock
    def test_a_movie_library_is_read_as_type_one(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = MOVIES_URL
        route_for(MOVIES_URL, container(MOVIE_WATCHED), container())

        mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        assert {c.request.url.params["type"] for c in respx.calls} == {"1"}

    @respx.mock
    def test_both_container_headers_are_sent(self, mock_plex: PlexClient):
        """Size alone is IGNORED by this PMS, which then returns the entire library — the same trap
        `_history_page` documents. Start must go with it."""
        mock_plex._server.url.return_value = MOVIES_URL
        route_for(MOVIES_URL, container(MOVIE_WATCHED), container())

        mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        for call in respx.calls:
            assert call.request.headers["X-Plex-Container-Start"] == "0"
            assert call.request.headers["X-Plex-Container-Size"] == "500"

    @respx.mock
    def test_it_reads_as_the_given_token_not_the_owners(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = MOVIES_URL
        route_for(MOVIES_URL, container(MOVIE_WATCHED), container())

        mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        assert {c.request.headers["X-Plex-Token"] for c in respx.calls} == {"TARGET-TOKEN"}

    @respx.mock
    def test_a_library_this_account_cannot_see_is_skipped_not_an_error(self, mock_plex: PlexClient):
        """403 is "not shared with them", which is a correct answer of "nothing", not a failure. A
        target with narrower sharing than the source is the normal case, not an exception."""
        mock_plex._server.url.return_value = MOVIES_URL
        respx.get(MOVIES_URL).mock(return_value=httpx.Response(403, text=""))

        assert mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN").items == {}

    @respx.mock
    def test_a_server_error_still_raises(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = MOVIES_URL
        respx.get(MOVIES_URL).mock(return_value=httpx.Response(500, text=""))

        with pytest.raises(httpx.HTTPStatusError):
            mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

    @respx.mock
    def test_a_row_with_no_rating_key_is_dropped_rather_than_keyed_on_zero(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = MOVIES_URL
        route_for(MOVIES_URL, container('<Video type="movie" title="broken" viewCount="1"/>'), container())

        assert mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN").items == {}


class TestApplyingOneWrite:
    _URL = "http://pms:32400/:/scrobble"

    def op(self, kind: OpKind, **kw) -> WriteOp:
        return WriteOp(kind=kind, rating_key=4242, media_type="movie", **kw)

    @respx.mock
    def test_a_mark_scrobbles_once_per_SHORTFALL_not_once_per_total(self, mock_plex: PlexClient):
        """A scrobble only ever adds one. `view_count` is where the title should END UP; `scrobbles`
        is how many calls that takes given what the account already has. Looping over the total would
        take a film already watched once to four, and the count would climb on every re-run."""
        mock_plex._server.url.return_value = self._URL
        route = respx.get(self._URL).mock(return_value=httpx.Response(200, text=""))

        assert mock_plex.apply_watch_op(self.op(OpKind.MARK, view_count=3, scrobbles=2), "TARGET-TOKEN") is True

        assert len(route.calls) == 2
        assert route.calls[0].request.url.params["key"] == "4242"
        assert route.calls[0].request.url.params["identifier"] == "com.plexapp.plugins.library"

    @respx.mock
    def test_a_mark_with_no_shortfall_still_writes_once(self, mock_plex: PlexClient):
        """Guards a silent no-op: a plan built from a source whose `viewCount` Plex omitted would
        otherwise mark nothing at all while reporting success."""
        mock_plex._server.url.return_value = self._URL
        route = respx.get(self._URL).mock(return_value=httpx.Response(200, text=""))

        mock_plex.apply_watch_op(self.op(OpKind.MARK, view_count=0, scrobbles=0), "TARGET-TOKEN")

        assert len(route.calls) == 1

    @respx.mock
    def test_an_unmark_hits_unscrobble(self, mock_plex: PlexClient):
        url = "http://pms:32400/:/unscrobble"
        mock_plex._server.url.return_value = url
        route = respx.get(url).mock(return_value=httpx.Response(200, text=""))

        assert mock_plex.apply_watch_op(self.op(OpKind.UNMARK), "TARGET-TOKEN") is True

        assert route.calls[0].request.url.params["key"] == "4242"
        assert route.calls[0].request.headers["X-Plex-Token"] == "TARGET-TOKEN"

    @respx.mock
    def test_setting_an_offset_sends_the_position_and_a_stopped_state(self, mock_plex: PlexClient):
        url = "http://pms:32400/:/progress"
        mock_plex._server.url.return_value = url
        route = respx.get(url).mock(return_value=httpx.Response(200, text=""))

        mock_plex.apply_watch_op(self.op(OpKind.SET_OFFSET, offset_ms=490509), "TARGET-TOKEN")

        assert route.calls[0].request.url.params["time"] == "490509"
        assert route.calls[0].request.url.params["state"] == "stopped"

    @respx.mock
    def test_clearing_an_offset_un_scrobbles_rather_than_sending_time_zero(self, mock_plex: PlexClient):
        """`/:/progress?time=0` does NOT clear an offset — live-probed, 1,139,347 stayed 1,139,347.
        `/:/unscrobble` is the only call that does. Sending `time=0` made a real undo report success
        while leaving 293 items part-watched."""
        url = "http://pms:32400/:/unscrobble"
        mock_plex._server.url.return_value = url
        route = respx.get(url).mock(return_value=httpx.Response(200, text=""))

        assert mock_plex.apply_watch_op(self.op(OpKind.CLEAR_OFFSET), "TARGET-TOKEN") is True

        assert route.calls[0].request.url.params["key"] == "4242"
        assert "time" not in route.calls[0].request.url.params

    @respx.mock
    def test_an_invisible_title_is_skipped_not_raised(self, mock_plex: PlexClient):
        """A target with narrower library sharing 404s on titles it cannot see. One of those must not
        abandon a run of eleven thousand others."""
        url = "http://pms:32400/:/unscrobble"
        mock_plex._server.url.return_value = url
        respx.get(url).mock(return_value=httpx.Response(404, text=""))

        assert mock_plex.unscrobble_as(4242, "TARGET-TOKEN") is False

    @respx.mock
    def test_a_server_error_still_raises(self, mock_plex: PlexClient):
        url = "http://pms:32400/:/progress"
        mock_plex._server.url.return_value = url
        respx.get(url).mock(return_value=httpx.Response(500, text=""))

        with pytest.raises(httpx.HTTPStatusError):
            mock_plex.set_progress_as(4242, 1000, "TARGET-TOKEN")

    @pytest.mark.parametrize(
        "kind,kw",
        [
            (OpKind.MARK, {"view_count": 3, "scrobbles": 3}),
            (OpKind.UNMARK, {}),
            (OpKind.SET_OFFSET, {"offset_ms": 5000}),
            (OpKind.CLEAR_OFFSET, {}),
        ],
    )
    def test_dry_run_touches_the_server_for_no_kind_of_write(self, mock_plex: PlexClient, monkeypatch, kind, kw):
        """Rule 8, across every op — including UNMARK and CLEAR_OFFSET, the two that delete."""
        from shortlist.engine.clients import plex_pms

        def explode(*_a, **_k):
            raise AssertionError("dry run must not touch the PMS")

        monkeypatch.setattr(plex_pms.http_retry, "get", explode)
        op = WriteOp(kind=kind, rating_key=4242, media_type="movie", **kw)

        assert mock_plex.apply_watch_op(op, "TARGET-TOKEN", dry_run=True) is True


class TestItemStateFromRealRows:
    def test_a_missing_view_count_reads_as_zero_not_one(self):
        """`viewCount` is absent, not "0", on a row that was only ever started — 72 films on the
        maintainer's account. Defaulting it to 1 would mark every one of them finished."""
        state = PlexClient._leaf_state(
            __import__("xml.etree.ElementTree", fromlist=["ET"]).fromstring(MOVIE_PARTIAL_ONLY), "movie"
        )

        assert state is not None
        assert state.view_count == 0
        assert state.view_offset_ms == 1139347

    def test_an_episode_without_a_show_key_still_parses(self):
        """Defensive: 9,850 of 9,850 real episodes carried one, but a `None` here must not raise
        mid-read and lose the other nine thousand."""
        import xml.etree.ElementTree as ET

        state = PlexClient._leaf_state(ET.fromstring('<Video ratingKey="1" viewCount="1"/>'), "episode")

        assert state == ItemState(rating_key=1, media_type="episode", view_count=1)


class TestTheRecordedResponses:
    """Replays `tests/fixtures/pms_*.xml.txt` through the real parser.

    A fixture nothing reads is documentation, not a fixture (rule 11). These exist because the whole
    replication turns on what these four reads return, and every one of them was an assumption until
    a live probe on 2026-08-25 (SFLIX, PMS 1.43.3.10896).
    """

    @staticmethod
    def _fixture(name: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "fixtures" / f"{name}.xml.txt").read_text()

    @respx.mock
    def test_a_real_episode_read_carries_the_show_key_on_every_row(self, mock_plex: PlexClient):
        """9,850 of 9,850 episodes on a real server carried `grandparentRatingKey`. The history log
        does NOT — it has only a `grandparentKey` path that has to be parsed — so this is the read
        that makes episode-to-show mapping free."""
        mock_plex._server.url.return_value = SHOWS_URL
        route_for(SHOWS_URL, self._fixture("pms_watched_episodes"), container())

        state = mock_plex.read_watch_state([("2", MediaType.SHOW)], "TOKEN")

        assert state.items
        assert all(item.show_rating_key for item in state.items.values())
        assert all(item.media_type == "episode" for item in state.items.values())

    @respx.mock
    def test_a_real_in_progress_movie_read_has_offsets_and_no_view_count(self, mock_plex: PlexClient):
        """These rows are invisible to `unwatched=0`, which is why the old transfer dropped every
        part-watched film — 72 of them on the maintainer's own account."""
        mock_plex._server.url.return_value = MOVIES_URL
        route_for(MOVIES_URL, container(), self._fixture("pms_in_progress_movies"))

        state = mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TOKEN")

        assert state.items
        assert all(item.view_offset_ms > 0 for item in state.items.values())

    @respx.mock
    def test_a_real_in_progress_episode_read_parses(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = SHOWS_URL
        route_for(SHOWS_URL, container(), self._fixture("pms_in_progress_episodes"))

        state = mock_plex.read_watch_state([("2", MediaType.SHOW)], "TOKEN")

        assert state.items
        assert all(item.view_offset_ms > 0 for item in state.items.values())

    def test_a_scrobbled_account_still_has_an_EMPTY_history_log(self):
        """The assumption the whole design rests on, and the one most likely to break silently.

        Recorded from an account that had just been scrobbled 31 times during the write probe: its
        `/status/sessions/history/all` is still `size="0"`. If a PMS upgrade starts logging scrobbles,
        a transfer would inject ~11,000 fake plays into `watch_events` and inflate every figure in the
        effectiveness report — and nothing else in the system would notice.
        """
        import xml.etree.ElementTree as ET

        root = ET.fromstring(self._fixture("pms_history_after_scrobble_empty"))

        assert root.get("size") == "0"
        assert list(root) == []


class TestTheOwnersTokenNeverReachesTheUrl:
    """`includeToken=False` is what stops the OWNER's token going into the query string.

    Asserted by nothing until now: every test in this file stubs `_server.url` with a `return_value`,
    which discards the kwarg entirely — so removing it from the SUT broke no test, while the real
    consequence is ~11,000 watch writes carrying the admin token in the URL, on the one feature whose
    whole purpose is to stop writing as the owner. Rule 9, and the thing the feature is for.
    """

    @respx.mock
    def test_a_write_asks_for_a_token_free_url(self, mock_plex: PlexClient):
        url = "http://pms:32400/:/scrobble"
        mock_plex._server.url.return_value = url
        respx.get(url).mock(return_value=httpx.Response(200, text=""))

        mock_plex.scrobble_as(4242, "TARGET-TOKEN")

        assert mock_plex._server.url.call_args.kwargs["includeToken"] is False

    @respx.mock
    def test_a_read_asks_for_a_token_free_url(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = MOVIES_URL
        route_for(MOVIES_URL, container(MOVIE_WATCHED), container())

        mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        assert mock_plex._server.url.call_args.kwargs["includeToken"] is False

    @respx.mock
    def test_no_token_appears_in_any_request_url(self, mock_plex: PlexClient):
        """The OUTCOME, with a `url` that behaves like plexapi's — belt to the two braces above.

        Honest about what it does and does not prove. It does NOT catch a dropped `includeToken`:
        every one of these call sites also passes `params=`, and httpx REPLACES a URL's query string
        with those — so the owner's token is stripped a second time whatever the flag says. The two
        assertions above are what pin the flag itself.

        What this pins is the end state, and it would catch a future call site that dropped BOTH
        protections, or a switch to a client that merges query strings instead of replacing them.
        """

        def build(path, includeToken=True):
            return f"http://pms:32400{path}" + ("" if includeToken is False else "?X-Plex-Token=OWNER-SECRET")

        mock_plex._server.url.side_effect = build
        route_for(MOVIES_URL, container(MOVIE_WATCHED), container())

        mock_plex.read_watch_state([("1", MediaType.MOVIE)], "TARGET-TOKEN")

        assert respx.calls
        for call in respx.calls:
            assert "X-Plex-Token" not in call.request.url.params
            assert "OWNER-SECRET" not in str(call.request.url)
