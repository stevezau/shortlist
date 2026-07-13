"""Request pass: which missing titles get asked for, and how the demand is gated and routed."""

from __future__ import annotations

from rowarr.engine import requests as requests_mod
from rowarr.engine.clients.arr import ArrError
from rowarr.engine.models import (
    ArrTarget,
    Candidate,
    MediaType,
    MissingTitle,
    RequestConfig,
)

RADARR = ArrTarget(url="http://radarr.test", api_key="rk", quality_profile_id=1, root_folder="/movies")
SONARR = ArrTarget(url="http://sonarr.test", api_key="sk", quality_profile_id=1, root_folder="/tv")


def _cand(tmdb_id: int, media: MediaType, *, rating: float = 8.0, votes: int = 500) -> Candidate:
    return Candidate(tmdb_id=tmdb_id, title=f"t{tmdb_id}", media_type=media, rating=rating, vote_count=votes)


class FakeArr:
    """A stand-in Radarr/Sonarr client that records adds and can be told to fail."""

    def __init__(self, *, raise_on: int | None = None):
        self.movie_calls: list[tuple[int, bool]] = []
        self.series_calls: list[tuple[int, bool]] = []
        self.raise_on = raise_on

    def add_movie(self, tmdb_id: int, *, dry_run: bool) -> tuple[str, str]:
        self.movie_calls.append((tmdb_id, dry_run))
        if self.raise_on == tmdb_id:
            raise ArrError("boom")
        return ("would_request" if dry_run else "requested", "ok")

    def add_series(self, tvdb_id: int, *, dry_run: bool) -> tuple[str, str]:
        self.series_calls.append((tvdb_id, dry_run))
        return ("would_request" if dry_run else "requested", "ok")


class FakeTmdb:
    def __init__(
        self,
        tvdb: dict[int, int | None] | None = None,
        *,
        raise_on: int | None = None,
        imdb: dict[int, str | None] | None = None,
    ):
        self._tvdb = tvdb or {}
        self._raise_on = raise_on
        self._imdb = imdb or {}

    def tvdb_id(self, tmdb_id: int, media_type: MediaType) -> int | None:
        if self._raise_on == tmdb_id:
            raise RuntimeError("TMDB API error HTTP 503")  # a non-ArrError, like the real client raises
        return self._tvdb.get(tmdb_id)

    def imdb_id(self, tmdb_id: int, media_type: MediaType) -> str | None:
        return self._imdb.get(tmdb_id, f"tt{tmdb_id:07d}")  # default: every title has a synthetic IMDb id


class FakeOmdb:
    """Stand-in OMDb client returning preset (rating, votes) by IMDb id, counting lookups."""

    def __init__(self, ratings: dict[str, tuple[float, int] | None], *, raise_on: str | None = None):
        self._ratings = ratings
        self._raise_on = raise_on
        self.calls = 0

    def rating(self, imdb_id: str) -> tuple[float, int] | None:
        self.calls += 1
        if imdb_id == self._raise_on:
            raise RuntimeError("OMDb exploded")
        return self._ratings.get(imdb_id, (8.0, 500))  # default: a passing score


class TestCollectMissing:
    def test_keeps_only_titles_absent_from_the_delivery_libraries(self):
        library = {MediaType.MOVIE: {1: 111}, MediaType.SHOW: {}}
        pool = [
            _cand(1, MediaType.MOVIE),  # present -> dropped
            _cand(2, MediaType.MOVIE),  # missing -> kept
            _cand(3, MediaType.SHOW),  # missing -> kept
        ]
        missing = requests_mod.collect_missing(pool, library)
        assert sorted((c.tmdb_id, c.media_type) for c in missing) == [(2, MediaType.MOVIE), (3, MediaType.SHOW)]

    def test_same_id_different_namespace_is_distinct(self):
        # Movie 550 present must not mask show 550 (ids are unique only within a namespace).
        library = {MediaType.MOVIE: {550: 1}, MediaType.SHOW: {}}
        pool = [_cand(550, MediaType.MOVIE), _cand(550, MediaType.SHOW)]
        missing = requests_mod.collect_missing(pool, library)
        assert [(c.tmdb_id, c.media_type) for c in missing] == [(550, MediaType.SHOW)]


class TestAccumulate:
    def test_counts_distinct_wanters(self):
        demand: requests_mod.DemandMap = {}
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)])
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)])  # a second user wants it
        requests_mod.accumulate(demand, [_cand(3, MediaType.SHOW)])
        assert demand[(2, MediaType.MOVIE)].demand == 2
        assert demand[(3, MediaType.SHOW)].demand == 1


class TestRequestMissing:
    def _demand(self, *titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def test_thresholds_exclude_low_rating_or_thin_votes(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "good", MediaType.MOVIE, 2020, rating=8.0, vote_count=500),
            MissingTitle(2, "low rated", MediaType.MOVIE, 2020, rating=6.0, vote_count=500),
            MissingTitle(3, "thin votes", MediaType.MOVIE, 2020, rating=9.0, vote_count=12),
        )
        cfg = RequestConfig(enabled=True, radarr=RADARR, min_rating=7.0, min_votes=100, max_per_run=10)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert report.considered == 1  # only the well-rated, widely-voted title
        assert [c[0] for c in fake.movie_calls] == [1]

    def test_ranks_by_demand_then_rating_and_caps_at_max_per_run(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "one wanter", MediaType.MOVIE, 2020, rating=9.5, vote_count=999, demand=1),
            MissingTitle(2, "three wanters", MediaType.MOVIE, 2020, rating=7.1, vote_count=200, demand=3),
            MissingTitle(3, "two wanters", MediaType.MOVIE, 2020, rating=7.0, vote_count=200, demand=2),
        )
        cfg = RequestConfig(enabled=True, radarr=RADARR, min_rating=7.0, min_votes=100, max_per_run=2)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        # Highest demand first (3 wanters, then 2), capped at 2 — the lone-wanter high score is dropped.
        assert [c[0] for c in fake.movie_calls] == [2, 3]

    def test_routes_movies_to_radarr_and_shows_to_sonarr_via_tvdb(self, monkeypatch):
        radarr, sonarr = FakeArr(), FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: radarr)
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(
            MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500),
            MissingTitle(20, "show", MediaType.SHOW, 2020, rating=8.0, vote_count=500),
        )
        cfg = RequestConfig(enabled=True, radarr=RADARR, sonarr=SONARR, min_rating=7.0, min_votes=100, max_per_run=10)
        tmdb = FakeTmdb({20: 55555})  # the show's TVDB id
        requests_mod.request_missing(cfg, tmdb, demand, dry_run=False)
        assert radarr.movie_calls == [(10, False)]
        assert sonarr.series_calls == [(55555, False)]  # requested by TVDB id, not TMDB id

    def test_show_without_tvdb_is_skipped_not_requested(self, monkeypatch):
        sonarr = FakeArr()
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        cfg = RequestConfig(enabled=True, sonarr=SONARR, min_rating=7.0, min_votes=100, max_per_run=10)
        report = requests_mod.request_missing(cfg, FakeTmdb({20: None}), demand, dry_run=False)
        assert sonarr.series_calls == []
        assert report.outcomes[0].status == "skipped_no_tvdb"

    def test_missing_target_for_media_type_is_skipped(self, monkeypatch):
        # Movies wanted but only Sonarr configured -> skipped_no_target, never an error.
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        cfg = RequestConfig(enabled=True, sonarr=SONARR, min_rating=7.0, min_votes=100, max_per_run=10)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert report.outcomes[0].status == "skipped_no_target"
        assert report.requested == 0

    def test_dry_run_flows_through_to_the_client(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        cfg = RequestConfig(enabled=True, radarr=RADARR, min_rating=7.0, min_votes=100, max_per_run=10)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=True)
        assert fake.movie_calls == [(10, True)]
        assert report.outcomes[0].status == "would_request"

    def test_min_demand_excludes_titles_too_few_people_want(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "one wanter", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=1),
            MissingTitle(2, "two wanters", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=2),
        )
        cfg = RequestConfig(enabled=True, radarr=RADARR, min_rating=7.0, min_votes=100, min_demand=2, max_per_run=10)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [2]  # the lone-wanter title is filtered out

    def test_min_year_excludes_older_titles(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "old", MediaType.MOVIE, 1998, rating=9.0, vote_count=900),
            MissingTitle(2, "new", MediaType.MOVIE, 2021, rating=8.0, vote_count=900),
            MissingTitle(3, "no year", MediaType.MOVIE, None, rating=8.5, vote_count=900),
        )
        cfg = RequestConfig(enabled=True, radarr=RADARR, min_rating=7.0, min_votes=100, min_year=2000, max_per_run=10)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [2]  # 1998 and unknown-year both excluded

    def test_imdb_source_gates_on_omdb_rating_not_tmdb(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        # Both clear TMDB, but only title 2 clears the IMDb floor once OMDb is consulted.
        omdb = FakeOmdb({"tt0000001": (6.2, 5000), "tt0000002": (8.3, 400000)})
        monkeypatch.setattr(requests_mod, "OmdbClient", lambda *a, **k: omdb)
        demand = self._demand(
            MissingTitle(1, "tmdb-hyped", MediaType.MOVIE, 2020, rating=9.0, vote_count=900),
            MissingTitle(2, "imdb-loved", MediaType.MOVIE, 2020, rating=7.5, vote_count=900),
        )
        cfg = RequestConfig(
            enabled=True,
            radarr=RADARR,
            rating_source="imdb",
            omdb_api_key="k",
            min_rating=7.0,
            min_votes=100,
            max_per_run=10,
        )
        tmdb = FakeTmdb(imdb={1: "tt0000001", 2: "tt0000002"})
        report = requests_mod.request_missing(cfg, tmdb, demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [2]
        assert report.considered == 1

    def test_imdb_lookup_failure_drops_only_that_title(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        omdb = FakeOmdb({"tt0000002": (8.5, 900)}, raise_on="tt0000001")
        monkeypatch.setattr(requests_mod, "OmdbClient", lambda *a, **k: omdb)
        demand = self._demand(
            MissingTitle(1, "omdb boom", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=5),
            MissingTitle(2, "fine", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=1),
        )
        cfg = RequestConfig(
            enabled=True,
            radarr=RADARR,
            rating_source="imdb",
            omdb_api_key="k",
            min_rating=7.0,
            min_votes=100,
            max_per_run=10,
        )
        tmdb = FakeTmdb(imdb={1: "tt0000001", 2: "tt0000002"})
        requests_mod.request_missing(cfg, tmdb, demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [2]  # the raising lookup is skipped, the rest survive

    def test_imdb_lookups_are_bounded_to_the_shortlist(self, monkeypatch):
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        omdb = FakeOmdb({})  # every title passes with the default score
        monkeypatch.setattr(requests_mod, "OmdbClient", lambda *a, **k: omdb)
        # 40 qualifying missing titles, but OMDb must be consulted at most the shortlist size.
        demand = self._demand(
            *[
                MissingTitle(i, f"t{i}", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=1)
                for i in range(1, 41)
            ]
        )
        cfg = RequestConfig(
            enabled=True,
            radarr=RADARR,
            rating_source="imdb",
            omdb_api_key="k",
            min_rating=7.0,
            min_votes=100,
            max_per_run=5,
        )
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert omdb.calls <= requests_mod._IMDB_SHORTLIST  # rate-limit guard holds

    def test_imdb_source_without_omdb_key_falls_back_to_tmdb(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=900))
        # rating_source imdb but no key -> gate on TMDB (never silently request nothing).
        cfg = RequestConfig(
            enabled=True,
            radarr=RADARR,
            rating_source="imdb",
            omdb_api_key="",
            min_rating=7.0,
            min_votes=100,
            max_per_run=10,
        )
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [1]

    def test_max_per_run_zero_requests_nothing(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=9.0, vote_count=900))
        cfg = RequestConfig(enabled=True, radarr=RADARR, min_rating=7.0, min_votes=100, max_per_run=0)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []  # the cap of 0 selects nothing, even though a title qualified
        assert report.requested == 0
        assert report.considered == 1  # it still counts as considered — it was gated by the cap, not thresholds

    def test_a_tvdb_lookup_error_becomes_that_titles_outcome_not_a_pass_wide_failure(self, monkeypatch):
        # A TMDB hiccup while resolving one show's TVDB id must not escape and discard the whole
        # report — the movie before it and the recorded show outcome must both survive.
        radarr, sonarr = FakeArr(), FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: radarr)
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(
            MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500),
            MissingTitle(20, "cursed show", MediaType.SHOW, 2020, rating=8.0, vote_count=500),
        )
        cfg = RequestConfig(enabled=True, radarr=RADARR, sonarr=SONARR, min_rating=7.0, min_votes=100, max_per_run=10)
        report = requests_mod.request_missing(cfg, FakeTmdb(raise_on=20), demand, dry_run=False)
        statuses = {o.tmdb_id: o.status for o in report.outcomes}
        assert statuses == {10: "requested", 20: "error"}  # both recorded; the movie still went through
        assert sonarr.series_calls == []  # the failed lookup never reached Sonarr

    def test_one_titles_failure_does_not_stop_the_rest(self, monkeypatch):
        fake = FakeArr(raise_on=10)  # the first title's add raises
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(10, "boom", MediaType.MOVIE, 2020, rating=9.9, vote_count=999, demand=5),
            MissingTitle(11, "fine", MediaType.MOVIE, 2020, rating=8.0, vote_count=500, demand=1),
        )
        cfg = RequestConfig(enabled=True, radarr=RADARR, min_rating=7.0, min_votes=100, max_per_run=10)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        statuses = {o.tmdb_id: o.status for o in report.outcomes}
        assert statuses == {10: "error", 11: "requested"}  # the second still went through
