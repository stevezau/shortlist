"""The "don't request these automatically" settings and their live preview against the inbox."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from shortlist.server.db.models import RequestCandidate

pytestmark = pytest.mark.integration

DOCUMENTARY, MUSIC, DRAMA, ROMANCE = 99, 10402, 18, 10749
CONCERT, CONCERT_FILM = 6029, 156205

# What TMDB says about these real titles (read 2026-10-09).
_GENRES = {1: [DOCUMENTARY, MUSIC], 2: [MUSIC, DRAMA, ROMANCE], 3: [DRAMA], 4: [DOCUMENTARY, MUSIC]}
_TAGS = {1: {CONCERT: "concert", CONCERT_FILM: "concert film"}, 2: {CONCERT: "concert"}, 3: {}, 4: {}}
_TITLES = {1: "BTS: Permission to Dance on Stage", 2: "A Star Is Born", 3: "A drama", 4: "Amy"}


def _fake_tmdb(fail_on: int | None = None) -> Mock:
    names = {DOCUMENTARY: "Documentary", MUSIC: "Music", DRAMA: "Drama", ROMANCE: "Romance"}

    def details(tmdb_id, media_type):
        if tmdb_id == fail_on:
            raise RuntimeError("TMDB API error HTTP 503")
        return {"genres": [{"id": g, "name": names[g]} for g in _GENRES[tmdb_id]]}

    tmdb = Mock()
    tmdb.details.side_effect = details
    tmdb.movie_keywords.side_effect = lambda tmdb_id: _TAGS[tmdb_id]
    return tmdb


@pytest.fixture
def inbox(client: TestClient):
    with client.app.state.sessions() as session:
        for tmdb_id, title in _TITLES.items():
            session.add(
                RequestCandidate(
                    tmdb_id=tmdb_id, media_type="movie", title=title, status="pending", demand=5 - tmdb_id, rating=8.0
                )
            )
        # Never previewed: a show (the picks are movie genres), a sent title, and a hidden one.
        session.add(RequestCandidate(tmdb_id=1, media_type="show", title="A show", status="pending", demand=9))
        session.add(RequestCandidate(tmdb_id=7, media_type="movie", title="Sent", status="sent", demand=9))
        session.add(RequestCandidate(tmdb_id=8, media_type="movie", title="Hidden", status="pending", hidden=True))
        session.commit()
    return client


def _preview(client: TestClient, monkeypatch, *, genres=(), tags=(), tmdb=None):
    tmdb = tmdb or _fake_tmdb()
    monkeypatch.setattr(client.app.state.run_service, "build_tmdb_only", lambda: tmdb)
    response = client.post("/api/requests/hold-preview", json={"genres": list(genres), "tags": list(tags)})
    assert response.status_code == 200, response.text
    return response.json()


class TestHoldSettings:
    def test_both_default_to_holding_nothing(self, client: TestClient):
        settings = client.get("/api/settings").json()
        assert settings["requests.hold_genres"] == []
        assert settings["requests.hold_tags"] == {}

    def test_known_genres_and_well_formed_tags_save(self, client: TestClient):
        values = {"requests.hold_genres": [DOCUMENTARY, MUSIC], "requests.hold_tags": {"156205": "concert film"}}
        assert client.put("/api/settings", json={"values": values}).status_code == 200
        settings = client.get("/api/settings").json()
        assert settings["requests.hold_genres"] == [DOCUMENTARY, MUSIC]
        assert settings["requests.hold_tags"] == {"156205": "concert film"}

    @pytest.mark.parametrize(
        "value",
        [[12345], ["Music"], [True], "99", None, [99, 99]],
        ids=["unknown-id", "a-name", "a-bool", "not-a-list", "null", "duplicate"],
    )
    def test_a_genre_that_is_not_a_tmdb_movie_genre_is_refused(self, client: TestClient, value):
        response = client.put("/api/settings", json={"values": {"requests.hold_genres": value}})
        assert response.status_code == 422, response.text

    @pytest.mark.parametrize(
        "value",
        [["concert"], {"concert": "concert"}, {"0": "x"}, {"156205": ""}, {"156205": 5}, {"156205": "x" * 201}],
        ids=["a-list", "non-numeric-id", "zero-id", "blank-name", "non-text-name", "overlong-name"],
    )
    def test_a_malformed_tag_is_refused(self, client: TestClient, value):
        response = client.put("/api/settings", json={"values": {"requests.hold_tags": value}})
        assert response.status_code == 422, response.text

    def test_too_many_tags_is_refused(self, client: TestClient):
        value = {str(i): f"tag {i}" for i in range(1, 52)}
        assert client.put("/api/settings", json={"values": {"requests.hold_tags": value}}).status_code == 422


class TestHoldPreview:
    def test_a_tag_holds_only_the_movies_carrying_it(self, inbox: TestClient, monkeypatch):
        result = _preview(inbox, monkeypatch, tags=[CONCERT_FILM])
        assert result["checked"] == 4
        assert result["unread"] == 0
        assert result["held"] == [
            {"tmdb_id": 1, "title": _TITLES[1], "year": None, "reason": "tag “concert film”", "story": False}
        ]

    def test_a_broad_tag_flags_the_story_film_it_catches(self, inbox: TestClient, monkeypatch):
        held = _preview(inbox, monkeypatch, tags=[CONCERT])["held"]
        assert [(h["tmdb_id"], h["story"]) for h in held] == [(1, False), (2, True)]

    def test_a_genre_names_itself_as_the_reason(self, inbox: TestClient, monkeypatch):
        held = _preview(inbox, monkeypatch, genres=[DOCUMENTARY])["held"]
        assert [(h["tmdb_id"], h["reason"]) for h in held] == [(1, "genre Documentary"), (4, "genre Documentary")]

    def test_no_picks_reads_nothing_from_tmdb(self, inbox: TestClient, monkeypatch):
        tmdb = _fake_tmdb()
        result = _preview(inbox, monkeypatch, tmdb=tmdb)
        assert result == {"checked": 4, "held": [], "unread": 0}
        tmdb.details.assert_not_called()

    def test_a_title_tmdb_cannot_read_is_counted_not_shown_as_held(self, inbox: TestClient, monkeypatch):
        result = _preview(inbox, monkeypatch, genres=[DOCUMENTARY], tmdb=_fake_tmdb(fail_on=1))
        assert result["unread"] == 1
        assert [h["tmdb_id"] for h in result["held"]] == [4]

    def test_no_tmdb_key_is_a_clear_503(self, inbox: TestClient, monkeypatch):
        monkeypatch.setattr(inbox.app.state.run_service, "build_tmdb_only", lambda: None)
        response = inbox.post("/api/requests/hold-preview", json={"genres": [DOCUMENTARY], "tags": []})
        assert response.status_code == 503
