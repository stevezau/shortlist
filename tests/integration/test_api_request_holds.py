"""The "don't request these automatically" settings and their live preview against the inbox."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.models import MediaType
from shortlist.server.db.models import RequestCandidate

pytestmark = pytest.mark.integration

DOCUMENTARY, MUSIC, DRAMA, ROMANCE = 99, 10402, 18, 10749
CONCERT, CONCERT_FILM = 6029, 156205
REALITY, TALK, ANIMATION, ANIME = 10764, 10767, 16, 210024
A24, NETFLIX_STUDIO = 41077, 171251

# What TMDB says about these real titles (read 2026-10-09).
_GENRES = {1: [DOCUMENTARY, MUSIC], 2: [MUSIC, DRAMA, ROMANCE], 3: [DRAMA], 4: [DOCUMENTARY, MUSIC]}
_TAGS = {1: {CONCERT: "concert", CONCERT_FILM: "concert film"}, 2: {CONCERT: "concert"}, 3: {}, 4: {}}
_TITLES = {1: "BTS: Permission to Dance on Stage", 2: "A Star Is Born", 3: "A drama", 4: "Amy"}
_STUDIOS = {1: [], 2: [], 3: [], 4: [A24]}
# The inbox's shows, by the TV endpoints: their own genre ids, and tags listed under `results`.
_SHOW_GENRES = {1: [REALITY], 5: [ANIMATION], 6: [ANIMATION, DRAMA]}
_SHOW_TAGS = {1: {}, 5: {ANIME: "anime"}, 6: {ANIME: "anime"}}
_SHOW_STUDIOS = {1: [], 5: [], 6: [NETFLIX_STUDIO]}
_SHOWS = {1: "A reality show", 5: "An anime", 6: "A prestige anime"}
_SHOW_RATINGS = {1: 6.0, 5: 7.2, 6: 8.9}


def _fake_tmdb(fail_on: int | None = None) -> Mock:
    names = {DOCUMENTARY: "Documentary", MUSIC: "Music", DRAMA: "Drama", ROMANCE: "Romance"}
    names |= {REALITY: "Reality", ANIMATION: "Animation"}
    studios = {A24: "A24", NETFLIX_STUDIO: "Netflix Studios"}

    def details(tmdb_id, media_type):
        if tmdb_id == fail_on:
            raise RuntimeError("TMDB API error HTTP 503")
        show = media_type is MediaType.SHOW
        genres, companies = (_SHOW_GENRES, _SHOW_STUDIOS) if show else (_GENRES, _STUDIOS)
        return {
            "genres": [{"id": g, "name": names[g]} for g in genres[tmdb_id]],
            "production_companies": [{"id": c, "name": studios[c]} for c in companies[tmdb_id]],
        }

    tmdb = Mock()
    tmdb.details.side_effect = details
    tmdb.movie_keywords.side_effect = lambda tmdb_id: _TAGS[tmdb_id]
    tmdb.show_keywords.side_effect = lambda tmdb_id: _SHOW_TAGS[tmdb_id]
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
        for tmdb_id, title in _SHOWS.items():
            session.add(
                RequestCandidate(
                    tmdb_id=tmdb_id,
                    media_type="show",
                    title=title,
                    status="pending",
                    demand=9 - tmdb_id,
                    rating=_SHOW_RATINGS[tmdb_id],
                )
            )
        # Never previewed: a sent title and a hidden one.
        session.add(RequestCandidate(tmdb_id=7, media_type="movie", title="Sent", status="sent", demand=9))
        session.add(RequestCandidate(tmdb_id=8, media_type="movie", title="Hidden", status="pending", hidden=True))
        session.commit()
    return client


def _preview(client: TestClient, monkeypatch, *, genres=(), tags=(), tmdb=None, **extra):
    tmdb = tmdb or _fake_tmdb()
    monkeypatch.setattr(client.app.state.run_service, "build_tmdb_only", lambda: tmdb)
    body = {"genres": list(genres), "tags": list(tags), **extra}
    response = client.post("/api/requests/hold-preview", json=body)
    assert response.status_code == 200, response.text
    return response.json()


class TestHoldSettings:
    def test_every_pick_defaults_to_holding_nothing(self, client: TestClient):
        settings = client.get("/api/settings").json()
        for prefix in ("requests.hold_", "requests.hold_show_"):
            assert settings[f"{prefix}genres"] == []
            assert settings[f"{prefix}tags"] == {}
            assert settings[f"{prefix}except_companies"] == {}
            assert settings[f"{prefix}except_min_rating"] is None

    def test_show_picks_and_exceptions_save(self, client: TestClient):
        values = {
            "requests.hold_show_genres": [REALITY, TALK],
            "requests.hold_show_tags": {"210024": "anime"},
            "requests.hold_show_except_companies": {"171251": "Netflix Studios"},
            "requests.hold_show_except_min_rating": 8.5,
            "requests.hold_except_companies": {"41077": "A24"},
            "requests.hold_except_min_rating": 0,
        }
        assert client.put("/api/settings", json={"values": values}).status_code == 200
        settings = client.get("/api/settings").json()
        assert {key: settings[key] for key in values} == values

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            # Action and Sci-Fi are movie genre ids: TMDB gives shows Action & Adventure (10759) and
            # Sci-Fi & Fantasy (10765) instead, so a movie id would match no show while reading as a pick.
            ("requests.hold_show_genres", [28]),
            ("requests.hold_show_genres", [878]),
            # ...and the reverse: Reality is a TV genre only.
            ("requests.hold_genres", [REALITY]),
            ("requests.hold_show_tags", ["anime"]),
            ("requests.hold_except_companies", {"A24": "A24"}),
            ("requests.hold_show_except_companies", {"41077": ""}),
            ("requests.hold_except_min_rating", 11),
            ("requests.hold_show_except_min_rating", -1),
        ],
    )
    def test_a_malformed_show_pick_or_exception_is_refused(self, client: TestClient, key, value):
        response = client.put("/api/settings", json={"values": {key: value}})
        assert response.status_code == 422, response.text

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
            {
                "tmdb_id": 1,
                "title": _TITLES[1],
                "year": None,
                "reason": "tag “concert film”",
                "story": False,
                "companies": [],
            }
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
        assert result == {"checked": 4, "held": [], "unread": 0, "excepted": []}
        tmdb.details.assert_not_called()

    def test_a_title_tmdb_cannot_read_is_counted_not_shown_as_held(self, inbox: TestClient, monkeypatch):
        result = _preview(inbox, monkeypatch, genres=[DOCUMENTARY], tmdb=_fake_tmdb(fail_on=1))
        assert result["unread"] == 1
        assert [h["tmdb_id"] for h in result["held"]] == [4]

    def test_no_tmdb_key_is_a_clear_503(self, inbox: TestClient, monkeypatch):
        monkeypatch.setattr(inbox.app.state.run_service, "build_tmdb_only", lambda: None)
        response = inbox.post("/api/requests/hold-preview", json={"genres": [DOCUMENTARY], "tags": []})
        assert response.status_code == 503

    def test_a_show_preview_reads_the_shows_waiting_with_the_tv_endpoints(self, inbox: TestClient, monkeypatch):
        tmdb = _fake_tmdb()
        result = _preview(inbox, monkeypatch, tmdb=tmdb, media_type="show", tags=[ANIME])
        assert result["checked"] == 3
        assert [(h["tmdb_id"], h["reason"], h["story"]) for h in result["held"]] == [
            (5, "tag “anime”", False),
            (6, "tag “anime”", False),
        ]
        assert result["excepted"] == []
        tmdb.movie_keywords.assert_not_called()

    def test_movie_picks_preview_only_movies_and_show_picks_only_shows(self, inbox: TestClient, monkeypatch):
        """TMDB id 1 is in the inbox as a movie and as a show: each panel must see only its own."""
        movies = _preview(inbox, monkeypatch, genres=[DOCUMENTARY])
        shows = _preview(inbox, monkeypatch, media_type="show", genres=[REALITY])
        assert [(h["tmdb_id"], h["title"]) for h in movies["held"]] == [(1, _TITLES[1]), (4, _TITLES[4])]
        assert [(h["tmdb_id"], h["title"]) for h in shows["held"]] == [(1, _SHOWS[1])]

    def test_a_studio_exception_moves_a_match_to_the_sent_anyway_list(self, inbox: TestClient, monkeypatch):
        result = _preview(inbox, monkeypatch, genres=[DOCUMENTARY], except_companies=[A24])
        assert [h["tmdb_id"] for h in result["held"]] == [1]
        assert result["excepted"] == [
            {"tmdb_id": 4, "title": _TITLES[4], "year": None, "reason": "genre Documentary", "exception": "studio A24"}
        ]

    def test_a_studio_list_tmdb_cannot_give_leaves_the_match_held(self, inbox: TestClient, monkeypatch):
        """A run holds a title whose exception it cannot check, so the preview must say the same."""
        tmdb = _fake_tmdb()
        real_details = tmdb.details.side_effect

        def details_without_studios(tmdb_id, media_type):
            payload = real_details(tmdb_id, media_type)
            if tmdb_id == 4:
                del payload["production_companies"]
            return payload

        tmdb.details.side_effect = details_without_studios
        result = _preview(inbox, monkeypatch, tmdb=tmdb, genres=[DOCUMENTARY], except_companies=[A24])
        assert [(h["tmdb_id"], h["companies"]) for h in result["held"]] == [(1, []), (4, [])]
        assert result["excepted"] == []
        assert result["unread"] == 0

    def test_a_rating_exception_reads_the_rating_the_inbox_recorded(self, inbox: TestClient, monkeypatch):
        result = _preview(inbox, monkeypatch, media_type="show", tags=[ANIME], except_min_rating=8.5)
        assert [h["tmdb_id"] for h in result["held"]] == [5]
        assert [(e["tmdb_id"], e["exception"]) for e in result["excepted"]] == [(6, "rated 8.9")]

    def test_exceptions_alone_preview_nothing(self, inbox: TestClient, monkeypatch):
        tmdb = _fake_tmdb()
        result = _preview(inbox, monkeypatch, tmdb=tmdb, except_companies=[A24], except_min_rating=1.0)
        assert result == {"checked": 4, "held": [], "unread": 0, "excepted": []}
        tmdb.details.assert_not_called()

    def test_an_unknown_media_type_is_refused(self, inbox: TestClient):
        response = inbox.post("/api/requests/hold-preview", json={"media_type": "episode", "genres": [DOCUMENTARY]})
        assert response.status_code == 422

    def test_a_held_title_lists_its_own_studios_to_let_through(self, inbox: TestClient, monkeypatch):
        """TMDB has several companies of one name, so the settings page offers the ones on the title itself."""
        result = _preview(inbox, monkeypatch, media_type="show", tags=[ANIME])
        companies = {h["tmdb_id"]: h["companies"] for h in result["held"]}
        assert companies == {5: [], 6: [{"id": NETFLIX_STUDIO, "name": "Netflix Studios"}]}

    def test_letting_a_listed_studio_through_moves_the_title_to_sent_anyway(self, inbox: TestClient, monkeypatch):
        result = _preview(inbox, monkeypatch, media_type="show", tags=[ANIME], except_companies=[NETFLIX_STUDIO])
        assert [h["tmdb_id"] for h in result["held"]] == [5]
        assert [(e["tmdb_id"], e["exception"]) for e in result["excepted"]] == [(6, "studio Netflix Studios")]


class TestCompanySearch:
    def test_companies_are_tmdbs_answer_with_their_logo(self, client: TestClient, monkeypatch):
        tmdb = Mock()
        tmdb.search_companies.return_value = [
            {"id": A24, "name": "A24", "country": "US", "logo_path": "/a24.png"},
            {"id": 7, "name": "A24 Films", "country": "", "logo_path": ""},
        ]
        monkeypatch.setattr(client.app.state.run_service, "build_tmdb_only", lambda: tmdb)
        assert client.get("/api/requests/tmdb-companies?q=%20a24%20").json() == [
            {"id": A24, "name": "A24", "country": "US", "logo_path": "/a24.png"},
            {"id": 7, "name": "A24 Films", "country": "", "logo_path": ""},
        ]
        tmdb.search_companies.assert_called_once_with("a24")

    def test_a_one_letter_query_asks_nobody(self, client: TestClient, monkeypatch):
        tmdb = Mock()
        monkeypatch.setattr(client.app.state.run_service, "build_tmdb_only", lambda: tmdb)
        assert client.get("/api/requests/tmdb-companies?q=a").json() == []
        tmdb.search_companies.assert_not_called()

    def test_no_tmdb_key_is_a_clear_503(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(client.app.state.run_service, "build_tmdb_only", lambda: None)
        response = client.get("/api/requests/tmdb-companies?q=a24")
        assert (response.status_code, response.json()["detail"]) == (503, "Add a TMDB API key in Settings first.")

    def test_a_tmdb_failure_is_a_502_without_the_key(self, client: TestClient, monkeypatch):
        tmdb = Mock()
        tmdb.search_companies.side_effect = RuntimeError("https://api.themoviedb.org/3?api_key=SECRET")
        monkeypatch.setattr(client.app.state.run_service, "build_tmdb_only", lambda: tmdb)
        response = client.get("/api/requests/tmdb-companies?q=a24")
        assert response.status_code == 502
        assert "SECRET" not in response.text
