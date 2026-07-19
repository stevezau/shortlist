"""Request pass: which missing titles get asked for, and how the demand is gated and routed."""

from __future__ import annotations

from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.arr import ArrError
from shortlist.engine.clients.mdblist import MdbListRateLimitError
from shortlist.engine.models import (
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


def _cfg(**kw) -> RequestConfig:
    """A config whose auto-send bar sits on the floor, so every title clearing the base floors is
    auto-sent (never queued). Lets the routing/gating tests below exercise the send path unchanged;
    the hybrid auto-vs-queue split has its own tests in TestHybridSplit.
    """
    defaults = dict(enabled=True, min_rating=7.0, min_votes=100, max_per_run=10, auto_min_demand=1, auto_min_rating=0.0)
    defaults.update(kw)
    return RequestConfig(**defaults)


class FakeArr:
    """A stand-in Radarr/Sonarr client that records adds and can be told to fail."""

    def __init__(
        self,
        *,
        raise_on: int | None = None,
        skip_present: set[int] | None = None,
        present: set[int] | None = None,
        excluded: set[int] | None = None,
        present_tmdb: set[int] | None = None,
    ):
        self.movie_calls: list[tuple[int, bool]] = []
        self.series_calls: list[tuple[int, bool]] = []
        self.tag_calls: list[set[str]] = []  # extra_tags passed on each add, in call order
        self.raise_on = raise_on
        self.skip_present = skip_present or set()
        # Ids the Arr already tracks / has excluded — drives the arr-state reconcile (empty = no-op).
        self._present = present or set()
        self._excluded = excluded or set()
        # As Sonarr: the tracked shows' OWN tmdbIds (v4 payload; empty = v3) for `library_ids`.
        self._present_tmdb = present_tmdb or set()

    # Movies key on tmdbId, shows on tvdbId — the FakeArr just returns whatever id-set it was given,
    # regardless of which accessor, since a test uses one FakeArr per app.
    def library_tmdb_ids(self) -> set[int]:
        return set(self._present)

    def excluded_tmdb_ids(self) -> set[int]:
        return set(self._excluded)

    def library_tvdb_ids(self) -> set[int]:
        return set(self._present)

    def library_ids(self) -> tuple[set[int], set[int]]:
        return set(self._present), set(self._present_tmdb)

    def excluded_tvdb_ids(self) -> set[int]:
        return set(self._excluded)

    def add_movie(self, tmdb_id: int, *, dry_run: bool, extra_tags: set[str] | None = None) -> tuple[str, str]:
        self.movie_calls.append((tmdb_id, dry_run))
        self.tag_calls.append(set(extra_tags or set()))
        if self.raise_on == tmdb_id:
            raise ArrError("boom")
        if tmdb_id in self.skip_present:
            return ("skipped_present", "already in Radarr")
        return ("would_request" if dry_run else "requested", "ok")

    def add_series(self, tvdb_id: int, *, dry_run: bool, extra_tags: set[str] | None = None) -> tuple[str, str]:
        self.series_calls.append((tvdb_id, dry_run))
        self.tag_calls.append(set(extra_tags or set()))
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


class FakeMdbList:
    """Stand-in MDBList client returning preset (rating, votes) by TMDB id, counting lookups.

    ``error_on`` raises a generic error for one title (drops just that title); ``rate_limit_after``
    raises MdbListRateLimitError once that many lookups have happened (drives the TMDB fallback).
    """

    def __init__(
        self,
        ratings: dict[int, tuple[float, int] | None],
        *,
        error_on: int | None = None,
        rate_limit_after: int | None = None,
    ):
        self._ratings = ratings
        self._error_on = error_on
        self._rate_limit_after = rate_limit_after
        self.calls = 0

    def rating(self, tmdb_id: int, media_type: MediaType, source: str) -> tuple[float, int] | None:
        self.calls += 1
        if self._rate_limit_after is not None and self.calls > self._rate_limit_after:
            raise MdbListRateLimitError("quota spent")
        if tmdb_id == self._error_on:
            raise RuntimeError("MDBList hiccup")
        return self._ratings.get(tmdb_id, (8.0, 500))  # default: a passing score


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

    def test_tags_union_across_users_and_dedupe_blanks(self):
        demand: requests_mod.DemandMap = {}
        # Sarah wants it (her tag + a row tag); Mike wants the same title (his tag). The title ends
        # up carrying every contributing tag, and empty strings are dropped, not stored.
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)], tags={"sarah", "kids", ""})
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)], tags={"mike"})
        requests_mod.accumulate(demand, [_cand(3, MediaType.SHOW)], tags=set())
        assert demand[(2, MediaType.MOVIE)].tags == {"sarah", "kids", "mike"}
        assert demand[(3, MediaType.SHOW)].tags == set()  # no tags configured -> stays empty

    def test_wanters_collect_the_usernames_behind_the_demand(self):
        demand: requests_mod.DemandMap = {}
        # Two people want the same title; the inbox needs to show WHO, not just a count of 2.
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)], wanter="Sarah")
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)], wanter="Mike")
        requests_mod.accumulate(demand, [_cand(3, MediaType.SHOW)], wanter="Sarah")
        assert demand[(2, MediaType.MOVIE)].wanters == {"Sarah", "Mike"}
        assert demand[(2, MediaType.MOVIE)].demand == len(demand[(2, MediaType.MOVIE)].wanters)
        assert demand[(3, MediaType.SHOW)].wanters == {"Sarah"}

    def test_why_collects_per_row_provenance_and_dedupes(self):
        from shortlist.engine.models import RequestWhy

        demand: requests_mod.DemandMap = {}
        sarah_comedy = RequestWhy(user="Sarah", row="Comedy Classics", seed="Fawlty Towers", source="tmdb_similar")
        mike_scifi = RequestWhy(user="Mike", row="Sci-Fi Night", seed="Dune", source="trakt")
        # Sarah wants it from one row; Mike from another; Sarah's SAME (row, seed) re-surfaces next run.
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)], wanter="Sarah", why=[sarah_comedy])
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)], wanter="Mike", why=[mike_scifi])
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE)], wanter="Sarah", why=[sarah_comedy])
        why = demand[(2, MediaType.MOVIE)].why
        assert why == [sarah_comedy, mike_scifi]  # both rows kept, the duplicate merged away
        # The provenance is the fuller answer behind the wanters set — same people, more detail.
        assert {w.user for w in why} == demand[(2, MediaType.MOVIE)].wanters


class TestRequestMissing:
    def _demand(self, *titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def test_a_title_already_sent_never_consumes_a_slot_again(self, monkeypatch):
        """A title asked for last night is still downloading — so it is still 'missing', still the
        most-wanted, and used to re-win a request slot EVERY night. With max_per_run=2, the same two
        titles starved the queue forever and nothing new was ever requested."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "still downloading", MediaType.MOVIE, 2020, rating=9.5, vote_count=900, demand=5),
            MissingTitle(2, "also downloading", MediaType.MOVIE, 2020, rating=9.4, vote_count=900, demand=4),
            MissingTitle(3, "new title", MediaType.MOVIE, 2020, rating=8.5, vote_count=900, demand=3),
        )
        cfg = _cfg(radarr=RADARR, max_per_run=2, auto_min_demand=1, auto_min_rating=8.0)

        report = requests_mod.request_missing(
            cfg,
            FakeTmdb(),
            demand,
            dry_run=False,
            already_handled={(1, "movie"), (2, "movie")},
        )

        assert [c[0] for c in fake.movie_calls] == [3]  # the slot went to the NEW title
        assert report.considered == 1

    def test_a_rejected_title_is_never_auto_sent(self, monkeypatch):
        """The owner said no in the inbox. The engine's auto-send never consulted that ledger, so a
        rejected title came back the moment its demand and rating cleared the bar."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(9, "rejected", MediaType.MOVIE, 2020, rating=9.9, vote_count=5000, demand=10),
        )
        cfg = _cfg(radarr=RADARR, auto_min_demand=1, auto_min_rating=8.0)

        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, already_handled={(9, "movie")})

        assert fake.movie_calls == []
        assert report.queued == []  # nor does it clutter the inbox again

    def test_a_handled_movie_does_not_silence_the_show_that_shares_its_id(self, monkeypatch):
        # TMDB ids are unique only within a namespace: movie 550 and show 550 are different titles.
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(550, "the movie", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=3),
            MissingTitle(550, "the show", MediaType.SHOW, 2020, rating=9.0, vote_count=900, demand=3),
        )
        cfg = _cfg(radarr=RADARR, sonarr=SONARR, auto_min_demand=1, auto_min_rating=8.0)

        # The show needs a TVDB id to be requestable at all.
        requests_mod.request_missing(
            cfg, FakeTmdb({550: 1550}), demand, dry_run=False, already_handled={(550, "movie")}
        )

        assert fake.movie_calls == []  # the MOVIE was handled
        assert len(fake.series_calls) == 1  # ...but the show that shares its id was still requested

    def test_thresholds_exclude_low_rating_or_thin_votes(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "good", MediaType.MOVIE, 2020, rating=8.0, vote_count=500),
            MissingTitle(2, "low rated", MediaType.MOVIE, 2020, rating=6.0, vote_count=500),
            MissingTitle(3, "thin votes", MediaType.MOVIE, 2020, rating=9.0, vote_count=12),
        )
        cfg = _cfg(radarr=RADARR)
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
        cfg = _cfg(radarr=RADARR, max_per_run=2)
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
        cfg = _cfg(radarr=RADARR, sonarr=SONARR)
        tmdb = FakeTmdb({20: 55555})  # the show's TVDB id
        requests_mod.request_missing(cfg, tmdb, demand, dry_run=False)
        assert radarr.movie_calls == [(10, False)]
        assert sonarr.series_calls == [(55555, False)]  # requested by TVDB id, not TMDB id

    def test_a_title_already_in_radarr_is_dropped_not_requested(self, monkeypatch):
        # A title Radarr already tracks (or is downloading) isn't really "missing" — it's dropped, not
        # re-requested and not queued to clutter the inbox.
        fake = FakeArr(present={5})
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "have it", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = requests_mod.request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []
        assert report.queued == [] and report.sent == []

    def test_a_show_already_in_sonarr_is_dropped_matched_on_tvdb(self, monkeypatch):
        # Sonarr keys on TVDB, candidates on TMDB — the drop must cross the namespace (the ID gap).
        sonarr = FakeArr(present={55555})
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "have show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        report = requests_mod.request_missing(_cfg(sonarr=SONARR), FakeTmdb({20: 55555}), demand, dry_run=False)
        assert sonarr.series_calls == [] and report.queued == []

    def test_an_excluded_title_is_queued_with_a_reason_never_auto_sent(self, monkeypatch):
        fake = FakeArr(excluded={5})
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "excluded film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = requests_mod.request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []  # the Arr would refuse it, so it's never auto-sent
        assert len(report.queued) == 1
        assert report.queued[0].excluded is True  # surfaced as a flag, not a mislabelled "last attempt"

    def test_an_arr_state_fetch_error_drops_nothing(self, monkeypatch):
        # Fail OPEN: a Radarr hiccup on the presence fetch must not silently drop a wanted title.
        fake = FakeArr(present={5})
        fake.library_tmdb_ids = lambda: (_ for _ in ()).throw(ArrError("radarr down"))
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = requests_mod.request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == [(5, False)] and report.requested == 1

    def test_a_non_arr_error_on_the_state_fetch_also_fails_open(self, monkeypatch):
        # A 200-with-HTML proxy response makes r.json() raise ValueError, not ArrError — still must
        # fail open (request as if the Arr held nothing), never abort the whole pass.
        fake = FakeArr(present={5})
        fake.library_tmdb_ids = lambda: (_ for _ in ()).throw(ValueError("expecting value"))
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = requests_mod.request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == [(5, False)] and report.requested == 1

    def test_an_excluded_show_is_flagged_via_tvdb_never_auto_sent(self, monkeypatch):
        # The one cell that exercises TVDB crossing AND the exclusion flag together.
        sonarr = FakeArr(excluded={55555})
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "excluded show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        report = requests_mod.request_missing(_cfg(sonarr=SONARR), FakeTmdb({20: 55555}), demand, dry_run=False)
        assert sonarr.series_calls == []
        assert len(report.queued) == 1 and report.queued[0].excluded is True

    def test_arr_present_carries_every_tracked_id_for_the_stale_row_prune(self, monkeypatch):
        # The report must hand the server EVERYTHING the Arrs track — keyed by tmdb for both types
        # (shows via Sonarr v4's own tmdbId) — not just the titles in this run's pool, so
        # _persist_request_queue can prune stale pending rows for titles added by other means.
        radarr = FakeArr(present={5, 6})
        sonarr = FakeArr(present={55555}, present_tmdb={20, 21})
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: radarr)
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(
            MissingTitle(5, "tracked film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500),
            MissingTitle(20, "tracked show", MediaType.SHOW, 2020, rating=8.0, vote_count=500),
        )
        report = requests_mod.request_missing(
            _cfg(radarr=RADARR, sonarr=SONARR), FakeTmdb({20: 55555}), demand, dry_run=False
        )
        assert report.arr_present == {(5, "movie"), (6, "movie"), (20, "show"), (21, "show")}
        assert radarr.movie_calls == [] and sonarr.series_calls == []  # both tracked -> neither sent

    def test_show_without_tvdb_is_skipped_not_requested(self, monkeypatch):
        sonarr = FakeArr()
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        cfg = _cfg(sonarr=SONARR)
        report = requests_mod.request_missing(cfg, FakeTmdb({20: None}), demand, dry_run=False)
        assert sonarr.series_calls == []
        assert report.outcomes[0].status == "skipped_no_tvdb"

    def test_missing_target_for_media_type_is_skipped(self, monkeypatch):
        # Movies wanted but only Sonarr configured -> skipped_no_target, never an error.
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        cfg = _cfg(sonarr=SONARR)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert report.outcomes[0].status == "skipped_no_target"
        assert report.requested == 0

    def test_dry_run_flows_through_to_the_client(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        cfg = _cfg(radarr=RADARR)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=True)
        assert fake.movie_calls == [(10, True)]
        assert report.outcomes[0].status == "would_request"

    def test_each_titles_tags_reach_the_client(self, monkeypatch):
        radarr, sonarr = FakeArr(), FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: radarr)
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(
            MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500, tags={"sarah", "kids"}),
            MissingTitle(20, "show", MediaType.SHOW, 2020, rating=8.0, vote_count=500, tags={"mike"}),
        )
        cfg = _cfg(radarr=RADARR, sonarr=SONARR)
        requests_mod.request_missing(cfg, FakeTmdb({20: 55555}), demand, dry_run=False)
        assert radarr.tag_calls == [{"sarah", "kids"}]  # the movie's per-user/per-row tags
        assert sonarr.tag_calls == [{"mike"}]

    def test_min_demand_excludes_titles_too_few_people_want(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "one wanter", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=1),
            MissingTitle(2, "two wanters", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=2),
        )
        cfg = _cfg(radarr=RADARR, min_demand=2)
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
        cfg = _cfg(radarr=RADARR, min_year=2000)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [2]  # 1998 and unknown-year both excluded

    def test_max_year_excludes_newer_titles(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "classic", MediaType.MOVIE, 1975, rating=9.0, vote_count=900),
            MissingTitle(2, "recent", MediaType.MOVIE, 2021, rating=8.0, vote_count=900),
            MissingTitle(3, "no year", MediaType.MOVIE, None, rating=8.5, vote_count=900),
        )
        cfg = _cfg(radarr=RADARR, max_year=1990)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [1]  # 2021 and unknown-year both excluded

    def test_year_window_keeps_only_titles_inside_both_bounds(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "too old", MediaType.MOVIE, 1995, rating=9.0, vote_count=900),
            MissingTitle(2, "in window", MediaType.MOVIE, 2010, rating=8.0, vote_count=900),
            MissingTitle(3, "too new", MediaType.MOVIE, 2024, rating=8.5, vote_count=900),
        )
        cfg = _cfg(radarr=RADARR, min_year=2000, max_year=2020)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [2]  # only the 2010 title is inside [2000, 2020]

    def test_impossible_year_window_requests_nothing_and_does_not_raise(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "a", MediaType.MOVIE, 2005, rating=9.0, vote_count=900),
            MissingTitle(2, "b", MediaType.MOVIE, 2015, rating=8.0, vote_count=900),
        )
        cfg = _cfg(radarr=RADARR, min_year=2020, max_year=2010)  # max < min -> matches nothing
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []  # fails safe: no request, no crash
        assert report.considered == 0

    def test_source_gates_on_mdblist_rating_not_tmdb(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        # Both clear TMDB, but only title 2 clears the IMDb floor once MDBList is consulted.
        mdblist = FakeMdbList({1: (6.2, 5000), 2: (8.3, 400000)})
        demand = self._demand(
            MissingTitle(1, "tmdb-hyped", MediaType.MOVIE, 2020, rating=9.0, vote_count=900),
            MissingTitle(2, "imdb-loved", MediaType.MOVIE, 2020, rating=7.5, vote_count=900),
        )
        cfg = _cfg(radarr=RADARR, rating_source="imdb", mdblist_api_key="k")
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert [c[0] for c in fake.movie_calls] == [2]
        assert report.considered == 1

    def test_mdblist_lookup_failure_drops_only_that_title(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        mdblist = FakeMdbList({2: (8.5, 900)}, error_on=1)
        demand = self._demand(
            MissingTitle(1, "mdblist boom", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=5),
            MissingTitle(2, "fine", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=1),
        )
        cfg = _cfg(radarr=RADARR, rating_source="imdb", mdblist_api_key="k")
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert [c[0] for c in fake.movie_calls] == [2]  # the raising lookup is skipped, the rest survive

    def test_mdblist_lookups_are_bounded_to_the_shortlist(self, monkeypatch):
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        mdblist = FakeMdbList({})  # every title passes with the default score
        demand = self._demand(
            *[
                MissingTitle(i, f"t{i}", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=1)
                for i in range(1, 41)
            ]
        )
        cfg = _cfg(radarr=RADARR, rating_source="imdb", mdblist_api_key="k", max_per_run=5)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert mdblist.calls <= requests_mod._IMDB_SHORTLIST  # daily-cap guard holds

    def test_non_tmdb_source_without_a_client_falls_back_to_tmdb(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=900))
        # rating_source imdb but no MDBList key/client -> gate on TMDB (never silently request nothing).
        cfg = _cfg(radarr=RADARR, rating_source="imdb", mdblist_api_key="")
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=None)
        assert [c[0] for c in fake.movie_calls] == [1]

    def test_critic_source_skips_the_vote_floor_but_still_enforces_rating(self, monkeypatch):
        # Rotten Tomatoes/Metacritic are critic scores, so the audience min_votes floor is skipped —
        # but a low score is still rejected. (votes=0 here would fail the floor for imdb.)
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        mdblist = FakeMdbList({1: (9.0, 0), 2: (5.5, 0)})  # already-normalised 0..10; no votes
        demand = self._demand(
            MissingTitle(1, "acclaimed", MediaType.MOVIE, 2020, rating=6.0, vote_count=10),
            MissingTitle(2, "panned", MediaType.MOVIE, 2020, rating=9.9, vote_count=10),
        )
        cfg = _cfg(radarr=RADARR, rating_source="tomatoes", mdblist_api_key="k", min_votes=100)
        requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert [c[0] for c in fake.movie_calls] == [1]  # 9.0 clears despite 0 votes; 5.5 rejected

    def test_mdblist_quota_exhaustion_falls_back_to_tmdb_and_flags(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        # First lookup fine, then the quota is spent — the whole pool is re-gated on TMDB and flagged.
        mdblist = FakeMdbList({1: (8.0, 900)}, rate_limit_after=1)
        demand = self._demand(
            MissingTitle(1, "a", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=2),
            MissingTitle(2, "b", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=1),
        )
        cfg = _cfg(radarr=RADARR, rating_source="imdb", mdblist_api_key="k")
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert report.ratings_rate_limited is True
        assert sorted(c[0] for c in fake.movie_calls) == [1, 2]  # both requested via the TMDB fallback

    def test_max_per_run_zero_requests_nothing(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=9.0, vote_count=900))
        cfg = _cfg(radarr=RADARR, max_per_run=0)
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
        cfg = _cfg(radarr=RADARR, sonarr=SONARR)
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
        cfg = _cfg(radarr=RADARR)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        statuses = {o.tmdb_id: o.status for o in report.outcomes}
        assert statuses == {10: "error", 11: "requested"}  # the second still went through

    def test_a_failed_auto_send_is_queued_with_its_reason_not_dropped(self, monkeypatch):
        # A failed auto-send used to vanish (neither sent nor queued) and retry blindly every night.
        # It must land in the inbox WITH the reason, so the owner sees it and can retry by hand.
        fake = FakeArr(raise_on=10)
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "boom", MediaType.MOVIE, 2020, rating=9.9, vote_count=999, demand=5))
        cfg = _cfg(radarr=RADARR)
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert report.requested == 0  # it didn't land
        assert [m.tmdb_id for m in report.queued] == [10]  # queued for the inbox, not lost
        outcome_detail = next(o.detail for o in report.outcomes if o.tmdb_id == 10)
        assert report.queued[0].detail == outcome_detail  # carries WHY, shown as "Last attempt: …"

    def test_a_skipped_present_auto_title_is_not_queued(self, monkeypatch):
        # "already in Radarr" is being handled — it must NOT clutter the inbox as a pending row that
        # reappears every night. Only genuine "error" outcomes are queued, never the skips.
        fake = FakeArr(skip_present={10})
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "have it", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=5))
        report = requests_mod.request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert [o.status for o in report.outcomes] == ["skipped_present"]
        assert report.queued == [] and report.sent == []  # handled elsewhere — kept out of the inbox


class TestHybridSplit:
    """The auto-send-vs-queue split: strong titles go now, borderline ones wait for the owner."""

    def _demand(self, *titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def _hybrid(self, **kw) -> RequestConfig:
        base = dict(
            enabled=True,
            radarr=RADARR,
            min_rating=7.0,
            min_votes=100,
            min_demand=1,
            auto_send=True,
            auto_min_demand=3,
            auto_min_rating=8.0,
            max_per_run=10,
        )
        base.update(kw)
        return RequestConfig(**base)

    def test_strong_titles_auto_send_borderline_ones_queue(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "strong", MediaType.MOVIE, 2020, rating=8.5, vote_count=900, demand=4),  # clears auto
            MissingTitle(2, "few wanters", MediaType.MOVIE, 2020, rating=8.5, vote_count=900, demand=1),  # base only
            MissingTitle(3, "lower rated", MediaType.MOVIE, 2020, rating=7.2, vote_count=900, demand=5),  # base only
        )
        report = requests_mod.request_missing(self._hybrid(), FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [1]  # only the title clearing BOTH auto bars is sent
        assert sorted(m.tmdb_id for m in report.queued) == [2, 3]  # the borderline ones wait for approval
        assert report.considered == 3

    def test_auto_send_off_queues_every_qualifying_title(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "strong", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=9))
        report = requests_mod.request_missing(self._hybrid(auto_send=False), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []  # fully manual: even a clear winner waits
        assert [m.tmdb_id for m in report.queued] == [1]

    def test_auto_worthy_overflow_beyond_cap_is_queued_not_lost(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            *[MissingTitle(i, f"t{i}", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=5) for i in range(4)]
        )
        report = requests_mod.request_missing(self._hybrid(max_per_run=2), FakeTmdb(), demand, dry_run=False)
        assert len(fake.movie_calls) == 2  # only max_per_run auto-sent
        assert len(report.queued) == 2  # the two that overflowed the cap wait for approval, not dropped

    def test_below_base_floor_is_neither_sent_nor_queued(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "too low", MediaType.MOVIE, 2020, rating=5.0, vote_count=900, demand=9))
        report = requests_mod.request_missing(self._hybrid(), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []
        assert report.queued == []  # below the base rating floor -> not even worth queuing
        assert report.considered == 0

    def test_imdb_rating_is_carried_onto_queued_titles(self, monkeypatch):
        # rating_source=imdb: a queued title must show the IMDb score it was gated on, not its TMDB one.
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        mdblist = FakeMdbList({1: (8.8, 250000)})
        demand = self._demand(
            # 1 wanter -> below the auto bar -> queued (with its IMDb score, checked below)
            MissingTitle(1, "imdb-loved", MediaType.MOVIE, 2020, rating=7.1, vote_count=120, demand=1),
        )
        cfg = self._hybrid(rating_source="imdb", mdblist_api_key="k")
        report = requests_mod.request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert len(report.queued) == 1
        assert report.queued[0].rating == 8.8  # IMDb, not the 7.1 TMDB value it arrived with
        assert report.queued[0].vote_count == 250000


class TestRequestTitles:
    """Explicit send of owner-approved titles from the inbox — no floors applied."""

    def test_sends_given_titles_ignoring_all_floors(self, monkeypatch):
        radarr, sonarr = FakeArr(), FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: radarr)
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        titles = [
            MissingTitle(10, "obscure film", MediaType.MOVIE, 1990, rating=3.0, vote_count=4, demand=1),
            MissingTitle(20, "niche show", MediaType.SHOW, 1990, rating=3.0, vote_count=4, demand=1),
        ]
        # Floors set impossibly high: request_titles must ignore them because the owner chose by hand.
        cfg = RequestConfig(enabled=True, radarr=RADARR, sonarr=SONARR, min_rating=9.9, min_votes=99999, min_demand=99)
        report = requests_mod.request_titles(cfg, FakeTmdb({20: 7777}), titles, dry_run=False)
        assert radarr.movie_calls == [(10, False)]
        assert sonarr.series_calls == [(7777, False)]  # routed by TVDB id, same path as the auto pass
        assert report.requested == 2

    def test_dry_run_flows_through(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        titles = [MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500)]
        cfg = RequestConfig(enabled=True, radarr=RADARR)
        report = requests_mod.request_titles(cfg, FakeTmdb(), titles, dry_run=True)
        assert fake.movie_calls == [(10, True)]
        assert report.outcomes[0].status == "would_request"

    def test_empty_list_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        report = requests_mod.request_titles(RequestConfig(enabled=True, radarr=RADARR), FakeTmdb(), [], dry_run=False)
        assert report.outcomes == []
        assert report.considered == 0
