"""Unit test for PlexClient.list_owned_collections — the cleanup audit's Plex read.

Pins the ownership filter directly (the endpoint test fakes the whole client): only ``shortlist_``-
labelled collections are returned, foreign (Kometa) and unlabelled ones are skipped, one entry each.
"""

from shortlist.engine.clients.plex_pms import PlexClient
from tests.unit.plex_fakes import FakeColl


class FakeSection:
    def __init__(self, title: str):
        self.title = title


def _client(sections_map: dict) -> PlexClient:
    client = PlexClient.__new__(PlexClient)  # bypass __init__ (no real PlexServer)
    client.sections = lambda: list(sections_map.keys())
    client._section_collections = lambda section: sections_map[section]
    return client


def test_lists_only_shortlist_labelled_collections_one_entry_each():
    movies = FakeSection("Movies")
    tv = FakeSection("TV Shows")
    client = _client(
        {
            movies: [
                FakeColl("Picked for You", ["Shortlist_sarah"], 1),  # Plex title-cases labels
                FakeColl("Kometa Genre", ["kometa"], 2),  # foreign -> excluded
                FakeColl("No labels", [], 3),  # unlabelled -> excluded
            ],
            tv: [FakeColl("Everyone", ["Shortlist__shared_all"], 4)],  # shared row -> included
        }
    )

    out = client.list_owned_collections("shortlist")

    assert {r["title"] for r in out} == {"Picked for You", "Everyone"}  # foreign + unlabelled skipped
    assert next(r for r in out if r["title"] == "Picked for You") == {
        "library": "Movies",
        "title": "Picked for You",
        "label": "Shortlist_sarah",
        "rating_key": 1,
    }
    # Shared collections are returned too (their label starts with the same prefix).
    assert next(r for r in out if r["title"] == "Everyone")["label"] == "Shortlist__shared_all"
