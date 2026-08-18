"""`PlexClient.fetch_items` — the delivery read, and what it does about titles Plex no longer has.

A title deleted from the library between a pick being made and the row being delivered must not take
the row down. Live on 2026-08-18 (run #17) it did: one person's whole delivery and the shared row
failed while the other 45 users succeeded.

The behaviour relied on is RECORDED, not assumed (plex-safety rule 11):
`tests/fixtures/pms_metadata_batch_partial.json` is a real PMS 1.43.3.10793 response showing a
PARTIAL batch returns 200 with the found subset. Only an ALL-missing batch 404s. An earlier version
of this fix believed a partial batch raised and re-fetched key by key to recover survivors — a loop
that could never recover anything, since a 404 means nothing was found. The repo's own fake_plex had
it right and the docstring was fiction.

The method returns `(items, missing)` because a partial miss is otherwise SILENT, and a caller that
cannot see it reports having delivered titles the row does not contain.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from plexapi.exceptions import BadRequest, NotFound

from shortlist.engine.clients import plex_pms
from shortlist.engine.clients.plex_pms import PlexClient


def _client(server) -> PlexClient:
    client = PlexClient.__new__(PlexClient)
    client._server = server
    return client


def _item(rating_key: int) -> MagicMock:
    item = MagicMock()
    item.ratingKey = rating_key
    return item


class TestFetchItemsToleratesVanishedTitles:
    def test_the_happy_path_is_one_batch_call_and_nothing_missing(self):
        server = MagicMock()
        server.fetchItems.return_value = [_item(1), _item(2)]

        items, missing = _client(server).fetch_items([1, 2])

        assert [i.ratingKey for i in items] == [1, 2]
        assert missing == []
        server.fetchItems.assert_called_once_with([1, 2])

    def test_a_partial_batch_reports_what_went(self):
        """The recorded shape: 200, dead key simply absent. No exception — which is why the per-item
        recovery loop this replaced was dead code, and why the caller must be TOLD."""
        server = MagicMock()
        server.fetchItems.return_value = [_item(1)]

        items, missing = _client(server).fetch_items([1, 99])

        assert [i.ratingKey for i in items] == [1]
        assert missing == [99]

    def test_every_key_gone_returns_nothing_rather_than_raising(self):
        """`to_add_keys` is the DELTA, so on a steady night whose only change was a deletion the
        delta IS the dead keys — an all-missing batch, which is what failed run #17."""
        server = MagicMock()
        server.fetchItems.side_effect = NotFound("404")

        assert _client(server).fetch_items([1, 2]) == ([], [1, 2])

    def test_an_empty_delta_costs_no_round_trip(self):
        server = MagicMock()

        assert _client(server).fetch_items([]) == ([], [])
        server.fetchItems.assert_not_called()

    def test_a_real_outage_still_raises(self):
        """401 maps to Unauthorized and 5xx to BadRequest — never NotFound. A tidied library and a
        dead server must not look alike, or a row would silently deliver empty during an outage."""
        server = MagicMock()
        server.fetchItems.side_effect = BadRequest("500")

        with pytest.raises(BadRequest):
            _client(server).fetch_items([1, 2])

    def test_the_drop_is_logged_with_the_keys(self):
        """loguru, not stdlib logging, so `caplog` sees nothing — this uses the sink pattern the rest
        of the suite uses (test_arr.py, test_clients.py)."""
        server = MagicMock()
        server.fetchItems.return_value = [_item(1)]

        lines: list[str] = []
        sink = plex_pms.logger.add(lines.append, level="WARNING", format="{message}")
        try:
            _client(server).fetch_items([1, 99])
        finally:
            plex_pms.logger.remove(sink)

        assert any("99" in line and "no longer exist" in line for line in lines), lines
