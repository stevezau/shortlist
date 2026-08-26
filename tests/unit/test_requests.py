"""Request pass: which missing titles get asked for, and how the demand is gated and routed."""

from __future__ import annotations

import math

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


def _cand(
    tmdb_id: int,
    media: MediaType,
    *,
    rating: float = 8.0,
    votes: int = 500,
    poster: str = "",
    overview: str = "",
) -> Candidate:
    return Candidate(
        tmdb_id=tmdb_id,
        title=f"t{tmdb_id}",
        media_type=media,
        rating=rating,
        vote_count=votes,
        poster_path=poster,
        overview=overview,
    )


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
        self.monitor_calls: list[str] = []  # the Sonarr monitor mode each add was sent with
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

    def add_movie(
        self, tmdb_id: int, *, dry_run: bool, extra_tags: set[str] | None = None
    ) -> tuple[str, str, str | None]:
        self.movie_calls.append((tmdb_id, dry_run))
        self.tag_calls.append(set(extra_tags or set()))
        if self.raise_on == tmdb_id:
            raise ArrError("boom")
        if tmdb_id in self.skip_present:
            return ("skipped_present", "already in Radarr", f"movie-{tmdb_id}")
        return ("would_request" if dry_run else "requested", "ok", f"movie-{tmdb_id}")

    def add_series(
        self, tvdb_id: int, *, dry_run: bool, extra_tags: set[str] | None = None, monitor: str = "all"
    ) -> tuple[str, str, str | None]:
        self.series_calls.append((tvdb_id, dry_run))
        self.tag_calls.append(set(extra_tags or set()))
        self.monitor_calls.append(monitor)
        return ("would_request" if dry_run else "requested", "ok", f"series-{tvdb_id}")


class FakeTmdb:
    def __init__(
        self,
        tvdb: dict[int, int | None] | None = None,
        *,
        raise_on: int | None = None,
        imdb: dict[int, str | None] | None = None,
        posters: dict[int, str] | None = None,
        poster_raises: bool = False,
        overviews: dict[int, str] | None = None,
        overview_raises: bool = False,
    ):
        self._tvdb = tvdb or {}
        self._raise_on = raise_on
        self._imdb = imdb or {}
        self._posters = posters or {}
        self._poster_raises = poster_raises
        self._overviews = overviews or {}
        self._overview_raises = overview_raises
        self.poster_calls: list[int] = []  # every tmdb_id a poster was actually looked up for
        self.overview_calls: list[int] = []  # and every one a synopsis was looked up for

    def tvdb_id(self, tmdb_id: int, media_type: MediaType) -> int | None:
        if self._raise_on == tmdb_id:
            raise RuntimeError("TMDB API error HTTP 503")  # a non-ArrError, like the real client raises
        return self._tvdb.get(tmdb_id)

    def imdb_id(self, tmdb_id: int, media_type: MediaType) -> str | None:
        return self._imdb.get(tmdb_id, f"tt{tmdb_id:07d}")  # default: every title has a synthetic IMDb id

    def poster_path(self, tmdb_id: int, media_type: MediaType) -> str:
        # This stub MUST exist: the engine backfills a missing poster here inside a bare `except
        # Exception`, so an absent attribute is an AttributeError that gets silently swallowed —
        # every request test passed while the backfill did nothing at all.
        self.poster_calls.append(tmdb_id)
        if self._poster_raises:
            raise RuntimeError("TMDB API error HTTP 503")
        return self._posters.get(tmdb_id, f"/poster-{tmdb_id}.jpg")

    def overview(self, tmdb_id: int, media_type: MediaType) -> str:
        # Must exist for the same reason poster_path must, and it is the same trap: the synopsis
        # backfill sits inside a bare `except Exception`, so a fake without this method swallows an
        # AttributeError and every assertion below passes against a backfill that never ran.
        self.overview_calls.append(tmdb_id)
        if self._overview_raises:
            raise RuntimeError("TMDB API error HTTP 503")
        return self._overviews.get(tmdb_id, f"synopsis for {tmdb_id}")


class FakeMdbList:
    """Stand-in MDBList client returning preset (rating, votes) by TMDB id, counting lookups.

    ``error_on`` raises a generic error for one title (drops just that title); ``rate_limit_after``
    raises MdbListRateLimitError once that many LIVE lookups have happened (drives the TMDB fallback).

    ``cached`` are the ids whose rating is already in the persistent cache. Modelling it is not
    optional detail: the real client answers a cached title from SQLite and never calls the API, so a
    fake that bills every lookup as a request is EASIER than the real thing in the one dimension the
    budget is spent in — and that is precisely how a five-day production outage passed a green suite
    (see TestACachedHeadOfTheListMustNotStarveTheGate).
    """

    def __init__(
        self,
        ratings: dict[int, tuple[float, int] | None],
        *,
        error_on: int | None = None,
        rate_limit_after: int | None = None,
        cached: set[int] | None = None,
    ):
        self._ratings = ratings
        self._error_on = error_on
        self._rate_limit_after = rate_limit_after
        self._cached = set(cached or ())
        self.calls = 0  # every lookup asked for, cached or not
        self.live_lookups = 0  # only those that cost an API call — what the daily quota actually sees
        self.deferred: dict[int, int] = {}  # tmdb_id -> the TTL the gate asked to hold its score for

    def rating(self, tmdb_id: int, media_type: MediaType, source: str) -> tuple[float, int] | None:
        self.calls += 1
        if tmdb_id not in self._cached:
            self.live_lookups += 1
            # The real client caches the whole rating set on first fetch, so a second read of the
            # same title — by another row, or another source — is free. A fake that re-bills it is
            # HARDER than the real thing, and would make the cross-row overlap contract untestable.
            self._cached.add(tmdb_id)
            if self._rate_limit_after is not None and self.live_lookups > self._rate_limit_after:
                raise MdbListRateLimitError("quota spent")
        if tmdb_id == self._error_on:
            raise RuntimeError("MDBList hiccup")
        return self._ratings.get(tmdb_id, (8.0, 500))  # default: a passing score

    def defer_recheck(self, tmdb_id: int, media_type: MediaType, ttl_s: int) -> None:
        self.deferred[tmdb_id] = ttl_s
        self._cached.add(tmdb_id)  # as the real cache does: it stays readable, and readable is free


def _request_missing(cfg: RequestConfig, tmdb, demand, **kw):
    """A single-row `request_missing` call — the shape every test in this module was written against.

    The per-row split has its own module (test_per_row_requests.py). What these tests exercise —
    floors, the rating gate, the Arr reconcile, the hybrid auto-vs-queue split, routing and failure
    handling — are all per-ROW concerns, which one row expresses exactly. Passing the same config as
    both the run's base and the row's own reproduces the pre-per-row behaviour byte for byte.
    """
    return requests_mod.request_missing(cfg, tmdb, [requests_mod.RowRequest("picked", cfg, demand)], **kw)


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

    def test_keeps_the_poster_from_whichever_copy_actually_has_one(self):
        # The same title can reach two people from different sources: TMDB's list carries poster_path,
        # Trakt's "related" does not. Folding must not let the poster-less copy blank out the artwork.
        demand: requests_mod.DemandMap = {}
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE, poster="")])  # Trakt first
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE, poster="/art.jpg")])  # then TMDB
        assert demand[(2, MediaType.MOVIE)].poster_path == "/art.jpg"

        # ...and in the other order, the first one's artwork survives the second, poster-less fold.
        reverse: requests_mod.DemandMap = {}
        requests_mod.accumulate(reverse, [_cand(3, MediaType.MOVIE, poster="/art.jpg")])
        requests_mod.accumulate(reverse, [_cand(3, MediaType.MOVIE, poster="")])
        assert reverse[(3, MediaType.MOVIE)].poster_path == "/art.jpg"

    def test_keeps_the_synopsis_from_whichever_copy_actually_has_one(self):
        # Same fold rule as the poster, and it matters for the same reason: a Trakt-surfaced copy
        # carries no synopsis, and letting it blank out TMDB's costs a detail call in the enrichment
        # loop to buy back text that was already in hand.
        demand: requests_mod.DemandMap = {}
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE, overview="")])
        requests_mod.accumulate(demand, [_cand(2, MediaType.MOVIE, overview="A synopsis.")])
        assert demand[(2, MediaType.MOVIE)].overview == "A synopsis."

        reverse: requests_mod.DemandMap = {}
        requests_mod.accumulate(reverse, [_cand(3, MediaType.MOVIE, overview="A synopsis.")])
        requests_mod.accumulate(reverse, [_cand(3, MediaType.MOVIE, overview="")])
        assert reverse[(3, MediaType.MOVIE)].overview == "A synopsis."

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

        report = _request_missing(
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

        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False, already_handled={(9, "movie")})

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
        _request_missing(cfg, FakeTmdb({550: 1550}), demand, dry_run=False, already_handled={(550, "movie")})

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
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert report.considered == 1  # only the well-rated, widely-voted title
        assert [c[0] for c in fake.movie_calls] == [1]

    def test_backfills_a_missing_poster_but_leaves_one_it_already_has(self, monkeypatch):
        """A title a non-TMDB source surfaced arrives with no artwork; the inbox still needs some."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "From Trakt", MediaType.MOVIE, 2020, rating=8.0, vote_count=500, poster_path=""),
            MissingTitle(2, "From TMDB", MediaType.MOVIE, 2020, rating=8.0, vote_count=500, poster_path="/already.jpg"),
        )
        tmdb = FakeTmdb()
        report = _request_missing(_cfg(radarr=RADARR), tmdb, demand, dry_run=False)
        by_title = {m.title: m for m in report.sent}
        assert by_title["From Trakt"].poster_path == "/poster-1.jpg"  # looked up
        assert by_title["From TMDB"].poster_path == "/already.jpg"  # left alone
        assert tmdb.poster_calls == [1]  # and the one that had art cost no call at all

    def test_backfills_a_missing_synopsis_but_leaves_one_it_already_has(self, monkeypatch):
        """The inbox's whole point is judging an unfamiliar title, so a poster-less source's title
        needs its synopsis bought too — and a title that arrived with one must not pay for it."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "From Trakt", MediaType.MOVIE, 2020, rating=8.0, vote_count=500, overview=""),
            MissingTitle(2, "From TMDB", MediaType.MOVIE, 2020, rating=8.0, vote_count=500, overview="Already known."),
        )
        tmdb = FakeTmdb()
        report = _request_missing(_cfg(radarr=RADARR), tmdb, demand, dry_run=False)
        by_title = {m.title: m for m in report.sent}
        assert by_title["From Trakt"].overview == "synopsis for 1"  # looked up
        assert by_title["From TMDB"].overview == "Already known."  # left alone
        assert tmdb.overview_calls == [1]  # and the one that had text cost no call at all

    def test_a_failing_synopsis_lookup_never_fails_the_run(self, monkeypatch):
        """TMDB being down must cost a paragraph, not the whole request pass."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "no text", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(radarr=RADARR), FakeTmdb(overview_raises=True), demand, dry_run=False)
        # Still requested, and carrying "" — never None, which the NOT NULL column rejects.
        assert [m.title for m in report.sent] == ["no text"]
        assert report.sent[0].overview == ""

    def test_a_failing_poster_lookup_never_fails_the_run(self, monkeypatch):
        """TMDB being down must cost a picture, not the whole request pass."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "no art", MediaType.MOVIE, 2020, rating=8.0, vote_count=500, poster_path="")
        )
        report = _request_missing(_cfg(radarr=RADARR), FakeTmdb(poster_raises=True), demand, dry_run=False)
        # The title is still requested, and carries "" — never None, which the NOT NULL column rejects.
        assert [m.title for m in report.sent] == ["no art"]
        assert report.sent[0].poster_path == ""

    def test_does_not_look_up_a_poster_for_a_title_the_arr_already_has(self, monkeypatch):
        """Enrichment runs AFTER the Arr drop, so a discarded title costs no TMDB call."""
        fake = FakeArr(present={10})  # Radarr already tracks tmdb 10
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(10, "already downloading", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, poster_path="")
        )
        tmdb = FakeTmdb()
        report = _request_missing(_cfg(radarr=RADARR), tmdb, demand, dry_run=False)
        assert report.sent == []  # dropped as already-present
        # Asserting the CALL, not the outcome: the engine swallows a failed poster lookup, so a test
        # that only checked the result would pass whether or not the wasted call was made.
        assert tmdb.poster_calls == []

    def test_ranks_by_demand_then_rating_and_caps_at_max_per_run(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "one wanter", MediaType.MOVIE, 2020, rating=9.5, vote_count=999, demand=1),
            MissingTitle(2, "three wanters", MediaType.MOVIE, 2020, rating=7.1, vote_count=200, demand=3),
            MissingTitle(3, "two wanters", MediaType.MOVIE, 2020, rating=7.0, vote_count=200, demand=2),
        )
        cfg = _cfg(radarr=RADARR, max_per_run=2)
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
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
        _request_missing(cfg, tmdb, demand, dry_run=False)
        assert radarr.movie_calls == [(10, False)]
        assert sonarr.series_calls == [(55555, False)]  # requested by TVDB id, not TMDB id

    def test_a_title_already_in_radarr_is_dropped_not_requested(self, monkeypatch):
        # A title Radarr already tracks (or is downloading) isn't really "missing" — it's dropped, not
        # re-requested and not queued to clutter the inbox.
        fake = FakeArr(present={5})
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "have it", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []
        assert report.queued == [] and report.sent == []

    def test_a_show_already_in_sonarr_is_dropped_matched_on_tvdb(self, monkeypatch):
        # Sonarr keys on TVDB, candidates on TMDB — the drop must cross the namespace (the ID gap).
        sonarr = FakeArr(present={55555})
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "have show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(sonarr=SONARR), FakeTmdb({20: 55555}), demand, dry_run=False)
        assert sonarr.series_calls == [] and report.queued == []

    def test_an_excluded_title_is_queued_with_a_reason_never_auto_sent(self, monkeypatch):
        fake = FakeArr(excluded={5})
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "excluded film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []  # the Arr would refuse it, so it's never auto-sent
        assert len(report.queued) == 1
        assert report.queued[0].excluded is True  # surfaced as a flag, not a mislabelled "last attempt"

    def test_an_arr_state_fetch_error_drops_nothing(self, monkeypatch):
        # Fail OPEN: a Radarr hiccup on the presence fetch must not silently drop a wanted title.
        fake = FakeArr(present={5})
        fake.library_tmdb_ids = lambda: (_ for _ in ()).throw(ArrError("radarr down"))
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == [(5, False)] and report.requested == 1

    def test_a_non_arr_error_on_the_state_fetch_also_fails_open(self, monkeypatch):
        # A 200-with-HTML proxy response makes r.json() raise ValueError, not ArrError — still must
        # fail open (request as if the Arr held nothing), never abort the whole pass.
        fake = FakeArr(present={5})
        fake.library_tmdb_ids = lambda: (_ for _ in ()).throw(ValueError("expecting value"))
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(5, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == [(5, False)] and report.requested == 1

    def test_an_excluded_show_is_flagged_via_tvdb_never_auto_sent(self, monkeypatch):
        # The one cell that exercises TVDB crossing AND the exclusion flag together.
        sonarr = FakeArr(excluded={55555})
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "excluded show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(sonarr=SONARR), FakeTmdb({20: 55555}), demand, dry_run=False)
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
        report = _request_missing(_cfg(radarr=RADARR, sonarr=SONARR), FakeTmdb({20: 55555}), demand, dry_run=False)
        assert report.arr_present == {(5, "movie"), (6, "movie"), (20, "show"), (21, "show")}
        assert radarr.movie_calls == [] and sonarr.series_calls == []  # both tracked -> neither sent

    def test_show_without_tvdb_is_skipped_not_requested(self, monkeypatch):
        sonarr = FakeArr()
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        cfg = _cfg(sonarr=SONARR)
        report = _request_missing(cfg, FakeTmdb({20: None}), demand, dry_run=False)
        assert sonarr.series_calls == []
        assert report.outcomes[0].status == "skipped_no_tvdb"
        # The reason is what the operator READS on the Requests page, so it has to end their search
        # rather than restate the fault. "no TheTVDB id for this show" was true and useless: nothing
        # in it says whether Shortlist is broken, Sonarr is misconfigured, or this is simply how it
        # is — and there is exactly one remedy, which is to add the show in Sonarr by hand.
        detail = report.outcomes[0].detail
        assert "TMDB has no TheTVDB id" in detail
        assert "add it in Sonarr yourself" in detail, f"the reason must say what to do, got: {detail!r}"

    def test_a_failed_tvdb_lookup_is_told_apart_from_a_missing_one(self, monkeypatch):
        """Same missing id, opposite advice — so the two must not share wording.

        A lookup that RAISED (TMDB down, a timeout) may succeed next run and is worth waiting on. A
        lookup that succeeded and came back empty is a settled fact about TMDB's data that will never
        change on its own. Telling someone to go and edit Sonarr by hand because TMDB was briefly
        down would send them off to fix something that is not broken.
        """

        class Exploding:
            def tvdb_id(self, *a, **k):
                raise RuntimeError("boom")

        sonarr = FakeArr()
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        demand = self._demand(MissingTitle(20, "show", MediaType.SHOW, 2020, rating=8.0, vote_count=500))
        report = _request_missing(_cfg(sonarr=SONARR), Exploding(), demand, dry_run=False)

        assert report.outcomes[0].status == "error"
        assert "may work next run" in report.outcomes[0].detail
        assert "add it in Sonarr" not in report.outcomes[0].detail

    def test_missing_target_for_media_type_is_skipped(self, monkeypatch):
        # Movies wanted but only Sonarr configured -> skipped_no_target, never an error.
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        cfg = _cfg(sonarr=SONARR)
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert report.outcomes[0].status == "skipped_no_target"
        assert report.requested == 0

    def test_dry_run_flows_through_to_the_client(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500))
        cfg = _cfg(radarr=RADARR)
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=True)
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
        _request_missing(cfg, FakeTmdb({20: 55555}), demand, dry_run=False)
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
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
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
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
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
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
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
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [2]  # only the 2010 title is inside [2000, 2020]

    def test_impossible_year_window_requests_nothing_and_does_not_raise(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(1, "a", MediaType.MOVIE, 2005, rating=9.0, vote_count=900),
            MissingTitle(2, "b", MediaType.MOVIE, 2015, rating=8.0, vote_count=900),
        )
        cfg = _cfg(radarr=RADARR, min_year=2020, max_year=2010)  # max < min -> matches nothing
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
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
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
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
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
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
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert mdblist.calls <= requests_mod._lookup_budget(5)  # daily-cap guard holds

    def test_non_tmdb_source_without_a_client_falls_back_to_tmdb(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=900))
        # rating_source imdb but no MDBList key/client -> gate on TMDB (never silently request nothing).
        cfg = _cfg(radarr=RADARR, rating_source="imdb", mdblist_api_key="")
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=None)
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
        _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
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
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
        assert report.ratings_rate_limited is True
        assert sorted(c[0] for c in fake.movie_calls) == [1, 2]  # both requested via the TMDB fallback

    def test_max_per_run_zero_requests_nothing(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=9.0, vote_count=900))
        cfg = _cfg(radarr=RADARR, max_per_run=0)
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
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
        report = _request_missing(cfg, FakeTmdb(raise_on=20), demand, dry_run=False)
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
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        statuses = {o.tmdb_id: o.status for o in report.outcomes}
        assert statuses == {10: "error", 11: "requested"}  # the second still went through

    def test_a_failed_auto_send_is_queued_with_its_reason_not_dropped(self, monkeypatch):
        # A failed auto-send used to vanish (neither sent nor queued) and retry blindly every night.
        # It must land in the inbox WITH the reason, so the owner sees it and can retry by hand.
        fake = FakeArr(raise_on=10)
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(10, "boom", MediaType.MOVIE, 2020, rating=9.9, vote_count=999, demand=5))
        cfg = _cfg(radarr=RADARR)
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
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
        report = _request_missing(_cfg(radarr=RADARR), FakeTmdb(), demand, dry_run=False)
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
        report = _request_missing(self._hybrid(), FakeTmdb(), demand, dry_run=False)
        assert [c[0] for c in fake.movie_calls] == [1]  # only the title clearing BOTH auto bars is sent
        assert sorted(m.tmdb_id for m in report.queued) == [2, 3]  # the borderline ones wait for approval
        assert report.considered == 3

    def _blocked_log(self, cfg, demand) -> str:
        """The request pass's INFO lines. Loguru doesn't feed stdlib logging, so `caplog` sees
        nothing here — the suite's convention is a sink on the module's own logger."""
        lines: list[str] = []
        sink = requests_mod.logger.add(lines.append, level="INFO", format="{message}")
        try:
            _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        finally:
            requests_mod.logger.remove(sink)
        return "\n".join(lines)

    def test_the_log_names_which_bar_blocked_each_queued_title(self, monkeypatch):
        """ "0 auto-sent" alone is unanswerable — the run must say WHICH bar stopped each title.

        Every bar here is owner-tunable, so a run can queue everything simply because the settings
        at the time were stricter than the ones you read afterwards. Reconstructing that took a full
        forensic pass (settings timestamps vs run times vs persisted ratings, 2026-08-01); the run
        already holds all four facts, so it logs them.
        """
        fake = FakeArr(excluded={4})
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            MissingTitle(2, "few wanters", MediaType.MOVIE, 2020, rating=8.5, vote_count=900, demand=1),
            MissingTitle(3, "lower rated", MediaType.MOVIE, 2020, rating=7.2, vote_count=900, demand=5),
            MissingTitle(4, "excluded", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=5),
        )
        text = self._blocked_log(self._hybrid(), demand)

        assert fake.movie_calls == []
        assert "auto_min_demand (3)" in text, "the demand-blocked title must name its bar"
        assert "auto_min_rating (8.0)" in text, "the rating-blocked title must name its bar"
        assert "exclusion list" in text, "the excluded title must name its reason"

    def test_the_log_names_auto_send_being_off_as_the_reason(self, monkeypatch):
        """The commonest cause of a silent inbox, and the one the old line hid completely."""
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        demand = self._demand(MissingTitle(1, "strong", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=9))
        assert "auto-send is off" in self._blocked_log(self._hybrid(auto_send=False), demand)

    def test_the_log_names_the_cap_when_it_is_what_blocked_the_overflow(self, monkeypatch):
        """An overflow title is queued for a completely different reason than a weak one — raising
        `max_per_run` fixes it, and nothing else does. The line has to tell them apart."""
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        demand = self._demand(
            *[MissingTitle(i, f"t{i}", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=5) for i in range(4)]
        )
        assert "max_per_run (2) already filled" in self._blocked_log(self._hybrid(max_per_run=2), demand)

    def test_auto_send_off_queues_every_qualifying_title(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "strong", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=9))
        report = _request_missing(self._hybrid(auto_send=False), FakeTmdb(), demand, dry_run=False)
        assert fake.movie_calls == []  # fully manual: even a clear winner waits
        assert [m.tmdb_id for m in report.queued] == [1]

    def test_auto_worthy_overflow_beyond_cap_is_queued_not_lost(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            *[MissingTitle(i, f"t{i}", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=5) for i in range(4)]
        )
        report = _request_missing(self._hybrid(max_per_run=2), FakeTmdb(), demand, dry_run=False)
        assert len(fake.movie_calls) == 2  # only max_per_run auto-sent
        assert len(report.queued) == 2  # the two that overflowed the cap wait for approval, not dropped

    def test_below_base_floor_is_neither_sent_nor_queued(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(MissingTitle(1, "too low", MediaType.MOVIE, 2020, rating=5.0, vote_count=900, demand=9))
        report = _request_missing(self._hybrid(), FakeTmdb(), demand, dry_run=False)
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
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)
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
        report = requests_mod.request_titles_by_row(
            {"r": cfg}, FakeTmdb({20: 7777}), [("r", t) for t in titles], dry_run=False
        )
        assert radarr.movie_calls == [(10, False)]
        assert sonarr.series_calls == [(7777, False)]  # routed by TVDB id, same path as the auto pass
        assert report.requested == 2

    def test_each_row_sends_shows_under_its_own_monitor_mode(self, monkeypatch):
        """The per-row control is worth nothing if every row's shows reach Sonarr the same way.

        Both rows share one Sonarr (one client, one rate limiter), so the mode has to travel with the
        title rather than with the client — the mistake this asserts against.
        """
        sonarr = FakeArr()
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        taster = MissingTitle(20, "kids show", MediaType.SHOW, 2020, rating=8.0, vote_count=500)
        everything = MissingTitle(21, "long show", MediaType.SHOW, 2020, rating=8.0, vote_count=500)
        cfg_by_row = {
            "kids": RequestConfig(enabled=True, sonarr=SONARR, sonarr_monitor="firstSeason"),
            "grown": RequestConfig(enabled=True, sonarr=SONARR, sonarr_monitor="all"),
        }

        requests_mod.request_titles_by_row(
            cfg_by_row, FakeTmdb({20: 7777, 21: 8888}), [("kids", taster), ("grown", everything)], dry_run=False
        )

        assert sonarr.series_calls == [(7777, False), (8888, False)]
        assert sonarr.monitor_calls == ["firstSeason", "all"]

    def test_dry_run_flows_through(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        titles = [MissingTitle(10, "film", MediaType.MOVIE, 2020, rating=8.0, vote_count=500)]
        cfg = RequestConfig(enabled=True, radarr=RADARR)
        report = requests_mod.request_titles_by_row({"r": cfg}, FakeTmdb(), [("r", t) for t in titles], dry_run=True)
        assert fake.movie_calls == [(10, True)]
        assert report.outcomes[0].status == "would_request"

    def test_empty_list_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        report = requests_mod.request_titles_by_row(
            {"r": RequestConfig(enabled=True, radarr=RADARR)}, FakeTmdb(), [], dry_run=False
        )
        assert report.outcomes == []
        assert report.considered == 0


class TestWhyATitleWasNotSent:
    """A queued title records WHY it stayed queued.

    The engine already worked the reason out per title — "demand below auto_min_demand (2)",
    "max_per_run (3) already filled" — and then dropped it into a Counter for one aggregate log line.
    So every `sent` row carried a detail and every `pending` row carried none, and the one question
    an owner actually asks of the requests inbox ("why didn't THIS one go?") had no answer anywhere
    in the product. The reason is now kept on the title, which is what reaches the DB and the trace.
    """

    def _demand(self, *titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def test_a_title_below_the_auto_rating_bar_says_so(self):
        cfg = _cfg(radarr=RADARR, auto_min_demand=1, auto_min_rating=8.0, min_rating=7.0)
        report = _request_missing(
            cfg,
            FakeTmdb(),
            self._demand(MissingTitle(1, "Nearly", MediaType.MOVIE, 2020, rating=7.5, vote_count=900, demand=5)),
            dry_run=True,
        )
        assert len(report.queued) == 1
        assert "auto_min_rating" in report.queued[0].detail

    def test_a_title_below_the_auto_demand_bar_says_so(self):
        cfg = _cfg(radarr=RADARR, auto_min_demand=3, auto_min_rating=0.0, min_rating=7.0)
        report = _request_missing(
            cfg,
            FakeTmdb(),
            self._demand(MissingTitle(1, "Lonely", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=1)),
            dry_run=True,
        )
        assert "auto_min_demand" in report.queued[0].detail

    def test_a_title_that_overflowed_the_per_run_cap_says_so(self, monkeypatch):
        # The cap only fills if the first send SUCCEEDS, so the Arr client is faked like its siblings.
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: FakeArr())
        cfg = _cfg(radarr=RADARR, max_per_run=1, auto_min_demand=1, auto_min_rating=0.0, min_rating=7.0)
        report = _request_missing(
            cfg,
            FakeTmdb(),
            self._demand(
                MissingTitle(1, "First", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=9),
                MissingTitle(2, "Overflow", MediaType.MOVIE, 2020, rating=8.9, vote_count=900, demand=8),
            ),
            dry_run=False,
        )
        assert len(report.queued) == 1
        assert "max_per_run" in report.queued[0].detail

    def test_auto_send_switched_off_says_so(self):
        cfg = _cfg(radarr=RADARR, auto_send=False, min_rating=7.0)
        report = _request_missing(
            cfg,
            FakeTmdb(),
            self._demand(MissingTitle(1, "Waiting", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=9)),
            dry_run=True,
        )
        assert "auto-send is off" in report.queued[0].detail

    def test_an_excluded_title_says_so(self):
        cfg = _cfg(radarr=RADARR, auto_min_demand=1, auto_min_rating=0.0, min_rating=7.0)
        title = MissingTitle(1, "Blocked", MediaType.MOVIE, 2020, rating=9.0, vote_count=900, demand=9)
        title.excluded = True
        report = _request_missing(cfg, FakeTmdb(), self._demand(title), dry_run=True)
        assert "exclusion list" in report.queued[0].detail


class TestTheLookupBudgetFollowsTheOwnersRequestCap:
    """`requests.max_per_run` is validated 0..100, so the MDBList shortlist must scale with it.

    It was a flat 20. The TMDB gate filters the whole pool and has no limit, so an owner who set
    `max_per_run` to 40 got 40 requests on TMDB and at most 20 on IMDb/Trakt/RT/Metacritic — the same
    setting meaning two different things, with nothing anywhere saying so. This is the `_REORDER_TOP_N`
    shape: a hidden number quietly overriding a visible one.
    """

    def test_a_raised_cap_raises_the_pool(self):
        """The bug: at max_per_run=40 the old flat 20 could not even supply the run, let alone let
        the rating floors reject anything."""
        assert requests_mod._lookup_budget(40) > 40, "the pool must exceed the cap, or the run cannot fill"
        assert requests_mod._lookup_budget(100) == 400

    def test_a_low_send_rate_does_not_mean_a_shallow_look(self):
        """`max_per_run` is how many to SEND — everything else qualifying goes to the Waiting inbox.
        Choosing 3 a night says "add a little to my library", not "and barely look". The floor is what
        keeps those apart, and at 20 it did not: 20 lookups at the observed ~10% pass rate yield about
        two qualifying titles, so a modest cap silently meant a run that found nothing at all."""
        assert requests_mod._lookup_budget(3) == 100
        assert requests_mod._lookup_budget(1) == 100
        assert requests_mod._lookup_budget(0) == 100

    def test_the_walk_limit_tracks_the_budget(self):
        """A flat walk bound becomes the binding constraint the moment the cap goes up — rejects are
        held for 60 days, so ~60 runs' worth can sit in front of the frontier. That is the same flat
        number this module has already been bitten by twice."""
        assert requests_mod._walk_limit(requests_mod._lookup_budget(3)) > 100 * 60
        assert requests_mod._walk_limit(requests_mod._lookup_budget(100)) > requests_mod._walk_limit(
            requests_mod._lookup_budget(3)
        )

    @staticmethod
    def _demand(*titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def test_the_gate_actually_looks_up_more_when_the_cap_is_raised(self, monkeypatch):
        """The wiring, not just the arithmetic: removing the derivation must break this."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        mdblist = FakeMdbList({})  # every lookup misses, so nothing qualifies and nothing is sent
        demand = self._demand(
            *[
                MissingTitle(i, f"t{i}", MediaType.MOVIE, 2020, rating=8.0, vote_count=900, demand=1)
                for i in range(1, 121)
            ]
        )
        cfg = _cfg(radarr=RADARR, rating_source="imdb", mdblist_api_key="k", max_per_run=40)

        _request_missing(cfg, FakeTmdb(), demand, dry_run=False, mdblist=mdblist)

        assert mdblist.calls > 20, "a raised max_per_run must widen the MDBList shortlist past the old flat 20"
        assert mdblist.calls <= requests_mod._lookup_budget(40)


class TestACachedHeadOfTheListMustNotStarveTheGate:
    """Production, 2026-08-10..18: 10,488 titles wanted, `0 qualifying, 0 auto-sent, 0 queued`, every
    night for five days, and nothing sent to Radarr/Sonarr since the 13th.

    The gate rated the top `_lookup_budget` titles BY DEMAND and stopped there. Ratings cache for a
    week and the demand ranking barely moves, so the same head was re-inspected every night — the
    server's own cache showed 18 of 20 slots going to titles already rated and already rejected —
    while every title that WOULD have cleared the floor sat below the cut and was never looked at.

    The budget exists to protect MDBList's daily quota. A cache hit costs no quota, so billing one
    against the budget protects nothing and pins the run to the titles it has already rejected. Budget
    the API calls, and the walk can read straight past a stale head for free.

    The demand-vs-rating mismatch is what makes it permanent rather than unlucky: the shortlist is
    ordered by how many people want a title, the gate asks how well-rated it is, and on a large server
    the most-wanted MISSING titles are the ones nobody bothered to add — so the head of the list is
    systematically the worst-rated part of it.
    """

    @staticmethod
    def _demand(*titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def _cfg_imdb(self, **kw) -> RequestConfig:
        return _cfg(
            **{
                "radarr": RADARR,
                "rating_source": "imdb",
                "mdblist_api_key": "k",
                "max_per_run": 3,
                "min_rating": 7.3,
                "min_votes": 100,
                "min_demand": 1,
                "auto_min_demand": 1,
                "auto_min_rating": 7.5,
                **kw,
            }
        )

    def test_a_qualifying_title_below_a_cached_head_is_still_found(self, monkeypatch):
        """The outage, minimised: 30 cached titles that all fail, one that passes ranked beneath them.

        At `max_per_run=3` the budget is 20, so the old slice never reached title 99 — not on this
        run, and not on any later one, because the head's ratings stay cached and its demand stays top.
        """
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        losers = [MissingTitle(i, f"loser{i}", MediaType.MOVIE, 2021, 0.0, 0, demand=100 - i) for i in range(1, 31)]
        winner = MissingTitle(99, "winner", MediaType.MOVIE, 2021, 0.0, 0, demand=2)
        ratings: dict[int, tuple[float, int] | None] = {t.tmdb_id: (6.1, 5000) for t in losers}
        ratings[99] = (8.4, 9000)
        mdblist = FakeMdbList(ratings, cached={t.tmdb_id for t in losers})

        report = _request_missing(
            self._cfg_imdb(), FakeTmdb(), self._demand(*losers, winner), dry_run=False, mdblist=mdblist
        )

        assert [m.title for m in report.sent] == ["winner"]
        assert fake.movie_calls == [(99, False)]

    def test_reading_past_the_cached_head_costs_no_quota(self, monkeypatch):
        """The daily-cap guard is the reason the budget exists — it must survive the fix intact.

        Only the one uncached title may cost an API call, however far down the list it sits.
        """
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        losers = [MissingTitle(i, f"loser{i}", MediaType.MOVIE, 2021, 0.0, 0, demand=100 - i) for i in range(1, 61)]
        winner = MissingTitle(99, "winner", MediaType.MOVIE, 2021, 0.0, 0, demand=2)
        ratings: dict[int, tuple[float, int] | None] = {t.tmdb_id: (6.1, 5000) for t in losers}
        ratings[99] = (8.4, 9000)
        mdblist = FakeMdbList(ratings, cached={t.tmdb_id for t in losers})

        _request_missing(self._cfg_imdb(), FakeTmdb(), self._demand(*losers, winner), dry_run=False, mdblist=mdblist)

        assert mdblist.live_lookups == 1, "60 cached titles must not bill a single request against the cap"
        assert mdblist.calls == 61, "but all 61 must actually have been consulted"

    def test_uncached_titles_still_stop_at_the_budget(self, monkeypatch):
        """The other half of the guard: when nothing is cached, the walk must stop at the budget and
        NOT read on through a 10,000-title pool spending a live request on each."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        budget = requests_mod._lookup_budget(3)
        # Flat demand, deliberately: a descending `100 - i` goes non-positive partway down and
        # `min_demand` then trims the pool below the budget, so the assertion would pass by running
        # out of titles rather than by the budget stopping the walk.
        pool = [MissingTitle(i, f"t{i}", MediaType.MOVIE, 2021, 0.0, 0, demand=5) for i in range(1, budget * 3)]
        mdblist = FakeMdbList({t.tmdb_id: (6.1, 5000) for t in pool})  # nothing cached, nothing passes

        _request_missing(self._cfg_imdb(), FakeTmdb(), self._demand(*pool), dry_run=False, mdblist=mdblist)

        assert mdblist.live_lookups == budget

    def test_a_clear_reject_is_not_re_rated_next_week(self, monkeypatch):
        """Without this the fix undoes itself in about a week.

        Everything rated is cached for 7 days, so in a steady state the titles expiring each night
        equal the titles rated each night — which is the budget. The whole allowance would go back to
        re-rating the same high-demand rejects and the walk would stop advancing again. A title far
        below the bar is held much longer; a near miss keeps the weekly re-check, because that is the
        one that can actually cross.
        """
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        hopeless = MissingTitle(1, "hopeless", MediaType.MOVIE, 2021, 0.0, 0, demand=9)
        near_miss = MissingTitle(2, "near miss", MediaType.MOVIE, 2021, 0.0, 0, demand=8)
        mdblist = FakeMdbList({1: (5.0, 5000), 2: (7.1, 5000)})  # floor is 7.3

        _request_missing(
            self._cfg_imdb(), FakeTmdb(), self._demand(hopeless, near_miss), dry_run=False, mdblist=mdblist
        )

        assert mdblist.deferred == {1: requests_mod._REJECT_RECHECK_TTL_S}
        assert 2 not in mdblist.deferred, "a title 0.2 off the bar must still be re-checked weekly"

    def test_a_deferred_title_is_still_admitted_when_the_floor_drops(self, monkeypatch):
        """Deferral holds the SCORE, not a verdict — so lowering min_rating admits it next run, free."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        title = MissingTitle(1, "held", MediaType.MOVIE, 2021, 0.0, 0, demand=9)
        mdblist = FakeMdbList({1: (6.0, 5000)})
        _request_missing(self._cfg_imdb(), FakeTmdb(), self._demand(title), dry_run=False, mdblist=mdblist)
        assert mdblist.deferred and not fake.movie_calls  # rejected at 7.3, and its score is held

        report = _request_missing(
            self._cfg_imdb(min_rating=5.5, auto_min_rating=5.5),
            FakeTmdb(),
            self._demand(MissingTitle(1, "held", MediaType.MOVIE, 2021, 0.0, 0, demand=9)),
            dry_run=False,
            mdblist=mdblist,
        )

        assert [m.title for m in report.sent] == ["held"]
        assert mdblist.live_lookups == 1, "the second run must read the held score, not re-buy it"

    def test_the_free_walk_is_still_bounded(self, monkeypatch):
        """Cache hits are free but not instant — each is a local read. A pool whose head is an
        unbounded run of cached titles must still terminate rather than scan the lot."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        limit = requests_mod._walk_limit(requests_mod._lookup_budget(3))
        pool = [MissingTitle(i, f"t{i}", MediaType.MOVIE, 2021, 0.0, 0, demand=1) for i in range(1, limit + 200)]
        mdblist = FakeMdbList({t.tmdb_id: (6.1, 5000) for t in pool}, cached={t.tmdb_id for t in pool})

        report = _request_missing(self._cfg_imdb(), FakeTmdb(), self._demand(*pool), dry_run=False, mdblist=mdblist)

        assert mdblist.calls == limit
        assert report.examined == limit

    def test_zero_qualifying_says_how_far_it_looked(self, monkeypatch):
        """The second half of the outage: it was SILENT. `0 qualifying, 0 auto-sent, 0 queued` is the
        same sentence whether the floors emptied the pool or the gate never reached the good titles,
        and neither the run stats nor the inbox carried the difference — the only way to tell them
        apart was reading the container's log by hand."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        pool = [MissingTitle(i, f"t{i}", MediaType.MOVIE, 2021, 0.0, 0, demand=100 - i) for i in range(1, 51)]
        mdblist = FakeMdbList({t.tmdb_id: (6.1, 5000) for t in pool}, cached={t.tmdb_id for t in pool})

        report = _request_missing(self._cfg_imdb(), FakeTmdb(), self._demand(*pool), dry_run=False, mdblist=mdblist)

        assert report.considered == 0
        assert report.pool_size == 50, "how many titles cleared the base floors"
        assert report.examined == 50, "how many the rating gate actually rated"
        assert report.lookups_spent == 0, "and what that cost against the daily cap"


class TestTheCachedHeadStarvesRowsToo:
    """Release review, 2026-08-18 (MEDIUM). The same outage this module was rewritten for, rotated
    onto the row axis — and it only appears once a run has three or more rows whose pools overlap.

    The lookup budget is split between rows, which is right: it is an API quota. But the WALK limit
    was derived from each row's share of it, while the rating cache the walk has to get past is
    global to the run. So the first rows stop inside a head of already-cached titles, spend nothing,
    and hand their share to later rows — which then have a bigger walk limit and clear the head. The
    run looks healthy (budget fully spent, titles considered, no "nothing qualified" alert), while
    the rows at the TOP of the owner's row order get zero, every night.

    Not hypothetical for the server this was found on: it builds four per-person rows per user, two
    of them over the same movie pool.
    """

    def _demand(self, *titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def _cfg(self, **kw) -> RequestConfig:
        return _cfg(
            **{
                "radarr": RADARR,
                "rating_source": "imdb",
                "mdblist_api_key": "k",
                "max_per_run": 4,
                "min_rating": 7.3,
                "min_votes": 100,
                "min_demand": 1,
                "auto_min_demand": 1,
                "auto_min_rating": 7.5,
                **kw,
            }
        )

    def test_the_first_row_is_not_starved_by_a_head_the_last_row_can_clear(self, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = self._cfg()
        rows = ["r0", "r1", "r2", "r3"]
        # A head every row must walk past, sized between one row's share of the budget and the whole
        # run's: short enough that the run CAN see past it, long enough that a quarter-share cannot.
        head_len = requests_mod._walk_limit(requests_mod._lookup_budget(cfg.max_per_run) // len(rows)) + 50
        head = [MissingTitle(i, f"cached{i}", MediaType.MOVIE, 2021, 0.0, 0, demand=90) for i in range(1, head_len)]
        winners = [MissingTitle(90_000 + i, f"win{i}", MediaType.MOVIE, 2021, 0.0, 0, demand=5) for i in range(4)]
        scores = {t.tmdb_id: (6.0, 5000) for t in head} | {w.tmdb_id: (8.9, 5000) for w in winners}
        mdblist = FakeMdbList(scores, cached={t.tmdb_id for t in head})
        pool = self._demand(*head, *winners)

        report = requests_mod.request_missing(
            cfg,
            FakeTmdb(),
            [requests_mod.RowRequest(slug, cfg, pool) for slug in rows],
            dry_run=False,
            mdblist=mdblist,
        )

        assert report.examined_by_row["r0"] >= head_len, (
            f"row r0 stopped at {report.examined_by_row['r0']} inside a {head_len}-title cached head — "
            "it spends nothing and hands its share to a later row, which then clears the head"
        )
        assert report.sent or report.queued, "the run as a whole must still find the good titles"


class TestLanguagePreference:
    """The language gate: which bar a title is judged against, and what "prefer" versus "only" means.

    The matrix `.claude/rules/testing.md` asks for, in full — mode (any / prefer / only) crossed with
    the title's language (preferred / other / unknown). No cell collapses into another: each of the
    three modes answers differently for a non-preferred title, and unknown is deliberately NOT the
    same case as "other" (see `is_preferred_language`).
    """

    def _demand(self, *titles: MissingTitle) -> requests_mod.DemandMap:
        return {(t.tmdb_id, t.media_type): t for t in titles}

    def _title(self, tmdb_id: int, language: str, *, rating: float, demand: int = 5) -> MissingTitle:
        m = MissingTitle(tmdb_id, f"t{tmdb_id}", MediaType.MOVIE, 2020, rating=rating, vote_count=900, demand=demand)
        m.language = language
        return m

    def _cfg_lang(self, **kw) -> RequestConfig:
        """min_rating 7.0 -> the derived other-language bar is 8.5, and auto_min_rating sits below it
        at 8.0 so the LANGUAGE bar is what decides, not the ordinary auto bar."""
        base = dict(
            enabled=True,
            radarr=RADARR,
            min_rating=7.0,
            min_votes=100,
            min_demand=1,
            auto_send=True,
            auto_min_demand=1,
            auto_min_rating=8.0,
            max_per_run=10,
        )
        base.update(kw)
        return RequestConfig(**base)

    def _run(self, cfg, demand, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        report = _request_missing(cfg, FakeTmdb(), demand, dry_run=False)
        return fake, report

    # ---- mode "any": one bar for everything (the shipped default) ----

    def test_any_mode_sends_every_language_on_the_one_bar(self, monkeypatch):
        """The default must be byte-identical to the behaviour before this setting existed."""
        demand = self._demand(
            self._title(1, "en", rating=8.2),
            self._title(2, "ja", rating=8.2),
            self._title(3, "", rating=8.2),
        )
        fake, _ = self._run(self._cfg_lang(language_mode="any"), demand, monkeypatch)
        assert sorted(c[0] for c in fake.movie_calls) == [1, 2, 3]

    # ---- mode "prefer": preferred keeps the normal bars, others need a higher one ----

    def test_prefer_sends_a_preferred_title_on_the_ordinary_bar(self, monkeypatch):
        demand = self._demand(self._title(1, "en", rating=8.2))
        fake, report = self._run(self._cfg_lang(language_mode="prefer"), demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [1]
        assert report.queued == []

    def test_prefer_holds_back_another_language_below_the_higher_bar(self, monkeypatch):
        """8.2 clears min_rating (7.0) AND auto_min_rating (8.0), and is still held — only the
        language bar (7.0 + 1.5 = 8.5) can explain that, which is exactly what it must say."""
        demand = self._demand(self._title(2, "ja", rating=8.2))
        fake, report = self._run(self._cfg_lang(language_mode="prefer"), demand, monkeypatch)
        assert fake.movie_calls == []
        assert [m.tmdb_id for m in report.queued] == [2]
        assert report.queued[0].detail == "rating below the bar for other languages (8.5)"

    def test_prefer_still_sends_another_language_that_is_really_highly_rated(self, monkeypatch):
        """The whole promise of the feature: great foreign titles are not excluded, only mid-tier ones."""
        demand = self._demand(self._title(2, "ja", rating=8.7))
        fake, report = self._run(self._cfg_lang(language_mode="prefer"), demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [2]
        assert report.queued == []

    def test_prefer_sends_a_title_sitting_exactly_on_the_bar(self, monkeypatch):
        """The comparison is `<`, so the bar itself passes. An off-by-one here silently moves
        everyone's threshold by a tenth of a point."""
        demand = self._demand(self._title(2, "ko", rating=8.5))
        fake, _ = self._run(self._cfg_lang(language_mode="prefer"), demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [2]

    def test_prefer_treats_an_unknown_language_as_preferred(self, monkeypatch):
        """Only a non-TMDB source (Trakt) leaves this empty. Holding those back would look like
        Trakt being broken rather than a language preference working."""
        demand = self._demand(self._title(3, "", rating=8.2))
        fake, _ = self._run(self._cfg_lang(language_mode="prefer"), demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [3]

    def test_prefer_never_discards_it_holds_back(self, monkeypatch):
        """The tier this lives in is load-bearing. A title below a BASE floor is dropped and the
        owner never learns it existed; this one has to reach the inbox so they can still say yes."""
        demand = self._demand(self._title(2, "ja", rating=7.4))
        _, report = self._run(self._cfg_lang(language_mode="prefer"), demand, monkeypatch)
        assert [m.tmdb_id for m in report.queued] == [2], "a held-back title must still reach the inbox"

    def test_prefer_honours_extra_languages_the_owner_added(self, monkeypatch):
        demand = self._demand(self._title(2, "ja", rating=8.2))
        cfg = self._cfg_lang(language_mode="prefer", preferred_languages=("en", "ja"))
        fake, _ = self._run(cfg, demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [2]

    def test_prefer_uses_an_explicit_bar_over_the_derived_one(self, monkeypatch):
        """min_rating_other=9.0 must beat the derived 8.5 — otherwise the setting reads back but
        never applies, the worst kind of disagreement between screen and run."""
        demand = self._demand(self._title(2, "ja", rating=8.7))
        cfg = self._cfg_lang(language_mode="prefer", min_rating_other=9.0)
        fake, report = self._run(cfg, demand, monkeypatch)
        assert fake.movie_calls == []
        assert report.queued[0].detail == "rating below the bar for other languages (9.0)"

    def test_a_zero_bar_is_a_real_choice_not_an_unset_one(self, monkeypatch):
        """0.0 is falsy, and the None-means-derive sentinel exists precisely so it cannot be mistaken
        for "unset" — a `x or derived` anywhere in the chain would turn this into 8.5."""
        demand = self._demand(self._title(2, "ja", rating=8.05))
        cfg = self._cfg_lang(language_mode="prefer", min_rating_other=0.0)
        fake, _ = self._run(cfg, demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [2]

    def test_prefer_with_an_empty_language_list_raises_the_bar_on_everything_it_can_identify(self, monkeypatch):
        """The fourth corner of the mode x list-state matrix, and the one the warning copy describes.

        With no languages listed, nothing is preferred — so every title whose language is KNOWN takes
        the higher bar, and only the unknown ones still go on the ordinary one. That last clause is
        why the on-screen warning says "every title Shortlist can identify a language for" rather
        than "every title": unknown stays permissive here, unlike in "only" mode.
        """
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            self._title(1, "en", rating=8.2),  # known, and now "other" — below the 8.5 bar
            self._title(2, "ja", rating=8.2),  # known, likewise
            self._title(3, "", rating=8.2),  # unknown — still preferred, still sent
        )
        cfg = self._cfg_lang(language_mode="prefer", preferred_languages=())
        fake, report = self._run(cfg, demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [3], "only the unidentifiable one goes"
        assert sorted(m.tmdb_id for m in report.queued) == [1, 2]
        assert all("other languages" in (m.detail or "") for m in report.queued)

    # ---- mode "only": never request another language at all ----

    def test_only_mode_drops_another_language_entirely(self, monkeypatch):
        """ "Only" is the one language decision that DISCARDS — a 9.9 must not reach the inbox either,
        or "never ask for this" would still be asking the owner about it every night."""
        demand = self._demand(self._title(1, "en", rating=8.2), self._title(2, "ja", rating=9.9))
        fake, report = self._run(self._cfg_lang(language_mode="only"), demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [1]
        assert [m.tmdb_id for m in report.queued] == []
        assert report.pool_size == 1, "the dropped title must not even be counted in the gated pool"

    def test_only_mode_keeps_an_unknown_language(self, monkeypatch):
        """Same call as `prefer` makes, and for the same reason — but here it is the difference
        between a Trakt title being requested and being silently deleted from the run."""
        demand = self._demand(self._title(3, "", rating=8.2))
        fake, _ = self._run(self._cfg_lang(language_mode="only"), demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [3]

    def test_only_mode_with_an_empty_language_list_requests_nothing(self, monkeypatch):
        """An empty list is a real state the row editor can produce, and it must not silently mean
        "everything" — the inverse of the control, on a path that adds titles to Radarr.

        The UNKNOWN title is the one that matters and the one this test originally missed. Unknown
        normally counts as preferred, so with an empty list every Trakt-sourced title was still
        auto-sent while the settings screen, the row editor and the reference table all said the
        server would ask for nothing. A test named `_requests_nothing` that only feeds it an English
        title proves the weaker claim and lets that copy go unchallenged.
        """
        demand = self._demand(
            self._title(1, "en", rating=9.0),
            self._title(2, "ja", rating=9.0),
            self._title(3, "", rating=9.0),  # unknown — the Trakt case
        )
        cfg = self._cfg_lang(language_mode="only", preferred_languages=())
        fake, report = self._run(cfg, demand, monkeypatch)
        assert fake.movie_calls == [], "nothing listed means nothing requested — unknown included"
        assert report.queued == [], "and nothing queued either; 'only' discards"

    # ---- the bar itself ----

    def test_the_derived_bar_follows_the_owners_own_floor(self):
        assert requests_mod.other_language_bar(self._cfg_lang(min_rating=6.0)) == 7.5
        assert requests_mod.other_language_bar(self._cfg_lang(min_rating=7.0)) == 8.5
        assert requests_mod.other_language_bar(self._cfg_lang(min_rating=8.0)) == 9.5

    def test_the_derived_bar_is_clamped_to_a_reachable_number(self):
        """`min_rating` may legally be 10, and 10 + 1.5 is a bar no rating can clear — which turns
        "prefer" into "hold back every other language" while the screen still says 8.5-ish. The
        settings UI clamps its preview at 10, so an unclamped engine would also disagree with it."""
        assert requests_mod.other_language_bar(self._cfg_lang(min_rating=8.6)) == 10.0
        assert requests_mod.other_language_bar(self._cfg_lang(min_rating=10.0)) == 10.0

    def test_the_derived_bar_rounds_the_way_the_settings_screen_does(self):
        """The API accepts two decimals, so 7.25 + 1.5 = 8.75 — which the screen renders as 8.8.
        Two implementations of one rule that disagree is how a setting starts lying about itself.

        7.15 is the regression case: `round(8.65, 1)` is 8.6 (banker's rounding on the true binary
        value) where the browser's `Math.round(86.5) / 10` is 8.7. The obvious Python spelling was
        wrong on 42 of the 1001 two-decimal floors in range, and this is one of them."""
        assert requests_mod.other_language_bar(self._cfg_lang(min_rating=7.25)) == 8.8
        assert requests_mod.other_language_bar(self._cfg_lang(min_rating=7.15)) == 8.7

    def test_the_derived_bar_matches_the_browsers_arithmetic_on_every_legal_floor(self):
        """The settings screen previews this number and the engine enforces it. If they disagree
        anywhere in range, the field shows a bar that is not the one applied — so the agreement is
        pinned across the whole domain, not at the two values a hand-written test would pick.

        This is `Math.round(Math.min(10, m + 1.5) * 10) / 10` transcribed; keep it that way.
        """
        for hundredths in range(0, 1001):
            floor_value = hundredths / 100
            browser = math.floor(min(10.0, floor_value + 1.5) * 10 + 0.5) / 10
            assert requests_mod.other_language_bar(self._cfg_lang(min_rating=floor_value)) == browser, (
                f"min_rating={floor_value} diverges from the settings screen"
            )

    def test_a_title_one_row_knows_the_language_of_is_not_blank_in_another(self, monkeypatch):
        """Per-row demand builds one copy of a title per row, from that row's OWN users' candidates —
        so a row that only saw it via Trakt has no language while another row's TMDB copy does. In
        "only" mode the blank copy would be requested: the exact title the owner said never to ask
        for, sent because a different row happened to find it a different way."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = self._cfg_lang(language_mode="only")

        knows = self._title(42, "ja", rating=9.0)
        blank = self._title(42, "", rating=9.0)
        report = requests_mod.request_missing(
            cfg,
            FakeTmdb(),
            [
                requests_mod.RowRequest("trakt_row", cfg, self._demand(blank)),
                requests_mod.RowRequest("tmdb_row", cfg, self._demand(knows)),
            ],
            dry_run=False,
        )
        assert fake.movie_calls == [], "the row with the blank copy must not request an excluded title"
        assert report.pool_size == 0

    def test_the_language_drops_are_counted_apart_from_the_other_floors(self, monkeypatch):
        """`pool_size` drives the "nothing qualified" alert, which tells the owner which limit to
        loosen. A pool emptied by the language mode must not be reported as their demand and year
        settings being too tight — advice they could follow forever without effect."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(
            self._title(1, "ja", rating=9.0),
            self._title(2, "ko", rating=9.0),
            self._title(3, "en", rating=9.0),
        )
        report = _request_missing(self._cfg_lang(language_mode="only"), FakeTmdb(), demand, dry_run=False)
        assert report.dropped_by_language == 2
        assert report.pool_size == 1

    def test_a_poisoned_language_list_fails_closed_in_only_mode(self, monkeypatch):
        """`normalise_languages` degrades an unusable stored value to `()`, which lands in the same
        branch as a deliberately-cleared list — so in "only" mode a corrupt setting makes the run
        request NOTHING rather than everything.

        That direction is the point. This path adds titles to someone's library: a run that asks for
        nothing is a visible non-event the owner can investigate, where a run that asks for
        everything is a mess to undo.
        """
        from shortlist.engine.models import normalise_languages

        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = self._cfg_lang(language_mode="only", preferred_languages=normalise_languages(12345))
        assert cfg.preferred_languages == (), "a poisoned value degrades to empty, not to a crash"
        demand = self._demand(self._title(1, "en", rating=9.5), self._title(2, "ja", rating=9.5))
        fake, _ = self._run(cfg, demand, monkeypatch)
        assert fake.movie_calls == [], "fail closed — never request everything on a corrupt setting"

    def test_a_poisoned_language_list_degrades_instead_of_crashing_every_run(self):
        """The context builder reads this on EVERY run, and validation does not cover every way a
        value can get into the database — a hand edit, a future format, a downgrade. A TypeError here
        is a crash loop rather than one broken setting, which is the shape of a bug this codebase has
        already shipped once (`row.size: "abc"` crashed every run and 500'd two endpoints).

        A bare string is the subtle one: iterating "en" yields ("e", "n"), two codes that match
        nothing, silently putting every title on the higher bar.
        """
        from shortlist.engine.models import normalise_languages, row_languages_or_inherit

        assert normalise_languages("en") == ("en",), "a bare string is ONE code, not two letters"
        assert normalise_languages(["EN", None, 5, "", "  ja  "]) == ("en", "ja")
        for poison in (123, 4.5, True, {"a": 1}, object()):
            assert normalise_languages(poison) == ()
            assert row_languages_or_inherit(poison) == ()
        # The inherit/cleared distinction must survive the hardening — they mean different things.
        assert row_languages_or_inherit(None) is None, "None inherits the owner's list"
        assert row_languages_or_inherit([]) == (), "[] is a row that cleared its languages"

    def test_a_title_two_rows_wanted_counts_as_one_language_drop(self, monkeypatch):
        """`report.wanted` is a DEDUPLICATED set of keys, and the "nothing qualified" alert compares
        the two to ask "did the language setting rule out everything?". Summing per row counts a
        title two rows both wanted twice, so the drop count can exceed `wanted` and the alert then
        blames the language setting as the sole cause when it was not — the exact mis-attribution
        this counter was added to prevent, reintroduced one level up."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        cfg = self._cfg_lang(language_mode="only")
        report = requests_mod.request_missing(
            cfg,
            FakeTmdb(),
            [
                requests_mod.RowRequest("row_a", cfg, self._demand(self._title(42, "ja", rating=9.0))),
                requests_mod.RowRequest("row_b", cfg, self._demand(self._title(42, "ja", rating=9.0))),
            ],
            dry_run=False,
        )
        assert report.wanted == 1, "one title, however many rows wanted it"
        assert report.dropped_by_language == 1, "and one drop — not one per row"
        assert report.dropped_by_language <= report.wanted

    def test_nothing_is_counted_as_a_language_drop_in_the_other_modes(self, monkeypatch):
        """ "prefer" holds back at the AUTO tier, so it must never register as a base-floor drop —
        or the alert would blame the language setting for titles sitting safely in the inbox."""
        fake = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        demand = self._demand(self._title(1, "ja", rating=7.4))
        report = _request_missing(self._cfg_lang(language_mode="prefer"), FakeTmdb(), demand, dry_run=False)
        assert report.dropped_by_language == 0
        assert [m.tmdb_id for m in report.queued] == [1]

    def test_language_matching_is_case_insensitive_at_the_boundary(self, monkeypatch):
        """TMDB reports lowercase, but a code typed into settings or seeded from an env var may not
        be — and a case mismatch would reclassify a whole language as "other" silently."""
        from shortlist.engine.models import normalise_languages

        demand = self._demand(self._title(2, "ja", rating=8.2))
        cfg = self._cfg_lang(language_mode="prefer", preferred_languages=normalise_languages(["EN", "JA"]))
        fake, _ = self._run(cfg, demand, monkeypatch)
        assert [c[0] for c in fake.movie_calls] == [2]
