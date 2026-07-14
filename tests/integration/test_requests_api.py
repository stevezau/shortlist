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

    def add_movie(self, tmdb_id: int, *, dry_run: bool) -> tuple[str, str]:
        self.movie_calls.append((tmdb_id, dry_run))
        return ("would_request" if dry_run else "requested", "queued in Radarr")


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

    def test_send_without_requests_configured_returns_409(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(
            client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(None)
        )
        assert client.post("/api/requests/send", json={"ids": [10]}).status_code == 409

    def test_send_marks_the_title_sent(self, client: TestClient, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = RequestConfig(enabled=True, radarr=_RADARR)
        monkeypatch.setattr(
            client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(cfg)
        )
        body = client.post("/api/requests/send", json={"ids": [1]}).json()
        assert body["sent"] == 1 and fake.movie_calls == [(10, False)]
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["status"] == "sent" and rows[10]["detail"] == "queued in Radarr"

    def test_send_dry_run_previews_without_changing_status(self, client: TestClient, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = RequestConfig(enabled=True, radarr=_RADARR)
        monkeypatch.setattr(
            client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(cfg)
        )
        body = client.post("/api/requests/send", json={"ids": [1], "dry_run": True}).json()
        assert body["dry_run"] is True and fake.movie_calls == [(10, True)]
        rows = {r["tmdb_id"]: r for r in client.get("/api/requests").json()}
        assert rows[10]["status"] == "pending"  # a preview leaves the inbox untouched
