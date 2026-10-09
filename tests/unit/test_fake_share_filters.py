"""The fake PMS applies share filters the way a real one was measured to — rows AND library items.

`tests/fixtures/pms_share_filter_allow_lists.json` settled how a PMS groups a filter (`(A|B)&C`) and
recorded that reading items AS a restricted account leaves out what its filter hides. A fake that shows
every item to everyone would let a pick that the account cannot see pass every end-to-end test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fakes.fake_plex import make_fake_plex, seed_state, share_filter_admits

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "pms_share_filter_allow_lists.json").read_text())


class TestTheEvaluatorMatchesTheRecording:
    def test_the_decisive_grouping_case_shows_nothing(self):
        """`XYZNOPE & (recommended | G)`: nothing is rated XYZNOPE. Left-to-right would show the G movies."""
        raw = next(c["filter"] for c in FIXTURE["grouping"] if c["left_to_right_would_give"] != c["movies_visible"])
        assert share_filter_admits(raw, (), "G") is False
        assert share_filter_admits(raw, ("recommended",), "PG-13") is False

    @pytest.mark.parametrize(
        ("raw", "labels", "rating", "admitted"),
        [
            ("contentRating=G|label=recommended&contentRating=XYZNOPE", (), "G", False),
            ("contentRating=G|label=recommended&label!=Shortlist_x", (), "G", True),
            ("contentRating=G|label=recommended&label!=Shortlist_x", ("recommended",), "PG-13", True),
            ("contentRating=G&label=Shortlist_x", ("Shortlist_x",), "", False),
            ("contentRating=R|label=Shortlist_x&label!=Shortlist_y", ("Shortlist_x", "Shortlist"), "", True),
            ("contentRating=R|label=Shortlist_x&label!=Shortlist_y", ("Shortlist_y", "Shortlist"), "", False),
            ("contentRating!=R|genre=NoSuchGenre&label!=Shortlist_x", ("Shortlist_x",), "", False),
            ("contentRating!=R|genre=NoSuchGenre&label!=Shortlist_x", (), "PG", True),
            ("label=Kids%20Safe", ("Kids Safe",), "", True),
            ("", (), "R", True),
        ],
    )
    def test_recorded_and_derived_shapes(self, raw, labels, rating, admitted):
        assert share_filter_admits(raw, labels, rating) is admitted


class TestReadingAsTheAccount:
    """`GET /library/metadata/{k1,k2,...}` with the account's own token (recorded in `read_as_account`)."""

    @pytest.fixture
    def server(self):
        state = seed_state()
        keys = sorted(state.movies)[:4]
        for i, key in enumerate(keys):
            state.movies[key].content_rating = "R" if i < 2 else "PG-13"
        return state, TestClient(make_fake_plex(state)), keys

    def _read(self, client: TestClient, token: str, keys: list[int]):
        return client.get(f"/library/metadata/{','.join(map(str, keys))}", headers={"X-Plex-Token": token})

    def test_an_allow_list_leaves_out_what_it_hides(self, server):
        state, client, keys = server
        state.users[201].filters["filterMovies"] = "contentRating=R"

        r = self._read(client, "server-201", keys)

        assert r.status_code == 200
        assert _keys(r) == set(keys[:2])

    def test_a_batch_of_only_hidden_items_is_a_404(self, server):
        state, client, keys = server
        state.users[201].filters["filterMovies"] = "contentRating=R"

        assert self._read(client, "server-201", keys[2:]).status_code == 404

    def test_an_exclude_only_filter_hides_no_item(self, server):
        state, client, keys = server
        state.users[201].filters["filterMovies"] = "label!=Shortlist_other"

        r = self._read(client, "server-201", keys)

        assert r.status_code == 200
        assert _keys(r) == set(keys)

    def test_a_library_the_account_is_not_shared_is_left_out_like_a_filtered_item(self, server):
        state, client, keys = server
        state.users[201].shared_sections = {2}  # TV only

        assert self._read(client, "server-201", keys).status_code == 404

    def test_the_owner_sees_everything(self, server):
        state, client, keys = server
        state.users[201].filters["filterMovies"] = "contentRating=R"

        assert _keys(self._read(client, state.owner_token, keys)) == set(keys)


def _keys(response) -> set[int]:
    return {int(k) for k in re.findall(r'ratingKey="(\d+)"', response.text)}
