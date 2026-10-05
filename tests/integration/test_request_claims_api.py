# ruff: noqa: F811
"""Real owner routes and server acquisition guards share the same durable claims."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from shortlist.engine.models import MediaType, MissingTitle, RequestConfig
from shortlist.server.assistant.operation_models import AssistantRequestDispatch
from shortlist.server.services.request_actions import AutomaticRequestGuard
from tests.integration.test_requests_api import _RADARR, FakeArr, _fake_requests_ctx, client  # noqa: F401

pytestmark = pytest.mark.integration


def test_owner_send_has_claim_before_vendor_and_blocks_automatic(client, monkeypatch):
    cfg = RequestConfig(enabled=True, radarr=_RADARR)
    monkeypatch.setattr(client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(cfg))
    calls = []

    class GuardedArr(FakeArr):
        def add_movie(self, tmdb_id, **kwargs):
            with client.app.state.sessions() as session:
                claim = session.scalars(select(AssistantRequestDispatch)).one()
                assert claim.origin == "manual" and claim.status == "external_started"
            title = MissingTitle(
                tmdb_id=tmdb_id, media_type=MediaType.MOVIE, title="Wanted Film", year=2024, rating=8, vote_count=1000
            )
            with AutomaticRequestGuard(client.app.state.sessions)("other", title, cfg) as record:
                assert record is None
            calls.append(tmdb_id)
            return super().add_movie(tmdb_id, **kwargs)

    monkeypatch.setattr("shortlist.engine.requests.RadarrClient", lambda *args, **kwargs: GuardedArr())
    response = client.post("/api/requests/send", json={"ids": [1]})
    assert response.status_code == 200 and response.json()["sent"] == 1
    assert calls == [10]
    with client.app.state.sessions() as session:
        assert session.scalars(select(AssistantRequestDispatch)).one().status == "succeeded"


def test_automatic_checkpoint_blocks_owner_send(client, monkeypatch):
    cfg = RequestConfig(enabled=True, radarr=_RADARR)
    monkeypatch.setattr(client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(cfg))
    monkeypatch.setattr(
        "shortlist.engine.requests.RadarrClient", lambda *a, **k: pytest.fail("duplicate client constructed")
    )
    title = MissingTitle(
        tmdb_id=10, media_type=MediaType.MOVIE, title="Wanted Film", year=2024, rating=8, vote_count=1000
    )
    with AutomaticRequestGuard(client.app.state.sessions)("movies", title, cfg) as record:
        assert client.post("/api/requests/send", json={"ids": [1]}).status_code == 409
        record(SimpleNamespace(status="requested", detail="accepted", arr_slug=None))


def test_owner_unknown_outcome_cannot_be_sent_again(client, monkeypatch):
    cfg = RequestConfig(enabled=True, radarr=_RADARR)
    monkeypatch.setattr(client.app.state.run_service, "build_requests_context", lambda: _fake_requests_ctx(cfg))
    calls = []

    class UncertainArr(FakeArr):
        def add_movie(self, tmdb_id, **kwargs):
            calls.append(tmdb_id)
            raise TimeoutError("unknown vendor acceptance")

    monkeypatch.setattr("shortlist.engine.requests.RadarrClient", lambda *args, **kwargs: UncertainArr())
    assert client.post("/api/requests/send", json={"ids": [1]}).json()["sent"] == 0
    assert client.post("/api/requests/send", json={"ids": [1]}).status_code == 409
    assert calls == [10]
    with client.app.state.sessions() as session:
        assert session.scalars(select(AssistantRequestDispatch)).one().status == "outcome_unknown"


def test_owner_recovery_requires_exact_review_and_releases_only_terminal_claim(client):
    with client.app.state.sessions() as session:
        claim = AssistantRequestDispatch(
            origin="manual",
            candidate_id=1,
            destination="http://radarr",
            status="outcome_unknown",
            request_body={"title": {"tmdb_id": 10, "media_type": "movie", "title": "Wanted Film"}},
        )
        session.add(claim)
        session.commit()
    listed = client.get("/api/requests/acquisition-claims").json()["items"][0]
    endpoint = f"/api/requests/acquisition-claims/{listed['id']}/release"
    body = {"review_token": listed["review_token"], "expected_status": "outcome_unknown", "checked_destination": True}
    assert client.post(endpoint, json={**body, "checked_destination": False}).status_code == 422
    assert client.post(endpoint, json={**body, "review_token": "0" * 64}).status_code == 409
    assert client.post(endpoint, json=body).json()["status"] == "released"
    assert client.post(endpoint, json=body).status_code == 409
    assert client.get("/api/requests/acquisition-claims").json()["items"] == []


def test_owner_recovery_rejects_active_claims_and_bearer_auth(client):
    with client.app.state.sessions() as session:
        claim = AssistantRequestDispatch(
            origin="manual",
            candidate_id=1,
            destination="http://radarr",
            status="external_started",
            request_body={"title": {"tmdb_id": 10, "media_type": "movie", "title": "Wanted Film"}},
        )
        session.add(claim)
        session.commit()
    listed = client.get("/api/requests/acquisition-claims").json()["items"][0]
    endpoint = f"/api/requests/acquisition-claims/{listed['id']}/release"
    body = {"review_token": listed["review_token"], "expected_status": "outcome_unknown", "checked_destination": True}
    assert client.post(endpoint, json=body).status_code == 409
    client.headers["Authorization"] = "Bearer any-token"
    assert client.get("/api/requests/acquisition-claims").status_code in {401, 403}
    assert client.post(endpoint, json=body).status_code in {401, 403}
