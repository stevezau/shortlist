"""`PlexClient.fetch_items` — the delivery read, and what it does about titles Plex no longer has.

A title deleted from the library between a pick being made and the row being delivered must not take
the row down. Live on 2026-08-18 (run #17) it did: one person's whole delivery and the shared row
failed while the other 45 users succeeded.

The behaviour this relies on is RECORDED, not assumed (plex-safety rule 11):
`tests/fixtures/pms_metadata_batch_partial.json` is a real PMS 1.43.3.10793 response showing a
PARTIAL batch returns 200 with the found subset. So only an ALL-missing batch 404s. An earlier
version of this fix believed a partial batch raised, and re-fetched key by key to recover the
survivors — a loop that could never have recovered anything, since a 404 means nothing was found.
The repo's own fake_plex had it right and the docstring was fiction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from plexapi.exceptions import BadRequest, NotFound

from shortlist.engine.clients.plex_pms import PlexClient


def _client(server) -> PlexClient:
    client = PlexClient.__new__(PlexClient)
    client._server = server
    return client


class TestFetchItemsToleratesVanishedTitles:
    def test_the_happy_path_is_one_batch_call(self):
        server = MagicMock()
        server.fetchItems.return_value = ["a", "b"]

        assert _client(server).fetch_items([1, 2]) == ["a", "b"]
        server.fetchItems.assert_called_once_with([1, 2])

    def test_a_partial_batch_needs_no_handling_because_plex_returns_the_subset(self):
        """The recorded shape: 200, with the dead key simply absent. There is no exception to catch,
        which is why the per-item recovery loop this replaced was dead code."""
        server = MagicMock()
        server.fetchItems.return_value = ["only-the-live-one"]

        assert _client(server).fetch_items([1, 99]) == ["only-the-live-one"]

    def test_every_key_gone_delivers_without_them_rather_than_raising(self):
        """`to_add_keys` is the DELTA, so on a steady night whose only change was a deletion, the
        delta IS the dead keys — an all-missing batch, which is exactly what failed run #17."""
        server = MagicMock()
        server.fetchItems.side_effect = NotFound("404")

        assert _client(server).fetch_items([1, 2]) == []

    def test_an_empty_delta_costs_no_round_trip(self):
        server = MagicMock()

        assert _client(server).fetch_items([]) == []
        server.fetchItems.assert_not_called()

    def test_a_real_outage_still_raises(self):
        """401 maps to Unauthorized and 5xx to BadRequest — never NotFound. A tidied library and a
        dead server must not look alike, or a row would silently deliver empty during an outage."""
        server = MagicMock()
        server.fetchItems.side_effect = BadRequest("500")

        with pytest.raises(BadRequest):
            _client(server).fetch_items([1, 2])
