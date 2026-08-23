"""`PlexClient.play_history` — the server's own play log, parsed from a recorded real response.

Shortlist read exactly one watch signal before this: `unwatched=0` per user, which is Plex's binary
watched flag with a single `lastViewedAt` per title. That cannot say WHEN someone watched something
(a rewatch overwrites the date) and it cannot see a partial play at all.

`tests/fixtures/pms_play_history.xml.txt` is a real `/status/sessions/history/all` response from
PMS 1.43.3.10793 (2026-08-23). Every assertion below is about a shape that server actually produced —
including the two it produces that are easy to design around and wrong: no `grandparentRatingKey`
attribute on episodes, and duplicate rows for one play.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shortlist.engine.clients.plex_pms import PlexClient

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "pms_play_history.xml.txt").read_text()


@pytest.fixture
def client():
    with patch("shortlist.engine.clients.plex_pms.PlexServer"):
        c = PlexClient("http://pms:32400", "tok")
    c._server.url = lambda path, includeToken=True: f"http://pms:32400{path}"
    return c


def _response(text: str = FIXTURE) -> MagicMock:
    r = MagicMock()
    r.text = text
    r.status_code = 200
    return r


class TestParsing:
    def test_a_movie_row_becomes_an_event_with_no_show_key(self, client):
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()):
            events = client.play_history()

        movie = next(e for e in events if e.rating_key == 456294)
        assert movie.plex_account_id == 218833834
        assert movie.media_type == "movie"
        assert movie.show_rating_key is None
        assert movie.viewed_at == datetime.fromtimestamp(1787395686, tz=UTC)

    def test_an_episode_carries_the_shows_key_dug_out_of_grandparentKey(self, client):
        """The whole reason `show_rating_key` exists. A history row has NO `grandparentRatingKey`
        attribute — only `grandparentKey="/library/metadata/592373"` — while a pick for a series
        stores the SHOW's rating key. Measured on 30 days of real history: 46 of 78 matches against
        our picks were reachable ONLY through this mapping, so parsing it wrong loses most of them
        silently."""
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()):
            events = client.play_history()

        episode = next(e for e in events if e.rating_key == 592386)
        assert episode.show_rating_key == 592373, "the show key must come out of grandparentKey's path"
        assert episode.media_type == "episode"

    def test_every_row_keeps_its_own_history_key_even_when_the_play_is_duplicated(self, client):
        """The log emits the same play more than once — these two rows are one play of one film, same
        account, same second, same device, differing only by `historyKey`. That id is what makes
        dedupe exact instead of a guess about how close together is 'the same watch'."""
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()):
            events = client.play_history()

        dupes = [e for e in events if e.rating_key == 618492]
        assert len(dupes) == 2
        assert {e.history_key for e in dupes} == {
            "/status/sessions/history/110516",
            "/status/sessions/history/110517",
        }
        assert len({e.viewed_at for e in dupes}) == 1, "same second — only the id separates them"


class TestRequestShape:
    def test_both_container_headers_are_sent(self, client):
        """`X-Plex-Container-Size` ALONE is ignored by this PMS: the server answers a request for 1000
        rows with the entire log — 101,604 rows, tens of MB. Sending Start as well is what makes it
        honour the page size. Live-probed 2026-08-23."""
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()) as get:
            client.play_history()

        headers = get.call_args.kwargs["headers"]
        assert headers["X-Plex-Container-Start"] == "0"
        assert headers["X-Plex-Container-Size"] == "1000"

    def test_since_is_sent_as_a_viewedAt_filter_not_a_sort(self, client):
        """`viewedAt>` IS honoured here, unlike `lastViewedAt>=` on the library read, which this PMS
        silently ignores (see `watched_titles`). Verified live: 101,604 rows unfiltered, 2,049 for 30
        days, 102 for 24 hours. Sending it as a sort instead would quietly re-read everything."""
        since = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()) as get:
            client.play_history(since=since)

        params = get.call_args.kwargs["params"]
        assert params["viewedAt>"] == int(since.timestamp())
        assert params["sort"] == "viewedAt:desc"

    def test_the_token_never_reaches_the_query_string(self, client):
        """Rule 9: tokens are never logged, and the PMS request logger keeps the path but drops params
        only because the token is in the HEADER. `includeToken=False` on the URL is what guarantees it."""
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()) as get:
            client.play_history()

        assert "X-Plex-Token" not in get.call_args.args[0]
        assert "X-Plex-Token" not in str(get.call_args.kwargs["params"])
        assert get.call_args.kwargs["headers"]["X-Plex-Token"] == "tok"


class TestPaging:
    def test_a_short_page_ends_the_read(self, client):
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()) as get:
            client.play_history()

        assert get.call_count == 1, "5 rows is short of the 1000 page size — nothing more to ask for"

    def test_limit_keeps_the_newest_events_not_the_oldest(self, client):
        """The read is newest-first, so truncation has to drop the far end. A caller that hits the
        limit on a first backfill wants this week, not 2020."""
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response()):
            events = client.play_history(limit=2)

        assert len(events) == 2
        assert events[0].viewed_at > events[1].viewed_at

    def test_a_row_missing_its_account_is_dropped_rather_than_attributed_to_nobody(self, client):
        broken = FIXTURE.replace('accountID="218833834"', 'accountID=""')
        with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=_response(broken)):
            events = client.play_history()

        assert all(e.plex_account_id for e in events)
        assert 456294 not in {e.rating_key for e in events}
