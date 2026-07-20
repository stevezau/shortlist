"""Requests inbox API: listing, rejecting, and sending approved titles to Sonarr/Radarr."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortlist.engine import requests as requests_mod
from shortlist.engine.models import ArrTarget, RequestConfig
from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import RequestCandidate, Server
from shortlist.server.main import create_app

pytestmark = pytest.mark.integration

OWNER_ID = 555000001


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as test_client:
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="m1",
                    url="http://pms:32400",
                    token_enc="x",
                    owner_account_id=OWNER_ID,
                    plex_pass=True,
                    capabilities={},
                )
            )
            # Explicit PK ids distinct from tmdb_ids — the API acts on the row id, and keeping them
            # different guards against any id/tmdb_id mix-up.
            session.add(
                RequestCandidate(
                    id=1,
                    tmdb_id=10,
                    media_type="movie",
                    title="Wanted Film",
                    year=2024,
                    rating=8.4,
                    vote_count=1000,
                    demand=2,
                    tags=["kids", "sarah"],  # per-user + per-row tags recorded when it was queued
                    wanters=["Sarah", "Mike"],  # the two people whose picks wanted it
                )
            )
            session.add(
                RequestCandidate(
                    id=2,
                    tmdb_id=20,
                    media_type="show",
                    title="Wanted Show",
                    year=2024,
                    rating=8.9,
                    vote_count=2000,
                    demand=5,
                )
            )
            session.add(
                RequestCandidate(
                    id=3, tmdb_id=30, media_type="movie", title="Sent Film", rating=8.0, vote_count=500, status="sent"
                )
            )
            session.commit()
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ID, "username": "owner"})
        test_client.cookies.set(SESSION_COOKIE, cookie)
        test_client.headers[CSRF_HEADER] = "1"
        yield test_client


class FakeArr:
    def __init__(self):
        self.movie_calls: list[tuple[int, bool]] = []
        self.tag_calls: list[set[str]] = []

    def add_movie(
        self, tmdb_id: int, *, dry_run: bool, extra_tags: set[str] | None = None
    ) -> tuple[str, str, str | None]:
        self.movie_calls.append((tmdb_id, dry_run))
        self.tag_calls.append(set(extra_tags or set()))
        return ("would_request" if dry_run else "requested", "queued in Radarr", f"movie-{tmdb_id}")


class FakeTmdb:
    def tvdb_id(self, tmdb_id: int, media_type) -> int | None:
        return 7777

    def imdb_id(self, tmdb_id: int, media_type) -> str | None:
        return None


def _fake_requests_ctx(cfg: RequestConfig | None):
    """Stand in for RunService.build_requests_context() -> (RequestConfig | None, TmdbClient)."""
    return (cfg, FakeTmdb())


_RADARR = ArrTarget(url="http://radarr", api_key="k", quality_profile_id=1, root_folder="/movies")


class TestRequestsApi:
    def test_requires_owner_session(self, client: TestClient):
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/requests").status_code == 401

    def test_list_is_pending_first_then_most_wanted(self, client: TestClient):
        rows = client.get("/api/requests").json()
        # Pending before sent; within pending, higher demand first.
        assert [r["tmdb_id"] for r in rows] == [20, 10, 30]
        assert rows[-1]["status"] == "sent"

    def test_reject_marks_rejected_and_drops_from_pending(self, client: TestClient):
        assert client.post("/api/requests/reject", json={"ids": [1]}).json()["rejected"] == 1
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["status"] == "rejected"

    def test_delete_removes_the_row_entirely_leaving_no_tombstone(self, client: TestClient):
        # Delete (unlike reject) removes the row outright, so a later run can re-surface the title.
        assert client.post("/api/requests/delete", json={"ids": [1]}).json()["deleted"] == 1
        assert 10 not in {r["tmdb_id"] for r in client.get("/api/requests").json()}
        with client.app.state.sessions() as session:
            assert session.get(RequestCandidate, 1) is None

    def test_delete_lifts_a_rejection_so_it_can_come_back(self, client: TestClient):
        # Reject first (a permanent tombstone), then delete it — the block is gone.
        client.post("/api/requests/reject", json={"ids": [1]})
        assert client.post("/api/requests/delete", json={"ids": [1]}).json()["deleted"] == 1
        with client.app.state.sessions() as session:
            assert session.get(RequestCandidate, 1) is None

    def test_delete_never_removes_a_sent_row(self, client: TestClient):
        # `sent` is a load-bearing tombstone: dropping it would let a still-downloading title be seen
        # as missing and re-requested nightly. A delete of a sent id is a no-op, not a removal.
        assert client.post("/api/requests/delete", json={"ids": [3]}).json()["deleted"] == 0
        with client.app.state.sessions() as session:
            assert session.get(RequestCandidate, 3) is not None  # id 3 is the seeded "Sent Film"

    def test_clear_hides_a_sent_row_but_keeps_the_tombstone(self, client: TestClient):
        # "Clear from log" hides a sent title from the inbox, but the row stays (status still sent,
        # hidden=True) so a still-downloading title can't be seen as missing and re-requested.
        assert client.post("/api/requests/clear", json={"ids": [3]}).json()["cleared"] == 1
        # Gone from the inbox list...
        assert 30 not in {r["tmdb_id"] for r in client.get("/api/requests").json()}
        # ...but still on file as a sent tombstone.
        with client.app.state.sessions() as session:
            row = session.get(RequestCandidate, 3)
            assert row is not None and row.status == "sent" and row.hidden is True
        # Idempotent: clearing an already-hidden row is a no-op (nothing left to hide).
        assert client.post("/api/requests/clear", json={"ids": [3]}).json()["cleared"] == 0

    def test_clear_ignores_a_pending_row(self, client: TestClient):
        # Clear is for the send log only; a pending id (id 2) has Delete/Reject, so clear is a no-op.
        assert client.post("/api/requests/clear", json={"ids": [2]}).json()["cleared"] == 0
        assert 20 in {r["tmdb_id"] for r in client.get("/api/requests").json()}

    def test_restore_moves_a_rejected_title_back_to_pending(self, client: TestClient):
        # "Allow again": a rejected title returns to Waiting immediately, ready to send — not deleted.
        client.post("/api/requests/reject", json={"ids": [1]})
        assert client.post("/api/requests/restore", json={"ids": [1]}).json()["restored"] == 1
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["status"] == "pending"  # back in the waiting queue, metadata intact

    def test_restore_only_touches_rejected_rows(self, client: TestClient):
        # A pending row (id 2) and a sent row (id 3) must be left exactly as they are.
        body = client.post("/api/requests/restore", json={"ids": [2, 3]}).json()
        assert body["restored"] == 0
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[20]["status"] == "pending" and rows[30]["status"] == "sent"

    def test_send_without_requests_configured_returns_409(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(None))
        assert client.post("/api/requests/send", json={"ids": [10]}).status_code == 409

    def test_list_surfaces_stored_tags(self, client: TestClient):
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["tags"] == ["kids", "sarah"]  # what the run recorded, round-tripped to the UI
        assert rows[20]["tags"] == []  # a title queued with no tags carries none

    def test_list_surfaces_who_wanted_each_title(self, client: TestClient):
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["wanters"] == ["Sarah", "Mike"]  # the "who" behind the demand count
        assert rows[20]["wanters"] == []  # a pre-wanters row (or none recorded) carries none

    def test_send_marks_the_title_sent_and_applies_stored_tags(self, client: TestClient, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = RequestConfig(enabled=True, radarr=_RADARR)
        monkeypatch.setattr(client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(cfg))
        body = client.post("/api/requests/send", json={"ids": [1]}).json()
        assert body["sent"] == 1 and fake.movie_calls == [(10, False)]
        assert fake.tag_calls == [{"kids", "sarah"}]  # the queued tags are applied on send
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["status"] == "sent" and rows[10]["detail"] == "queued in Radarr"
        assert rows[10]["arr_slug"] == "movie-10"  # captured at send time -> the inbox deep-links to it

    def test_send_dry_run_previews_without_changing_status(self, client: TestClient, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = RequestConfig(enabled=True, radarr=_RADARR)
        monkeypatch.setattr(client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(cfg))
        body = client.post("/api/requests/send", json={"ids": [1], "dry_run": True}).json()
        assert body["dry_run"] is True and fake.movie_calls == [(10, True)]
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["status"] == "pending"  # a preview leaves the inbox untouched
