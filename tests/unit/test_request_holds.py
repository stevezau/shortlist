"""The owner's "don't request these automatically" genres and tags (PR #144, generalised), per media type.

A held title is queued for the inbox instead of auto-sent, takes no automatic slot, and stays
sendable by hand: an approval is the owner's own decision, so the filter never blocks it. Movies and
shows each have their own picks and exceptions; one kind's never touch the other.
"""

from unittest.mock import Mock

import pytest

from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import HoldFilter, MediaType, MissingTitle, RequestOverrides
from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.request_holds import HOLD_REASON_PREFIX, hold_reason, is_story_film
from tests.unit.test_requests import RADARR, SONARR, FakeArr, FakeTmdb, _cfg, _request_missing

MUSIC, DOCUMENTARY, DRAMA, ROMANCE, COMEDY, FAMILY = 10402, 99, 18, 10749, 35, 10751
CONCERT, CONCERT_FILM, STANDUP = 6029, 156205, 9716
# TMDB's TV genres and tags (the TV genre list is its own: Reality and Talk exist only for shows).
REALITY, TALK, ANIMATION, ANIME = 10764, 10767, 16, 210024
A24, NETFLIX_STUDIO = 41077, 171251

# What TMDB says about real titles (read 2026-10-09).
BTS = ([DOCUMENTARY, MUSIC], {CONCERT: "concert", 11634: "live performance", CONCERT_FILM: "concert film"})
A_STAR_IS_BORN = ([MUSIC, DRAMA, ROMANCE], {CONCERT: "concert"})
SOUNDTRACK_TO_A_COUP = ([36, DOCUMENTARY, MUSIC], {})
# An anime series, as TMDB's TV endpoints describe it: genres from the TV list, tags under `results`.
AN_ANIME = ([ANIMATION, 10759], {ANIME: "anime", 10701: "magic"})


def _tmdb(genres: list[int] | None, keywords: dict[int, str], companies: list[int] | None = None) -> Mock:
    tmdb = Mock()
    names = {MUSIC: "Music", DOCUMENTARY: "Documentary", DRAMA: "Drama", ROMANCE: "Romance", 36: "History"}
    names |= {REALITY: "Reality", ANIMATION: "Animation", 10759: "Action & Adventure"}
    studios = {A24: "A24", NETFLIX_STUDIO: "Netflix Studios"}
    tmdb.details.return_value = (
        {}
        if genres is None
        else {
            "genres": [{"id": g, "name": names.get(g, "X")} for g in genres],
            "production_companies": [{"id": c, "name": studios.get(c, "X")} for c in companies or []],
        }
    )
    tmdb.movie_keywords.return_value = keywords
    tmdb.show_keywords.return_value = keywords
    return tmdb


def _movie(tmdb_id: int = 100, **kw) -> MissingTitle:
    return MissingTitle(tmdb_id, "Neutral title", MediaType.MOVIE, 2020, kw.pop("rating", 8.0), 500, **kw)


def _show(tmdb_id: int = 100, **kw) -> MissingTitle:
    return MissingTitle(tmdb_id, "Neutral show", MediaType.SHOW, 2020, kw.pop("rating", 8.0), 500, **kw)


def _holds(genres=(), tags=(), companies=(), min_rating=None) -> HoldFilter:
    return HoldFilter(frozenset(genres), frozenset(tags), frozenset(companies), min_rating)


class TestHoldReason:
    def test_a_picked_tag_holds_the_movie_and_names_the_tag(self):
        reason = hold_reason(_tmdb(*BTS), _movie(), _holds(genres=(), tags={CONCERT_FILM}))
        assert reason == "tag “concert film”"

    def test_a_picked_genre_holds_the_movie_and_names_the_genre(self):
        reason = hold_reason(_tmdb(*BTS), _movie(), _holds(genres={DOCUMENTARY}, tags=()))
        assert reason == "genre Documentary"

    def test_genres_are_checked_before_tags_and_keywords_are_not_fetched_once_held(self):
        tmdb = _tmdb(*BTS)
        assert hold_reason(tmdb, _movie(), _holds(genres={MUSIC}, tags={CONCERT})) == "genre Music"
        tmdb.movie_keywords.assert_not_called()

    def test_a_musical_without_the_picked_tag_is_not_held(self):
        assert hold_reason(_tmdb(*A_STAR_IS_BORN), _movie(), _holds(genres=(), tags={CONCERT_FILM})) == ""

    def test_a_movie_with_none_of_the_picks_is_not_held(self):
        picks = _holds(genres={DOCUMENTARY}, tags={STANDUP})
        assert hold_reason(_tmdb(*A_STAR_IS_BORN), _movie(), picks) == ""

    def test_an_empty_genre_list_is_an_answer_not_an_outage(self):
        """TMDB has no genres for some films. The PR held those forever as "metadata unavailable"."""
        assert hold_reason(_tmdb([], {}), _movie(), _holds(genres={DOCUMENTARY}, tags={1})) == ""

    def test_no_picks_reads_nothing_from_tmdb(self):
        tmdb = _tmdb(*BTS)
        assert hold_reason(tmdb, _movie(), _holds(genres=(), tags=())) == ""
        tmdb.details.assert_not_called()
        tmdb.movie_keywords.assert_not_called()

    def test_genre_only_picks_never_fetch_keywords(self):
        tmdb = _tmdb(*A_STAR_IS_BORN)
        hold_reason(tmdb, _movie(), _holds(genres={DOCUMENTARY}, tags=()))
        tmdb.movie_keywords.assert_not_called()

    def test_tag_only_picks_never_fetch_details(self):
        tmdb = _tmdb(*BTS)
        assert hold_reason(tmdb, _movie(), _holds(genres=(), tags={CONCERT})) == "tag “concert”"
        tmdb.details.assert_not_called()

    @pytest.mark.parametrize("broken", ["details", "movie_keywords"])
    def test_a_tmdb_failure_holds_the_movie_without_leaking_the_error(self, broken):
        tmdb = _tmdb(*A_STAR_IS_BORN)
        getattr(tmdb, broken).side_effect = RuntimeError("https://api.themoviedb.org/3?api_key=SECRET")
        reason = hold_reason(tmdb, _movie(), _holds(genres={DOCUMENTARY}, tags={CONCERT_FILM}))
        assert reason.startswith("couldn't read its genres and tags from TMDB")
        assert "SECRET" not in reason

    def test_a_detail_payload_without_genres_is_a_failure_not_an_empty_answer(self):
        """A 404 reads as `{}` from the client: that is not evidence the movie is allowed."""
        reason = hold_reason(_tmdb(None, {}), _movie(), _holds(genres={DOCUMENTARY}, tags=()))
        assert reason.startswith("couldn't read")


class TestShowHolds:
    """Shows are judged on the TV details and the TV keywords endpoint, with their own picks."""

    def test_a_picked_tv_genre_holds_the_show_read_from_the_tv_details(self):
        tmdb = _tmdb([REALITY], {})
        assert hold_reason(tmdb, _show(), _holds(genres={REALITY, TALK})) == "genre Reality"
        tmdb.details.assert_called_once_with(100, MediaType.SHOW)

    def test_a_picked_tag_holds_the_show_read_from_the_tv_keywords(self):
        tmdb = _tmdb(*AN_ANIME)
        assert hold_reason(tmdb, _show(), _holds(tags={ANIME})) == "tag “anime”"
        tmdb.show_keywords.assert_called_once_with(100)
        tmdb.movie_keywords.assert_not_called()

    def test_a_show_with_none_of_the_picks_is_not_held(self):
        assert hold_reason(_tmdb(*AN_ANIME), _show(), _holds(genres={REALITY}, tags={STANDUP})) == ""

    def test_genre_only_picks_never_fetch_a_shows_keywords(self):
        tmdb = _tmdb(*AN_ANIME)
        assert hold_reason(tmdb, _show(), _holds(genres={REALITY})) == ""
        tmdb.show_keywords.assert_not_called()
        tmdb.movie_keywords.assert_not_called()

    def test_a_tmdb_failure_holds_the_show(self):
        tmdb = _tmdb(*AN_ANIME)
        tmdb.show_keywords.side_effect = RuntimeError("https://api.themoviedb.org/3?api_key=SECRET")
        reason = hold_reason(tmdb, _show(), _holds(tags={ANIME}))
        assert reason.startswith("couldn't read its genres and tags from TMDB")
        assert "SECRET" not in reason


class TestExceptions:
    """A match still goes out on its own when it meets one of its kind's exceptions."""

    @pytest.mark.parametrize("title", [_movie(), _show()], ids=["movie", "show"])
    def test_a_picked_studio_lets_a_match_go(self, title):
        tmdb = _tmdb([DOCUMENTARY], {}, companies=[NETFLIX_STUDIO, A24])
        assert hold_reason(tmdb, title, _holds(genres={DOCUMENTARY}, companies={A24})) == ""

    def test_another_studio_leaves_the_match_held(self):
        tmdb = _tmdb([DOCUMENTARY], {}, companies=[NETFLIX_STUDIO])
        assert hold_reason(tmdb, _movie(), _holds(genres={DOCUMENTARY}, companies={A24})) == "genre Documentary"

    @pytest.mark.parametrize(("rating", "held"), [(8.5, False), (8.4, True)], ids=["at-the-bar", "below-it"])
    def test_a_rating_at_or_above_the_bar_lets_a_match_go(self, rating, held):
        reason = hold_reason(_tmdb(*AN_ANIME), _show(rating=rating), _holds(tags={ANIME}, min_rating=8.5))
        assert reason == ("tag “anime”" if held else "")

    def test_the_rating_exception_reads_nothing_from_tmdb(self):
        """A tag-only filter never fetched details, and a rating exception must not start to."""
        tmdb = _tmdb(*BTS)
        assert hold_reason(tmdb, _movie(rating=9.0), _holds(tags={CONCERT}, min_rating=8.0)) == ""
        tmdb.details.assert_not_called()

    def test_a_studio_exception_reuses_the_detail_payload_the_genre_check_read(self):
        tmdb = _tmdb([DOCUMENTARY], {}, companies=[A24])
        hold_reason(tmdb, _movie(), _holds(genres={DOCUMENTARY}, companies={A24}))
        assert tmdb.details.call_count == 2  # both reads are the client's one cached call
        assert {c.args for c in tmdb.details.call_args_list} == {(100, MediaType.MOVIE)}

    def test_exceptions_alone_hold_nothing_and_read_nothing(self):
        tmdb = _tmdb([DOCUMENTARY], {}, companies=[A24])
        assert hold_reason(tmdb, _movie(), _holds(companies={A24}, min_rating=1.0)) == ""
        tmdb.details.assert_not_called()

    def test_an_unmatched_title_never_reads_its_studios(self):
        tmdb = _tmdb(*A_STAR_IS_BORN)
        assert hold_reason(tmdb, _movie(), _holds(tags={CONCERT_FILM}, companies={A24})) == ""
        tmdb.details.assert_not_called()

    def test_a_failed_studio_read_keeps_the_hold(self):
        """ "Couldn't check the exception" is not a yes, any more than "couldn't check the match" is."""
        tmdb = _tmdb(*BTS)
        tmdb.details.side_effect = RuntimeError("TMDB API error HTTP 503")
        assert hold_reason(tmdb, _movie(), _holds(tags={CONCERT}, companies={A24})) == "tag “concert”"

    def test_a_detail_payload_without_companies_keeps_the_hold(self):
        tmdb = _tmdb(*BTS)
        tmdb.details.return_value = {"genres": []}
        assert hold_reason(tmdb, _movie(), _holds(tags={CONCERT}, companies={A24})) == "tag “concert”"


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

    def test_reads_the_tv_keywords_endpoint_which_lists_them_under_results(self, monkeypatch):
        """`GET /tv/{id}/keywords` answers ``{"id", "results": [...]}`` where the movie one says ``keywords``."""
        client = TmdbClient.__new__(TmdbClient)
        get = Mock(return_value={"id": 100, "results": [{"id": ANIME, "name": "anime"}]})
        monkeypatch.setattr(client, "_get", get)
        assert client.show_keywords(100) == {ANIME: "anime"}
        get.assert_called_once_with("/tv/100/keywords")

    @pytest.mark.parametrize("payload", [{}, {"keywords": [{"id": ANIME, "name": "anime"}]}, {"results": None}])
    def test_a_tv_payload_without_its_results_list_raises(self, monkeypatch, payload):
        """A movie-shaped payload is not a show's answer: reading `keywords` here would hold every show."""
        client = TmdbClient.__new__(TmdbClient)
        monkeypatch.setattr(client, "_get", Mock(return_value=payload))
        with pytest.raises(ValueError):
            client.show_keywords(100)


class TestCompanySearch:
    def test_returns_each_company_with_its_country(self, monkeypatch):
        client = TmdbClient.__new__(TmdbClient)
        results = [
            {"id": A24, "name": "A24", "logo_path": "/a.png", "origin_country": "US"},
            {"id": 7, "name": "A24 Films", "logo_path": None, "origin_country": ""},
            {"name": "no id"},
        ]
        get = Mock(return_value={"page": 1, "results": results})
        monkeypatch.setattr(client, "_get", get)
        assert client.search_companies(" a24 ") == [
            {"id": A24, "name": "A24", "country": "US"},
            {"id": 7, "name": "A24 Films", "country": ""},
        ]
        get.assert_called_once_with("/search/company", params={"query": "a24"})

    def test_a_blank_query_makes_no_call(self, monkeypatch):
        client = TmdbClient.__new__(TmdbClient)
        get = Mock()
        monkeypatch.setattr(client, "_get", get)
        assert client.search_companies("  ") == []
        get.assert_not_called()


def _run_tmdb(held_ids: set[int]) -> FakeTmdb:
    """A FakeTmdb where every id in ``held_ids`` is a concert film (or, as a show, an anime) by A24, and
    everything else a drama. TVDB ids mirror the TMDB ids so Sonarr can take any show."""
    tmdb = FakeTmdb(tvdb={i: 1000 + i for i in range(1, 10)})

    def details(tid: int, media: MediaType) -> dict:
        genres = [DOCUMENTARY, MUSIC] if tid in held_ids else [DRAMA]
        if media is MediaType.SHOW:
            genres = [ANIMATION] if tid in held_ids else [DRAMA]
        return {
            "genres": [{"id": g, "name": "G"} for g in genres],
            "production_companies": [{"id": A24, "name": "A24"}] if tid in held_ids else [],
        }

    tmdb.details = Mock(side_effect=details)
    tmdb.movie_keywords = Mock(side_effect=lambda tid: {CONCERT_FILM: "concert film"} if tid in held_ids else {})
    tmdb.show_keywords = Mock(side_effect=lambda tid: {ANIME: "anime"} if tid in held_ids else {})
    return tmdb


def _arrs(monkeypatch) -> tuple[FakeArr, FakeArr]:
    radarr, sonarr = FakeArr(), FakeArr()
    monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: radarr)
    monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **kw: sonarr)
    return radarr, sonarr


class TestRequestPass:
    def test_a_held_movie_is_queued_with_its_reason_and_takes_no_slot(self, monkeypatch):
        arr = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
        cfg = _cfg(radarr=RADARR, max_per_run=1, hold_movies=_holds(tags={CONCERT_FILM}))
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
        cfg = _cfg(radarr=RADARR, hold_movies=_holds(genres={DOCUMENTARY}))
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
        cfg = _cfg(radarr=RADARR, auto_send=False, hold_movies=_holds(tags={CONCERT_FILM}))
        report = _request_missing(cfg, tmdb, {(1, MediaType.MOVIE): _movie(1)}, dry_run=False)
        assert report.queued[0].detail == "auto-send is off"
        tmdb.movie_keywords.assert_not_called()

    def test_an_owner_approval_sends_a_held_movie(self, monkeypatch):
        """The inbox's Send is the exception path: the PR blocked it, so one concert film meant
        switching the whole filter off and back on again."""
        arr = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
        tmdb = _run_tmdb({1})
        cfg = _cfg(radarr=RADARR, hold_movies=_holds(tags={CONCERT_FILM}))
        report = requests_mod.request_titles_by_row({"picked": cfg}, tmdb, [("picked", _movie(1))], dry_run=False)
        assert report.outcomes[0].status == "requested"
        assert arr.movie_calls == [(1, False)]

    def test_row_overrides_keep_the_global_picks(self):
        movies, shows = _holds(genres={DOCUMENTARY}, tags={CONCERT_FILM}), _holds(tags={ANIME}, min_rating=9.0)
        cfg = _cfg(hold_movies=movies, hold_shows=shows)
        resolved = resolve_request_config(cfg, RequestOverrides(min_rating=8.5))
        assert (resolved.hold_movies, resolved.hold_shows) == (movies, shows)

    def test_a_held_show_is_queued_with_its_reason_and_takes_no_slot(self, monkeypatch):
        _, sonarr = _arrs(monkeypatch)
        cfg = _cfg(sonarr=SONARR, max_per_run=1, hold_shows=_holds(tags={ANIME}))
        shows = [_show(1, rating=9.5, demand=5), _show(2, rating=8.5, demand=3)]
        report = _request_missing(cfg, _run_tmdb({1}), {(m.tmdb_id, m.media_type): m for m in shows}, dry_run=False)
        assert sonarr.series_calls == [(1002, False)]
        assert report.queued[0].detail == f"{HOLD_REASON_PREFIX} — tag “anime”"

    def test_movie_picks_never_hold_a_show_and_show_picks_never_hold_a_movie(self, monkeypatch):
        """Each side's picks name exactly what the OTHER kind's title carries, so only the kind of the
        picks can be what keeps each one moving."""
        radarr, sonarr = _arrs(monkeypatch)
        tmdb = _run_tmdb({1, 2})
        cfg = _cfg(
            radarr=RADARR,
            sonarr=SONARR,
            hold_movies=_holds(genres={ANIMATION}, tags={ANIME}),
            hold_shows=_holds(genres={DOCUMENTARY}, tags={CONCERT_FILM}),
        )
        titles = [_movie(1), _show(2)]
        report = _request_missing(cfg, tmdb, {(m.tmdb_id, m.media_type): m for m in titles}, dry_run=False)
        assert radarr.movie_calls == [(1, False)]
        assert sonarr.series_calls == [(1002, False)]
        assert report.queued == []

    def test_show_picks_alone_read_nothing_for_a_movie(self, monkeypatch):
        radarr, _ = _arrs(monkeypatch)
        tmdb = _run_tmdb({1})
        cfg = _cfg(radarr=RADARR, hold_shows=_holds(tags={ANIME}))
        _request_missing(cfg, tmdb, {(1, MediaType.MOVIE): _movie(1)}, dry_run=False)
        assert radarr.movie_calls == [(1, False)]
        tmdb.movie_keywords.assert_not_called()
        tmdb.show_keywords.assert_not_called()

    @pytest.mark.parametrize(
        "exceptions",
        [{"companies": {A24}}, {"min_rating": 9.0}],
        ids=["by-studio", "by-rating"],
    )
    def test_an_exception_sends_a_matched_title_on_its_own(self, monkeypatch, exceptions):
        radarr, _ = _arrs(monkeypatch)
        cfg = _cfg(radarr=RADARR, hold_movies=_holds(tags={CONCERT_FILM}, **exceptions))
        report = _request_missing(cfg, _run_tmdb({1}), {(1, MediaType.MOVIE): _movie(1, rating=9.2)}, dry_run=False)
        assert radarr.movie_calls == [(1, False)]
        assert report.queued == []

    def test_an_excepted_title_still_has_to_clear_the_auto_bars(self, monkeypatch):
        """The exception lifts the hold, not the rest of the auto-send gate."""
        radarr, _ = _arrs(monkeypatch)
        cfg = _cfg(radarr=RADARR, auto_min_demand=3, hold_movies=_holds(tags={CONCERT_FILM}, companies={A24}))
        report = _request_missing(cfg, _run_tmdb({1}), {(1, MediaType.MOVIE): _movie(1, demand=1)}, dry_run=False)
        assert radarr.movie_calls == []
        assert report.queued[0].detail.startswith("demand below auto_min_demand")
