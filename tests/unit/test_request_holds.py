"""The owner's "don't request these automatically" genres and tags (PR #144, generalised).

A held movie is queued for the inbox instead of auto-sent, takes no automatic slot, and stays
sendable by hand: an approval is the owner's own decision, so the filter never blocks it.
"""

from unittest.mock import Mock

import pytest

from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import MediaType, MissingTitle, RequestOverrides
from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.request_holds import HOLD_REASON_PREFIX, hold_reason, is_story_film
from tests.unit.test_requests import RADARR, FakeArr, FakeTmdb, _cfg, _request_missing

MUSIC, DOCUMENTARY, DRAMA, ROMANCE, COMEDY, FAMILY = 10402, 99, 18, 10749, 35, 10751
CONCERT, CONCERT_FILM, STANDUP = 6029, 156205, 9716

# What TMDB says about real titles (read 2026-10-09).
BTS = ([DOCUMENTARY, MUSIC], {CONCERT: "concert", 11634: "live performance", CONCERT_FILM: "concert film"})
A_STAR_IS_BORN = ([MUSIC, DRAMA, ROMANCE], {CONCERT: "concert"})
SOUNDTRACK_TO_A_COUP = ([36, DOCUMENTARY, MUSIC], {})


def _tmdb(genres: list[int] | None, keywords: dict[int, str]) -> Mock:
    tmdb = Mock()
    names = {MUSIC: "Music", DOCUMENTARY: "Documentary", DRAMA: "Drama", ROMANCE: "Romance", 36: "History"}
    tmdb.details.return_value = (
        {} if genres is None else {"genres": [{"id": g, "name": names.get(g, "X")} for g in genres]}
    )
    tmdb.movie_keywords.return_value = keywords
    return tmdb


def _movie(tmdb_id: int = 100, **kw) -> MissingTitle:
    return MissingTitle(tmdb_id, "Neutral title", MediaType.MOVIE, 2020, kw.pop("rating", 8.0), 500, **kw)


class TestHoldReason:
    def test_a_picked_tag_holds_the_movie_and_names_the_tag(self):
        reason = hold_reason(_tmdb(*BTS), _movie(), genres=frozenset(), tags=frozenset({CONCERT_FILM}))
        assert reason == "tag “concert film”"

    def test_a_picked_genre_holds_the_movie_and_names_the_genre(self):
        reason = hold_reason(_tmdb(*BTS), _movie(), genres=frozenset({DOCUMENTARY}), tags=frozenset())
        assert reason == "genre Documentary"

    def test_genres_are_checked_before_tags_and_keywords_are_not_fetched_once_held(self):
        tmdb = _tmdb(*BTS)
        assert hold_reason(tmdb, _movie(), genres=frozenset({MUSIC}), tags=frozenset({CONCERT})) == "genre Music"
        tmdb.movie_keywords.assert_not_called()

    def test_a_musical_without_the_picked_tag_is_not_held(self):
        assert hold_reason(_tmdb(*A_STAR_IS_BORN), _movie(), genres=frozenset(), tags=frozenset({CONCERT_FILM})) == ""

    def test_a_movie_with_none_of_the_picks_is_not_held(self):
        picks = {"genres": frozenset({DOCUMENTARY}), "tags": frozenset({STANDUP})}
        assert hold_reason(_tmdb(*A_STAR_IS_BORN), _movie(), **picks) == ""

    def test_an_empty_genre_list_is_an_answer_not_an_outage(self):
        """TMDB has no genres for some films. The PR held those forever as "metadata unavailable"."""
        assert hold_reason(_tmdb([], {}), _movie(), genres=frozenset({DOCUMENTARY}), tags=frozenset({1})) == ""

    def test_no_picks_reads_nothing_from_tmdb(self):
        tmdb = _tmdb(*BTS)
        assert hold_reason(tmdb, _movie(), genres=frozenset(), tags=frozenset()) == ""
        tmdb.details.assert_not_called()
        tmdb.movie_keywords.assert_not_called()

    def test_genre_only_picks_never_fetch_keywords(self):
        tmdb = _tmdb(*A_STAR_IS_BORN)
        hold_reason(tmdb, _movie(), genres=frozenset({DOCUMENTARY}), tags=frozenset())
        tmdb.movie_keywords.assert_not_called()

    def test_tag_only_picks_never_fetch_details(self):
        tmdb = _tmdb(*BTS)
        assert hold_reason(tmdb, _movie(), genres=frozenset(), tags=frozenset({CONCERT})) == "tag “concert”"
        tmdb.details.assert_not_called()

    def test_shows_are_never_held(self):
        tmdb = _tmdb(*BTS)
        show = MissingTitle(100, "A show", MediaType.SHOW, 2020, 8.0, 500)
        assert hold_reason(tmdb, show, genres=frozenset({DOCUMENTARY}), tags=frozenset({CONCERT})) == ""
        tmdb.details.assert_not_called()

    @pytest.mark.parametrize("broken", ["details", "movie_keywords"])
    def test_a_tmdb_failure_holds_the_movie_without_leaking_the_error(self, broken):
        tmdb = _tmdb(*A_STAR_IS_BORN)
        getattr(tmdb, broken).side_effect = RuntimeError("https://api.themoviedb.org/3?api_key=SECRET")
        reason = hold_reason(tmdb, _movie(), genres=frozenset({DOCUMENTARY}), tags=frozenset({CONCERT_FILM}))
        assert reason.startswith("couldn't read its genres and tags from TMDB")
        assert "SECRET" not in reason

    def test_a_detail_payload_without_genres_is_a_failure_not_an_empty_answer(self):
        """A 404 reads as `{}` from the client: that is not evidence the movie is allowed."""
        reason = hold_reason(_tmdb(None, {}), _movie(), genres=frozenset({DOCUMENTARY}), tags=frozenset())
        assert reason.startswith("couldn't read")


class TestIsStoryFilm:
    @pytest.mark.parametrize(
        ("genres", "story"),
        [
            ([MUSIC, DRAMA, ROMANCE], True),  # A Star Is Born
            ([COMEDY, FAMILY, MUSIC], True),  # School of Rock
            ([DOCUMENTARY, MUSIC], False),  # a concert film
            ([80, DOCUMENTARY], False),  # a crime documentary is still a documentary
            ([COMEDY], False),  # a stand-up special is Comedy alone
            ([MUSIC], False),  # The Eras Tour
        ],
    )
    def test_story_genres_without_documentary(self, genres, story):
        assert is_story_film(genres) is story


class TestKeywordClient:
    def test_reads_the_movie_keywords_endpoint(self, monkeypatch):
        client = TmdbClient.__new__(TmdbClient)
        get = Mock(return_value={"id": 100, "keywords": [{"id": CONCERT, "name": "concert"}]})
        monkeypatch.setattr(client, "_get", get)
        assert client.movie_keywords(100) == {CONCERT: "concert"}
        get.assert_called_once_with("/movie/100/keywords")

    def test_a_movie_with_no_keywords_is_an_empty_answer(self, monkeypatch):
        client = TmdbClient.__new__(TmdbClient)
        monkeypatch.setattr(client, "_get", Mock(return_value={"id": 100, "keywords": []}))
        assert client.movie_keywords(100) == {}

    @pytest.mark.parametrize("payload", [{}, {"keywords": None}, {"keywords": [{"name": "x"}]}])
    def test_a_missing_or_malformed_payload_raises(self, monkeypatch, payload):
        client = TmdbClient.__new__(TmdbClient)
        monkeypatch.setattr(client, "_get", Mock(return_value=payload))
        with pytest.raises(ValueError):
            client.movie_keywords(100)


def _run_tmdb(held_ids: set[int]) -> FakeTmdb:
    """A FakeTmdb where every id in ``held_ids`` is a concert film and everything else a drama."""
    tmdb = FakeTmdb()
    tmdb.details = Mock(
        side_effect=lambda tid, media: {
            "genres": [{"id": g, "name": "G"} for g in ([DOCUMENTARY, MUSIC] if tid in held_ids else [DRAMA])]
        }
    )
    tmdb.movie_keywords = Mock(side_effect=lambda tid: {CONCERT_FILM: "concert film"} if tid in held_ids else {})
    return tmdb


class TestRequestPass:
    def test_a_held_movie_is_queued_with_its_reason_and_takes_no_slot(self, monkeypatch):
        arr = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
        cfg = _cfg(radarr=RADARR, max_per_run=1, hold_tags=frozenset({CONCERT_FILM}))
        movies = [_movie(1, rating=9.5, demand=5), _movie(2, rating=8.5, demand=3)]
        report = _request_missing(cfg, _run_tmdb({1}), {(m.tmdb_id, m.media_type): m for m in movies}, dry_run=False)
        assert arr.movie_calls == [(2, False)]
        assert [m.tmdb_id for m in report.queued] == [1]
        assert report.queued[0].detail == f"{HOLD_REASON_PREFIX} — tag “concert film”"

    def test_the_reason_classifies_as_a_queue_note_not_a_failed_send(self):
        from shortlist.server.services.run_persistence import _is_failure_detail

        assert not _is_failure_detail(f"{HOLD_REASON_PREFIX} — genre Music")

    def test_a_tmdb_outage_holds_the_movie_rather_than_sending_it(self, monkeypatch):
        arr = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
        tmdb = _run_tmdb(set())
        tmdb.details.side_effect = RuntimeError("TMDB API error HTTP 503")
        cfg = _cfg(radarr=RADARR, hold_genres=frozenset({DOCUMENTARY}))
        report = _request_missing(cfg, tmdb, {(1, MediaType.MOVIE): _movie(1)}, dry_run=False)
        assert arr.movie_calls == []
        assert report.queued[0].detail.startswith(f"{HOLD_REASON_PREFIX} — couldn't read")

    def test_no_picks_changes_nothing_and_reads_no_extra_metadata(self, monkeypatch):
        arr = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
        tmdb = _run_tmdb({1})
        _request_missing(_cfg(radarr=RADARR), tmdb, {(1, MediaType.MOVIE): _movie(1)}, dry_run=False)
        assert arr.movie_calls == [(1, False)]
        tmdb.details.assert_not_called()
        tmdb.movie_keywords.assert_not_called()

    def test_auto_send_off_keeps_its_own_reason_and_reads_nothing(self, monkeypatch):
        """Everything waits anyway, so the filter has nothing to decide and costs no TMDB calls."""
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: FakeArr())
        tmdb = _run_tmdb({1})
        cfg = _cfg(radarr=RADARR, auto_send=False, hold_tags=frozenset({CONCERT_FILM}))
        report = _request_missing(cfg, tmdb, {(1, MediaType.MOVIE): _movie(1)}, dry_run=False)
        assert report.queued[0].detail == "auto-send is off"
        tmdb.movie_keywords.assert_not_called()

    def test_an_owner_approval_sends_a_held_movie(self, monkeypatch):
        """The inbox's Send is the exception path: the PR blocked it, so one concert film meant
        switching the whole filter off and back on again."""
        arr = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
        tmdb = _run_tmdb({1})
        cfg = _cfg(radarr=RADARR, hold_tags=frozenset({CONCERT_FILM}))
        report = requests_mod.request_titles_by_row({"picked": cfg}, tmdb, [("picked", _movie(1))], dry_run=False)
        assert report.outcomes[0].status == "requested"
        assert arr.movie_calls == [(1, False)]

    def test_row_overrides_keep_the_global_picks(self):
        cfg = _cfg(hold_genres=frozenset({DOCUMENTARY}), hold_tags=frozenset({CONCERT_FILM}))
        resolved = resolve_request_config(cfg, RequestOverrides(min_rating=8.5))
        assert resolved.hold_genres == frozenset({DOCUMENTARY})
        assert resolved.hold_tags == frozenset({CONCERT_FILM})
