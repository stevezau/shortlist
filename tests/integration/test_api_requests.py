"""API contract tests: the request inbox size cap, and the "wanted by" name filter that must
reach past it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


class TestRequestInboxIsBounded:
    """The sent log only grows — every run that wants a missing title adds a row — so an unbounded
    read is a query that gets slower for ever and eventually times out the page."""

    def test_the_inbox_is_capped_and_keeps_pending_first(self, client: TestClient):
        from shortlist.server.api.requests import MAX_INBOX
        from shortlist.server.db.models import RequestCandidate

        with client.app.state.sessions() as session:
            # More sent history than the cap, plus a handful of pending the owner must still see.
            for i in range(MAX_INBOX + 25):
                session.add(
                    RequestCandidate(
                        tmdb_id=1000 + i,
                        media_type="movie",
                        title=f"Sent {i}",
                        status="sent",
                        demand=1,
                        rating=5.0,
                    )
                )
            for i in range(5):
                session.add(
                    RequestCandidate(
                        tmdb_id=1 + i,  # (tmdb_id, media_type) is unique
                        media_type="movie",
                        title=f"Pending {i}",
                        status="pending",
                        demand=9,
                        rating=9.0,
                    )
                )
            session.commit()

        rows = client.get("/api/requests").json()

        assert len(rows) == MAX_INBOX, "the read must be bounded"
        # The cap can only ever truncate the OLDEST history — never the things awaiting a decision.
        assert [r["title"] for r in rows[:5]] == [f"Pending {i}" for i in range(5)]


def _seed_buried_titles(client: TestClient, owner: str, buried: int = 3) -> None:
    """Fill the inbox past :data:`MAX_INBOX` with titles nobody is recorded as wanting, then add
    ``buried`` titles for ``owner`` that sort BELOW the cut (lowest demand, lowest rating).

    That is the shape the filter has to survive: everything ``owner`` wants is past the cap, so a
    filter applied to the response can only ever return an empty list.
    """
    from shortlist.server.api.requests import MAX_INBOX
    from shortlist.server.db.models import RequestCandidate

    with client.app.state.sessions() as session:
        for i in range(MAX_INBOX):
            session.add(
                RequestCandidate(
                    tmdb_id=1000 + i,
                    media_type="movie",
                    title=f"Filler {i}",
                    status="sent",
                    demand=5,
                    rating=8.0,
                    wanters=[],
                )
            )
        for i in range(buried):
            session.add(
                RequestCandidate(
                    tmdb_id=1 + i,  # (tmdb_id, media_type) is unique
                    media_type="movie",
                    title=f"{owner} Buried {i}",
                    status="sent",
                    demand=1,
                    rating=1.0,
                    wanters=[owner, "mike"] if i == 0 else [owner],
                )
            )
        session.commit()


class TestWantedByFilter:
    """ "Wanted by" narrows the inbox to the people named, SERVER-side — the whole point being to
    answer "what does this new person still need?" across the whole history, not just the most
    recent :data:`MAX_INBOX` rows the page happened to load."""

    def test_the_filter_reaches_past_the_cap(self, client: TestClient):
        from shortlist.server.api.requests import MAX_INBOX

        _seed_buried_titles(client, "sarah")

        unfiltered = client.get("/api/requests").json()
        filtered = client.get("/api/requests", params={"wanted_by": "sarah"}).json()

        # Sarah's titles are nowhere on the unfiltered page — the cap ate them.
        assert len(unfiltered) == MAX_INBOX
        assert not [r for r in unfiltered if "sarah" in r["wanters"]]
        # ...and every one of them comes back when she is named, because the filter runs first.
        assert [r["title"] for r in filtered] == [f"sarah Buried {i}" for i in range(3)]

    def test_several_names_are_a_union(self, client: TestClient):
        _seed_buried_titles(client, "sarah")

        filtered = client.get("/api/requests", params={"wanted_by": ["sarah", "mike"]}).json()

        # Mike wanted only the first title, and it is listed once, not twice.
        assert [r["title"] for r in filtered] == [f"sarah Buried {i}" for i in range(3)]

        mike_only = client.get("/api/requests", params={"wanted_by": "mike"}).json()
        assert [r["title"] for r in mike_only] == ["sarah Buried 0"]

    def test_a_name_nobody_carries_returns_nothing(self, client: TestClient):
        """A filter, not a hint: an unmatched name must empty the list rather than fall back to all."""
        _seed_buried_titles(client, "sarah")

        assert client.get("/api/requests", params={"wanted_by": "nobody"}).json() == []

    def test_no_name_is_the_whole_inbox_unchanged(self, client: TestClient):
        """The default must mean exactly what it meant before the parameter existed."""
        from shortlist.server.api.requests import MAX_INBOX

        _seed_buried_titles(client, "sarah")

        # An empty repeated parameter is the same as omitting it — the SPA sends no `wanted_by` at
        # all when no name is picked, but a blank one must not empty the inbox either.
        assert len(client.get("/api/requests").json()) == MAX_INBOX
        assert len(client.get("/api/requests", params={"wanted_by": ""}).json()) == MAX_INBOX

    def test_the_status_order_survives_the_filter(self, client: TestClient):
        """Filtering must not reorder the inbox: pending is still the owner's to-do list, on top."""
        from shortlist.server.db.models import RequestCandidate

        with client.app.state.sessions() as session:
            for i, status in enumerate(("rejected", "sent", "pending")):
                session.add(
                    RequestCandidate(
                        tmdb_id=10 + i,
                        media_type="movie",
                        title=f"Sarah {status}",
                        status=status,
                        demand=1,
                        rating=5.0,
                        wanters=["sarah"],
                    )
                )
            session.commit()

        rows = client.get("/api/requests", params={"wanted_by": "sarah"}).json()

        assert [r["status"] for r in rows] == ["pending", "sent", "rejected"]
