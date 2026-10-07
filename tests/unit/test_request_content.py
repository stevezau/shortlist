"""The optional content policy guards both automatic allocation and inbox sends."""

from unittest.mock import Mock

import pytest

from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import MediaType, MissingTitle, RequestConfig, RequestOverrides, SeerrTarget
from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.request_content import music_nonfiction_reason
from tests.unit.test_requests import RADARR, FakeArr, FakeTmdb, _cfg, _request_missing


@pytest.mark.parametrize(
    ("genres", "keywords", "blocked"),
    [
        ([10402, 99], [], True),  # Music documentary
        ([10402, 99, 16], [], True),  # Animation doesn't make a documentary fiction
        ([10402], [6029], True),  # Concert
        ([10402], [11634], True),  # Live performance
        ([10402], [156205], True),  # Concert film
        ([10402], [162066], True),  # Rock concert
        ([99], [246377], True),  # Music documentary without Music genre
        ([99], [156205], True),
        ([10402, 18], [6029], False),  # Fictional musical with concert scene
        ([10402, 35], [11634], False),
        ([18], [], False),
        ([99], [], False),  # Non-music documentary
        ([10402], [], False),  # Music alone isn't enough
    ],
)
def test_policy_uses_genres_and_keywords_without_title_matching(genres, keywords, blocked):
    tmdb = Mock()
    tmdb.details.return_value = {"genres": [{"id": g} for g in genres] if genres is not None else None}
    tmdb.keyword_ids_for.return_value = set(keywords)
    title = MissingTitle(100, "Neutral title", MediaType.MOVIE, 2020, 8.0, 500)
    assert bool(music_nonfiction_reason(tmdb, title)) is blocked


@pytest.mark.parametrize("genres", [[], None])
def test_unknown_genres_hold_the_movie(genres):
    tmdb = Mock()
    tmdb.details.return_value = {"genres": [{"id": g} for g in genres] if genres is not None else None}
    reason = music_nonfiction_reason(tmdb, MissingTitle(100, "Neutral title", MediaType.MOVIE, 2020, 8.0, 500))
    assert "metadata unavailable" in reason


@pytest.mark.parametrize("genres", [[{"id": 10402}, {"name": "Documentary"}], [{"id": "10402"}], [None]])
def test_partially_malformed_genre_payload_is_held_instead_of_silently_cleaned(genres):
    tmdb = Mock()
    tmdb.details.return_value = {"genres": genres}
    tmdb.keyword_ids_for.return_value = set()
    title = MissingTitle(100, "Neutral title", MediaType.MOVIE, 2020, 8.0, 500)
    assert "metadata unavailable" in music_nonfiction_reason(tmdb, title)
    tmdb.keyword_ids_for.assert_not_called()


def test_lookup_failure_holds_only_that_movie_and_does_not_expose_exception_details():
    tmdb = Mock()
    tmdb.details.side_effect = RuntimeError("sensitive diagnostic")
    reason = music_nonfiction_reason(tmdb, MissingTitle(100, "Neutral title", MediaType.MOVIE, 2020, 8.0, 500))
    assert "metadata unavailable" in reason
    assert "sensitive" not in reason


def test_shows_do_not_need_movie_metadata():
    tmdb = Mock()
    assert music_nonfiction_reason(tmdb, MissingTitle(100, "Neutral title", MediaType.SHOW, 2020, 8.0, 500)) == ""
    tmdb.details.assert_not_called()


def test_row_overrides_preserve_the_global_content_restriction():
    cfg = _cfg(exclude_music_nonfiction=True)
    assert resolve_request_config(cfg, RequestOverrides(min_rating=8.5)).exclude_music_nonfiction is True


def test_missing_keywords_hold_a_documentary_but_allow_a_fictional_musical():
    tmdb = Mock()
    tmdb.details.return_value = {"genres": [{"id": 99}]}
    tmdb.keyword_ids_for.side_effect = ValueError("unavailable")
    title = MissingTitle(100, "Neutral title", MediaType.MOVIE, 2020, 8.0, 500)
    assert "metadata unavailable" in music_nonfiction_reason(tmdb, title)
    tmdb.details.return_value = {"genres": [{"id": 10402}, {"id": 18}]}
    assert music_nonfiction_reason(tmdb, title) == ""


def test_keyword_client_reads_the_public_cached_movie_endpoint(monkeypatch):
    client = TmdbClient.__new__(TmdbClient)
    get = Mock(return_value={"id": 100, "keywords": [{"id": 6029, "name": "concert"}]})
    monkeypatch.setattr(client, "_get", get)
    assert client.keyword_ids_for(100) == {6029}
    get.assert_called_once_with("/movie/100/keywords")


def test_missing_keyword_payload_is_unknown_not_an_empty_success(monkeypatch):
    client = TmdbClient.__new__(TmdbClient)
    monkeypatch.setattr(client, "_get", Mock(return_value={}))
    with pytest.raises(ValueError):
        client.keyword_ids_for(100)


def _metadata_tmdb():
    tmdb = FakeTmdb()
    tmdb.details = Mock(
        side_effect=lambda tid, media: {"genres": [{"id": g} for g in ([10402, 99] if tid == 1 else [18])]}
    )
    tmdb.keyword_ids_for = Mock(return_value=set())
    return tmdb


def test_blocked_movie_does_not_take_an_auto_send_slot(monkeypatch):
    arr = FakeArr()
    monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
    cfg = _cfg(radarr=RADARR, max_per_run=1, exclude_music_nonfiction=True)
    movies = [
        MissingTitle(1, "Neutral A", MediaType.MOVIE, 2020, rating=9.5, vote_count=500, demand=5),
        MissingTitle(2, "Neutral B", MediaType.MOVIE, 2020, rating=8.5, vote_count=500, demand=3),
    ]
    report = _request_missing(cfg, _metadata_tmdb(), {(m.tmdb_id, m.media_type): m for m in movies}, dry_run=False)
    assert arr.movie_calls == [(2, False)]
    assert [m.tmdb_id for m in report.queued] == [1]
    assert report.queued[0].detail.startswith("music content filter:")


def test_final_metadata_hold_stays_in_the_automatic_inbox(monkeypatch):
    arr = FakeArr()
    monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
    tmdb = _metadata_tmdb()
    tmdb.details.side_effect = [{"genres": [{"id": 18}]}, RuntimeError("unavailable")]
    title = MissingTitle(2, "Neutral B", MediaType.MOVIE, 2020, 8.5, 500, demand=3)
    cfg = _cfg(radarr=RADARR, exclude_music_nonfiction=True)
    report = _request_missing(cfg, tmdb, {(2, MediaType.MOVIE): title}, dry_run=False)
    assert report.sent == []
    assert arr.movie_calls == []
    assert report.queued == [title]
    assert "metadata unavailable" in report.queued[0].detail


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("route", ["arr", "overseerr"])
def test_existing_inbox_movie_is_checked_before_any_downloader_client_is_created(monkeypatch, dry_run, route):
    factory = Mock()
    monkeypatch.setattr(requests_mod, "RadarrClient", factory)
    monkeypatch.setattr(requests_mod, "SeerrClient", factory)
    cfg = _cfg(
        radarr=RADARR if route == "arr" else None,
        overseerr=SeerrTarget("http://seerr.test", "sk") if route == "overseerr" else None,
        exclude_music_nonfiction=True,
    )
    title = MissingTitle(1, "Neutral A", MediaType.MOVIE, 2020, 8.0, 500)
    report = requests_mod.request_titles_by_row({"picked": cfg}, _metadata_tmdb(), [("picked", title)], dry_run=dry_run)
    assert report.outcomes[0].status == "skipped_content"
    assert report.outcomes[0].detail.startswith("music content filter:")
    factory.assert_not_called()


def test_disabled_policy_preserves_requests_without_additional_metadata_calls(monkeypatch):
    arr = FakeArr()
    monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
    tmdb = _metadata_tmdb()
    title = MissingTitle(1, "Neutral A", MediaType.MOVIE, 2020, 8.0, 500)
    cfg = _cfg(radarr=RADARR)
    requests_mod.request_titles_by_row({"picked": cfg}, tmdb, [("picked", title)], dry_run=False)
    assert RequestConfig().exclude_music_nonfiction is False
    assert arr.movie_calls == [(1, False)]
    tmdb.details.assert_not_called()


def test_blocked_movie_never_reserves_a_durable_acquisition_claim(monkeypatch):
    guard = Mock(side_effect=AssertionError("blocked content reserved an acquisition"))
    factory = Mock()
    monkeypatch.setattr(requests_mod, "RadarrClient", factory)
    title = MissingTitle(1, "Neutral A", MediaType.MOVIE, 2020, 8.0, 500)
    cfg = _cfg(radarr=RADARR, exclude_music_nonfiction=True)
    outcomes = requests_mod._send_claims(
        [("picked", title)],
        {"picked": cfg},
        _metadata_tmdb(),
        dry_run=False,
        min_write_interval=0,
        acquisition_guard=guard,
    )
    assert outcomes[0].status == "skipped_content"
    guard.assert_not_called()
    factory.assert_not_called()


def test_allowed_movie_keeps_the_upstream_claim_and_outcome_recording(monkeypatch):
    from contextlib import contextmanager

    arr = FakeArr()
    monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
    recorded = []
    entries = []

    @contextmanager
    def guard(slug, title, cfg):
        entries.append((slug, title.tmdb_id))
        assert arr.movie_calls == []
        yield recorded.append
        assert arr.movie_calls == [(2, False)]

    title = MissingTitle(2, "Neutral B", MediaType.MOVIE, 2020, 8.0, 500)
    cfg = _cfg(radarr=RADARR, exclude_music_nonfiction=True)
    outcomes = requests_mod._send_claims(
        [("picked", title)],
        {"picked": cfg},
        _metadata_tmdb(),
        dry_run=False,
        min_write_interval=0,
        acquisition_guard=guard,
    )
    assert entries == [("picked", 2)]
    assert recorded == outcomes
    assert recorded[0].status == "requested"


def test_metadata_failure_holds_without_sending_and_a_later_retry_can_succeed(monkeypatch):
    arr = FakeArr()
    monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **kw: arr)
    tmdb = _metadata_tmdb()
    tmdb.details.side_effect = RuntimeError("unavailable")
    cfg = _cfg(radarr=RADARR, exclude_music_nonfiction=True)
    title = MissingTitle(1, "Neutral A", MediaType.MOVIE, 2020, 8.0, 500)
    report = requests_mod.request_titles_by_row({"picked": cfg}, tmdb, [("picked", title)], dry_run=False)
    assert "metadata unavailable" in report.outcomes[0].detail
    assert arr.movie_calls == []
    tmdb.details.side_effect = None
    tmdb.details.return_value = {"genres": [{"id": 18}]}
    report = requests_mod.request_titles_by_row({"picked": cfg}, tmdb, [("picked", title)], dry_run=False)
    assert report.outcomes[0].status == "requested"
    assert arr.movie_calls == [(1, False)]
