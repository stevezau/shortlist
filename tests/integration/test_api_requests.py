"""API contract tests: the request inbox size cap."""

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
