"""PlexClient watched-title reads: paging, window coverage, dating shows and the recorded responses."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from shortlist.engine.clients.plex_pms import (
    LibraryCollection,
    LibraryTitle,
    PlexClient,
)
from shortlist.engine.models import MediaType
from tests.db_helpers import disposing_engine
from tests.unit.clients_support import FIXTURES


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
        with disposing_engine(make_engine(tmp_path)) as engine:
            sessions = make_session_factory(engine)
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
            respx.get(self._URL).mock(
                return_value=httpx.Response(200, text=self._page(everything[:1], size=1, total=None))
            )
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
        with disposing_engine(make_engine(tmp_path)) as engine:
            sessions = make_session_factory(engine)
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

            respx.get(self._URL).mock(
                return_value=httpx.Response(200, text=self._page(everything[:1], size=1, total=None))
            )
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
    def test_a_dropped_title_is_named_by_its_ratingKey_on_any_read(self, mock_plex):
        """The count says how many; a run's summary has to know WHICH, to count one title once across
        everyone it was dropped for. Named on an incremental read as well — a run's reads mostly are,
        and a title with no tmdb:// guid is missing from the person's watched set whichever read met it.
        """
        legacy = (
            f'<Video ratingKey="7" type="movie" title="Legacy Agent" year="2001" viewCount="1" '
            f'lastViewedAt="{self._NOW}"><Guid id="imdb://tt0000007"/></Video>'
        )
        matched = (
            f'<Video ratingKey="8" type="movie" title="Matched" year="2002" viewCount="1" '
            f'lastViewedAt="{self._NOW - 10}"><Guid id="tmdb://88"/></Video>'
        )
        body = f'<MediaContainer size="2" totalSize="2">{legacy}{matched}</MediaContainer>'

        incremental = self._read(mock_plex, body, since_ago=1000)
        complete = mock_plex.watched_titles("1", MediaType.MOVIE, "TOK")

        assert incremental.dropped_keys == frozenset({"7"})
        assert complete.dropped_keys == frozenset({"7"})
        assert [i.tmdb_id for i in complete.items] == [88]

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
        no `lastViewedAt` on the show at all. Verified live — Guest/Rabbit Hole read
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
        from shortlist.engine.rows import watched_titles

        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, text=self._fixture()))

        items = mock_plex.watched_titles("2", MediaType.SHOW, "TOK").items
        shows = {i.tmdb_id: (i.viewed_leaf_count, i.leaf_count) for i in items}
        finished = watched_titles(set(), shows, 0.8)

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


class TestTheRecordedSeasonSourceReads:
    """Replays `pms_collection_children.xml.txt` and `pms_section_title_search.xml.txt` through plexapi's real
    parser: the two reads a custom season makes of the owner's library (issue #137).

    The collections listing itself is built from the Directory recorded in `pms_collections_listing.json`, and
    the fake PMS refuses any read it was not given — so a per-item re-read, which plexapi issues silently for
    an attribute a listing lacks, fails here rather than costing a round trip per film in production.
    """

    LISTING = "/library/sections/{key}/all"
    CHILDREN = "/library/collections/{rating_key}/children"

    @staticmethod
    def _directory(rating_key: int, title: str, *, smart: bool = False, subtype: str = "movie") -> str:
        recorded = json.loads((FIXTURES / "pms_collections_listing.json").read_text())["listing"]
        attrs = {
            **recorded["directory_attributes"],
            "ratingKey": str(rating_key),
            "key": f"/library/collections/{rating_key}/children",
            "title": title,
            "titleSort": title,
            "subtype": subtype,
            "childCount": "4",
        }
        if smart:
            attrs["smart"] = "1"
        return ET.tostring(ET.Element("Directory", attrs), encoding="unicode")

    @staticmethod
    def _section(server, key: str, kind: str, title: str):
        from plexapi.library import MovieSection, ShowSection

        cls = MovieSection if kind == "movie" else ShowSection
        return cls(server, ET.Element("Directory", {"key": key, "type": kind, "title": title}), "/library/sections")

    def _serve(self, mock_plex: PlexClient, routes: dict[str, str], sections: list[tuple[str, str, str]]) -> list:
        """Answer each read from ``routes`` (path, or path + one telling query param) and record it."""
        reads: list[tuple[str, dict]] = []

        def query(key, method=None, headers=None, params=None, **_kwargs):
            headers = dict(headers or {})
            reads.append((key, headers))
            path, _, query_string = key.partition("?")
            for route, body in routes.items():
                route_path, _, needle = route.partition("?")
                if path == route_path and needle in query_string:
                    return self._page(ET.fromstring(body), headers)
            raise AssertionError(f"the fake PMS was not given {key}")

        mock_plex._server.query.side_effect = query
        mock_plex._server.library.sections.return_value = [
            self._section(mock_plex._server, key, kind, title) for key, kind, title in sections
        ]
        return reads

    @staticmethod
    def _page(container: ET.Element, headers: dict) -> ET.Element:
        """The slice the container headers ask for, as a real PMS serves it (``size`` is the slice's)."""
        start = int(headers.get("X-Plex-Container-Start", 0))
        size = int(headers.get("X-Plex-Container-Size", len(container)))
        children = list(container)
        for child in children:
            container.remove(child)
        container.extend(children[start : start + size])
        container.set("size", str(len(container)))
        return container

    def _listing(self, *directories: str) -> str:
        return f'<MediaContainer size="{len(directories)}">{"".join(directories)}</MediaContainer>'

    def test_collection_members_are_the_recorded_children_by_tmdb_id(self, mock_plex: PlexClient):
        reads = self._serve(
            mock_plex,
            {
                self.LISTING.format(key="1") + "?type=18": self._listing(
                    self._directory(536664, "Letterboxd Oscars Best Picture Winners")
                ),
                self.CHILDREN.format(rating_key=536664): (FIXTURES / "pms_collection_children.xml.txt").read_text(),
            },
            [("1", "movie", "Movies")],
        )

        # Case-insensitive: Kometa may re-case a title it recreates each season.
        members = mock_plex.collection_members("1", "letterboxd OSCARS best picture winners")

        assert members == [
            LibraryTitle(424, MediaType.MOVIE, "Schindler's List", 1993),
            LibraryTitle(197, MediaType.MOVIE, "Braveheart", 1995),
            LibraryTitle(1054867, MediaType.MOVIE, "One Battle After Another", 2025),
            LibraryTitle(11050, MediaType.MOVIE, "Terms of Endearment", 1983),
        ]
        assert [key.partition("?")[0] for key, _headers in reads] == [
            "/library/sections/1/all",
            "/library/collections/536664/children",
        ], "one listing read and one children read; no per-film re-read"

    def test_collection_members_is_none_when_no_collection_has_that_title(self, mock_plex: PlexClient):
        """Kometa deletes a seasonal collection out of season, so "absent tonight" is a normal answer."""
        self._serve(
            mock_plex,
            {self.LISTING.format(key="1") + "?type=18": self._listing(self._directory(536664, "Something Else"))},
            [("1", "movie", "Movies")],
        )
        assert mock_plex.collection_members("1", "Thanksgiving Movies") is None
        assert mock_plex.collection_members("9", "Something Else") is None, "a library that is gone"

    def test_collection_members_never_reads_a_shortlist_row(self, mock_plex: PlexClient):
        from shortlist.engine.delivery import row_marker

        ours = "🎄 Christmas picks" + row_marker(100)
        reads = self._serve(
            mock_plex,
            {self.LISTING.format(key="1") + "?type=18": self._listing(self._directory(575662, ours))},
            [("1", "movie", "Movies")],
        )
        assert mock_plex.collection_members("1", ours) is None
        assert len(reads) == 1, "its members were never read"

    def test_list_collections_offers_every_library_but_never_a_shortlist_row(self, mock_plex: PlexClient):
        from shortlist.engine.delivery import row_marker

        self._serve(
            mock_plex,
            {
                self.LISTING.format(key="1") + "?type=18": self._listing(
                    self._directory(536664, "Letterboxd Oscars Best Picture Winners"),
                    self._directory(575662, "✨ Movies Picked for You" + row_marker(100)),
                    self._directory(536700, "Christmas Movies", smart=True),
                ),
                self.LISTING.format(key="2") + "?type=18": self._listing(
                    self._directory(600001, "Christmas Specials", subtype="show")
                ),
            },
            [("1", "movie", "Movies"), ("2", "show", "TV Shows")],
        )

        assert mock_plex.list_collections() == [
            LibraryCollection("1", "Movies", "Letterboxd Oscars Best Picture Winners", 4, False, MediaType.MOVIE),
            LibraryCollection("1", "Movies", "Christmas Movies", 4, True, MediaType.MOVIE),
            LibraryCollection("2", "TV Shows", "Christmas Specials", 4, False, MediaType.SHOW),
        ]

    def test_search_titles_asks_the_pms_for_no_more_than_the_limit(self, mock_plex: PlexClient):
        reads = self._serve(
            mock_plex,
            {self.LISTING.format(key="1") + "?title=free": (FIXTURES / "pms_section_title_search.xml.txt").read_text()},
            [("1", "movie", "Movies")],
        )

        found = mock_plex.search_titles("free", limit=3)

        assert found == [
            LibraryTitle(663075, MediaType.MOVIE, "Free Burma Rangers", 2020),
            LibraryTitle(913824, MediaType.MOVIE, "Free Chol Soo Lee", 2022),
            LibraryTitle(334521, MediaType.MOVIE, "Free Fire", 2016),
        ]
        assert [headers["X-Plex-Container-Size"] for _key, headers in reads] == ["3"]

    def test_search_titles_skips_a_title_with_no_tmdb_guid(self, mock_plex: PlexClient):
        """A film matched by another agent carries other guids but no ``tmdb://`` one; nothing could pick it."""
        recorded = ET.fromstring((FIXTURES / "pms_section_title_search.xml.txt").read_text())
        free_fire = next(video for video in recorded if video.get("title") == "Free Fire")
        free_fire.remove(next(guid for guid in free_fire.findall("Guid") if guid.get("id").startswith("tmdb://")))
        self._serve(
            mock_plex,
            {self.LISTING.format(key="1") + "?title=free": ET.tostring(recorded, encoding="unicode")},
            [("1", "movie", "Movies")],
        )

        found = mock_plex.search_titles("free", limit=5)

        assert [title.title for title in found] == ["Free Burma Rangers", "Free Chol Soo Lee", "Free Guy", "Free Solo"]
