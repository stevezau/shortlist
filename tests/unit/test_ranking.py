from types import SimpleNamespace

from loguru import logger

from shortlist.engine.models import Candidate, MediaType, Pick, RowSpec, Seed, UserProfile, UserType
from shortlist.engine.ranking import diversify_by_seed, pre_rank, score
from tests.conftest import make_candidate


def seed(tmdb_id: int, weight: float) -> Seed:
    return Seed(tmdb_id=tmdb_id, title=f"seed{tmdb_id}", media_type=MediaType.MOVIE, weight=weight)


class TestScore:
    def test_seed_frequency_raises_the_score(self):
        one = make_candidate(1, "One", seeds=[seed(1, 1.0)])
        two = make_candidate(2, "Two", seeds=[seed(1, 1.0), seed(2, 1.0)])
        assert score(two) > score(one)

    def test_unrated_gets_neutral_prior_not_zero(self):
        unrated = make_candidate(1, "Unrated", rating=0.0)
        assert score(unrated) > 0

    def test_a_seedless_candidate_is_not_worthless(self):
        """Provenance ADDS, it doesn't multiply. When score was `seed_frequency x rating x weight`,
        every title from a seedless source (tmdb_discover / llm_library / llm_web) scored exactly 0
        and sorted below the worst seeded one — so those three sources never reached the curator."""
        seedless = make_candidate(1, "Great", rating=9.5, seeds=[])
        assert score(seedless) > 0

    def test_a_great_seedless_title_beats_a_poor_seeded_one(self):
        seedless = make_candidate(1, "Great", rating=9.5, seeds=[])
        seeded_but_poor = make_candidate(2, "Meh", rating=2.0, seeds=[seed(1, 0.5)])
        assert score(seedless) > score(seeded_but_poor)

    def test_seeds_still_win_all_else_equal(self):
        seeded = make_candidate(1, "Seeded", rating=7.0, seeds=[seed(1, 1.0)])
        seedless = make_candidate(2, "Seedless", rating=7.0, seeds=[])
        assert score(seeded) > score(seedless)


class TestPreRank:
    def test_keeps_top_n_by_score(self):
        cands = [make_candidate(i, f"m{i}", rating=float(i)) for i in range(1, 6)]
        top = pre_rank(cands, keep=2)
        assert [c.tmdb_id for c in top] == [5, 4]

    def test_stable_tiebreak_by_title(self):
        a = make_candidate(1, "Alpha", rating=7.0)
        b = make_candidate(2, "Beta", rating=7.0)
        assert [c.title for c in pre_rank([b, a], keep=2)] == ["Alpha", "Beta"]

    def test_a_flooding_source_cannot_shut_the_others_out(self):
        """The bug this exists to prevent: 30 seeds x TMDB suggestions is hundreds of seeded
        candidates, so a global sort handed the curator 40 tmdb_similar titles and nothing else —
        the LLM sources we paid for never reached it. Every source now gets a turn."""
        pool = [
            make_candidate(100 + i, f"sim{i}", rating=6.0, seeds=[seed(1, 1.0)], sources={"tmdb_similar"})
            for i in range(600)
        ]
        pool += [
            make_candidate(200 + i, f"disc{i}", rating=8.0, seeds=[], sources={"tmdb_discover"}) for i in range(20)
        ]
        pool += [make_candidate(300 + i, f"lib{i}", rating=9.0, seeds=[], sources={"llm_library"}) for i in range(20)]

        kept = pre_rank(pool, keep=30)

        by_source = {
            s: sum(1 for c in kept if s in c.sources) for s in ("tmdb_similar", "tmdb_discover", "llm_library")
        }
        assert all(count == 10 for count in by_source.values()), by_source

    def test_a_narrow_source_running_out_gives_its_slack_to_the_others(self):
        pool = [make_candidate(100 + i, f"sim{i}", rating=6.0, sources={"tmdb_similar"}) for i in range(50)]
        pool += [make_candidate(300 + i, f"lib{i}", rating=9.0, seeds=[], sources={"llm_library"}) for i in range(3)]

        kept = pre_rank(pool, keep=20)

        assert len(kept) == 20  # no slots wasted on a source with nothing left to offer
        assert sum(1 for c in kept if "llm_library" in c.sources) == 3  # all it had
        assert sum(1 for c in kept if "tmdb_similar" in c.sources) == 17

    def test_a_title_two_sources_both_found_is_only_kept_once(self):
        both = make_candidate(1, "Both", rating=8.0, sources={"tmdb_similar", "llm_library"})
        others = [make_candidate(10 + i, f"o{i}", rating=5.0, sources={"tmdb_similar"}) for i in range(5)]

        kept = pre_rank([both, *others], keep=3)

        assert [c.tmdb_id for c in kept].count(1) == 1

    def test_untagged_candidates_still_rank(self):
        """Candidates built by hand (cold start, tests) carry no source tag and must not vanish."""
        pool = [make_candidate(i, f"m{i}", rating=float(i)) for i in range(1, 6)]
        assert len(pre_rank(pool, keep=3)) == 3


class TestDiversifyBySeed:
    """The final row-selection step that replaced the LLM curate call: each *seed* gets a fair share
    of the row so one heavily-watched title can't swallow it. Input is `pre_rank` output (best-first)."""

    @staticmethod
    def _seeded(tmdb_id: int, seed_id: int, weight: float = 1.0) -> Candidate:
        """A candidate whose sole (and therefore top) seed is `seed_id` — so it queues under it."""
        return make_candidate(tmdb_id, f"t{tmdb_id}", seeds=[seed(seed_id, weight)])

    def test_a_short_pool_is_returned_unchanged(self):
        pool = [self._seeded(10, 1), self._seeded(11, 1), self._seeded(12, 2)]
        # keep >= len: nothing to spread, hand the pool back exactly (same objects, same order).
        assert diversify_by_seed(pool, keep=5) is pool

    def test_the_single_best_pick_still_leads(self):
        # Best-first input: the top-scored title heads a seed's queue, so seed A's best is picked first
        # and diversifying never displaces the strongest pick.
        pool = [self._seeded(10, 1), self._seeded(20, 2), self._seeded(11, 1), self._seeded(21, 2)]
        out = diversify_by_seed(pool, keep=2)
        assert out[0].tmdb_id == 10

    def test_each_seed_gets_a_slot_before_any_seed_gets_a_second(self):
        pool = [
            self._seeded(10, 1),  # seed 1, best
            self._seeded(11, 1),  # seed 1, second
            self._seeded(20, 2),  # seed 2, best
            self._seeded(30, 3),  # seed 3, best
        ]
        out = diversify_by_seed(pool, keep=3)
        # One per seed in the order each seed's best appeared — NOT 10,11,20 (which a score-sort gives).
        assert [c.tmdb_id for c in out] == [10, 20, 30]

    def test_one_flooding_seed_cannot_occupy_every_slot(self):
        """The bug this exists to prevent: 30 Breaking Bad look-alikes and a handful of everything else
        gave a row of nothing but Breaking Bad. Every other taste must still reach the row."""
        pool = [self._seeded(100 + i, 1) for i in range(30)]  # one seed floods the pool
        pool += [self._seeded(200, 2), self._seeded(300, 3), self._seeded(400, 4)]
        out = diversify_by_seed(pool, keep=8)
        seeds_present = {c.top_seed.tmdb_id for c in out}
        assert seeds_present == {1, 2, 3, 4}, "all four tastes must survive to the row"
        assert sum(1 for c in out if c.top_seed.tmdb_id == 1) < 8, "the flooding seed can't take every slot"

    def test_seedless_candidates_share_one_queue_and_interleave(self):
        """discover / web / cold-start picks carry no seed — they queue together under None and get
        their fair share alongside the seeded tastes, not dropped."""
        pool = [
            self._seeded(10, 1),
            make_candidate(90, "web1", seeds=[]),  # seedless
            self._seeded(11, 1),
            make_candidate(91, "web2", seeds=[]),  # seedless
        ]
        out = diversify_by_seed(pool, keep=2)
        # Seed 1's best, then the seedless queue's best — one taste each, not both seed-1 titles.
        assert [c.tmdb_id for c in out] == [10, 90]

    def test_falls_back_to_the_remaining_queue_when_others_run_dry(self):
        # keep exceeds the seed count, so after every seed is served the still-full queue backfills.
        pool = [self._seeded(10, 1), self._seeded(11, 1), self._seeded(12, 1), self._seeded(20, 2)]
        out = diversify_by_seed(pool, keep=3)
        assert [c.tmdb_id for c in out] == [10, 20, 11]  # seed1, seed2, back to seed1's next


class TestWatchedTitles:
    """The finished-title set: a movie you watched, or a show seen >= show_pct — but not a partway
    show or one with a new season."""

    def _finished(self, movies, plays, episodes, pct=0.9):
        from shortlist.engine.rows import _watched_titles

        # Plex gives per-show (viewed, total) counts; the old split plays/episodes dicts merge into that.
        watched_shows = {tid: (viewed, episodes.get(tid)) for tid, viewed in plays.items()}
        return _watched_titles(set(movies), watched_shows, pct)

    def test_counts_finished_movies_and_shows_but_not_partial(self):
        # movie 1 watched; show 10 finished (9 of 10 eps); show 20 partway (2 of 10); show 30 well
        # past the scaled floor of a 40-ep show (8 >= 15% of 40 = 6), so counts as finished.
        # Show 40: only 2 eps of a 40-ep show = below the scaled floor (6), still fresh.
        finished = self._finished(
            movies={1},
            plays={10: 9, 20: 2, 30: 8, 40: 2},
            episodes={10: 10, 20: 10, 30: 40, 40: 40},
        )
        assert (1, MediaType.MOVIE) in finished  # finished movie
        assert (10, MediaType.SHOW) in finished  # finished show (9 >= min(10*0.9, floor 3) = 3)
        assert (20, MediaType.SHOW) not in finished  # partway (2 < floor 3) -> still recommend
        assert (30, MediaType.SHOW) in finished  # 8 >= floor max(3, 40*0.15=6) = 6 -> counts as watched
        assert (40, MediaType.SHOW) not in finished  # 2 < floor 6 -> eligible again

    def test_unknown_episode_count_is_treated_as_finished(self):
        finished = self._finished(movies=set(), plays={10: 3}, episodes={})
        assert (10, MediaType.SHOW) in finished  # can't tell -> count as finished, don't re-recommend

    def test_a_returning_show_watched_a_lot_but_under_the_fraction_still_counts(self):
        # SFLIX/MooHouse Gold Rush: 160 of 226 episodes = 71%, under show_pct — but 160 plays clearly
        # means they're watching it, so the season-worth floor counts it as watched (was recommended).
        finished = self._finished(movies=set(), plays={40: 160}, episodes={40: 226})
        assert (40, MediaType.SHOW) in finished

    def test_a_lightly_sampled_show_is_still_a_fresh_pick(self):
        # A handful of episodes of a big show: below both the fraction AND the floor -> recommendable.
        # With _ENGAGED_EPISODES=3, need <3 plays to stay fresh: 2 of 226 is well below both bars.
        finished = self._finished(movies=set(), plays={40: 2}, episodes={40: 226})
        assert (40, MediaType.SHOW) not in finished

    def test_a_near_complete_short_show_counts_at_the_lowered_bar(self):
        # 8 of 9 episodes = 89%: caught up on a returning show (the newest ep just aired). At the 0.8
        # default it counts as watched. With _ENGAGED_EPISODES lowered from 10 to 3 (issue #12), the
        # floor (3) catches short shows early — 8 >= min(9*0.9, 3) = 3, so even at pct=0.9 it's finished.
        assert (10, MediaType.SHOW) in self._finished(movies=set(), plays={10: 8}, episodes={10: 9}, pct=0.8)
        assert (10, MediaType.SHOW) in self._finished(movies=set(), plays={10: 8}, episodes={10: 9}, pct=0.9)


class TestWatchedCap:
    """The percentage cap: at most `floor(k*pct)` of a row may be already-finished; the rest is
    backfilled from fresh candidates."""

    def _pick(self, tmdb_id: int):
        from shortlist.engine.models import Pick

        return Pick(
            tmdb_id=tmdb_id, rating_key=tmdb_id * 10, title=f"t{tmdb_id}", rank=1, reason="", media_type=MediaType.MOVIE
        )

    def test_zero_pct_would_not_be_called_but_cap_of_one_keeps_only_one_watched(self):
        from shortlist.engine.rows import _apply_watched_cap

        # 5 picks, first 3 already finished. Cap at 20% of 5 = 1 watched; the other two are dropped
        # and backfilled from fresh candidates 90, 91.
        watched = {(1, MediaType.MOVIE), (2, MediaType.MOVIE), (3, MediaType.MOVIE)}
        picks = [self._pick(i) for i in (1, 2, 3, 4, 5)]
        candidates = [make_candidate(i, f"c{i}") for i in (1, 2, 3, 4, 5, 90, 91)]
        out = _apply_watched_cap(picks, candidates, watched, k=5, pct=0.2)
        assert len(out) == 5
        kept_watched = [p for p in out if (p.tmdb_id, p.media_type) in watched]
        assert len(kept_watched) == 1, "only floor(5*0.2)=1 finished title may remain"
        assert {90, 91} <= {p.tmdb_id for p in out}, "freed slots backfilled from fresh candidates"

    def test_full_pct_keeps_every_watched_pick(self):
        from shortlist.engine.rows import _apply_watched_cap

        watched = {(1, MediaType.MOVIE), (2, MediaType.MOVIE)}
        picks = [self._pick(i) for i in (1, 2, 3)]
        out = _apply_watched_cap(picks, [make_candidate(i, f"c{i}") for i in (1, 2, 3)], watched, k=3, pct=1.0)
        assert {p.tmdb_id for p in out} == {1, 2, 3}  # no filtering at 100%


class TestReusablePrior:
    """Which carried-forward picks may be redelivered on a reuse night — the privacy-adjacent filter
    that keeps a stale/departed/now-watched title from being reused."""

    def _pick(self, tmdb_id: int, rank: int, mt=MediaType.MOVIE):
        from shortlist.engine.models import Pick

        return Pick(tmdb_id=tmdb_id, rating_key=0, title=f"t{tmdb_id}", rank=rank, reason="", media_type=mt)

    def test_keeps_valid_priors_in_order(self):
        from shortlist.engine.rows import _reusable_prior

        prior = [self._pick(10, 1), self._pick(11, 2), self._pick(12, 3)]
        sec_idx = {10: 100, 11: 110, 12: 120}
        out = _reusable_prior(prior, MediaType.MOVIE, sec_idx, watched=set(), pct=0.0)
        assert [p.tmdb_id for p in out] == [10, 11, 12]

    def test_drops_wrong_media_type(self):
        from shortlist.engine.rows import _reusable_prior

        prior = [self._pick(10, 1, MediaType.SHOW), self._pick(11, 2, MediaType.MOVIE)]
        out = _reusable_prior(prior, MediaType.MOVIE, {10: 100, 11: 110}, watched=set(), pct=0.0)
        assert [p.tmdb_id for p in out] == [11]  # the show is filtered out for a movie library

    def test_drops_a_title_that_left_the_library(self):
        from shortlist.engine.rows import _reusable_prior

        prior = [self._pick(10, 1), self._pick(11, 2)]
        out = _reusable_prior(prior, MediaType.MOVIE, {10: 100}, watched=set(), pct=0.0)
        assert [p.tmdb_id for p in out] == [10]  # 11 is no longer in the section index

    def test_drops_a_since_watched_title_only_when_pct_is_zero(self):
        from shortlist.engine.rows import _reusable_prior

        prior = [self._pick(10, 1), self._pick(11, 2)]
        sec_idx = {10: 100, 11: 110}
        watched = {(11, MediaType.MOVIE)}
        # 0% row: a now-finished title is no longer eligible, so it's dropped.
        assert [p.tmdb_id for p in _reusable_prior(prior, MediaType.MOVIE, sec_idx, watched, 0.0)] == [10]
        # A >0 row keeps it — the delivery-time watched cap trims the surplus instead.
        assert [p.tmdb_id for p in _reusable_prior(prior, MediaType.MOVIE, sec_idx, watched, 0.3)] == [10, 11]


class TestFreshnessCadence:
    """Freshness as a REFRESH CADENCE: 0 = never rebuild, 1 = nightly, in between = every N days."""

    def test_period_scales_with_freshness(self):
        from shortlist.engine.rows import _refresh_period_days

        assert _refresh_period_days(1.0) == 1  # full freshness -> rebuild every night
        assert _refresh_period_days(0.5) == 8  # ~weekly at the default
        assert _refresh_period_days(0.1) > _refresh_period_days(0.9)  # lower freshness -> longer gap
        assert _refresh_period_days(0.01) <= 14  # capped near a fortnight

    def test_zero_freshness_never_refreshes(self):
        from shortlist.engine.rows import _is_refresh_night

        # A frozen row: once built it is never a refresh night, on any day.
        assert not any(_is_refresh_night("row", "sarah", day, 0.0) for day in range(1, 60))

    def test_full_freshness_refreshes_every_night(self):
        from shortlist.engine.rows import _is_refresh_night

        assert all(_is_refresh_night("row", "sarah", day, 1.0) for day in range(1, 30))

    def test_run_day_zero_always_refreshes(self):
        # Direct engine calls / tests pass no day (0) — behave like the pre-cadence engine.
        from shortlist.engine.rows import _is_refresh_night

        assert _is_refresh_night("row", "sarah", 0, 0.5)

    def test_cadence_fires_once_per_period_and_is_stable(self):
        from shortlist.engine.rows import _is_refresh_night, _refresh_period_days

        period = _refresh_period_days(0.5)
        # Real run days start at 1 (day 0 is the tests/direct "always refresh" sentinel).
        hits = [d for d in range(1, period * 3 + 1) if _is_refresh_night("row", "sarah", d, 0.5)]
        # Exactly one refresh night per period window, and the schedule is reproducible (stable crc).
        assert len(hits) == 3
        assert all(_is_refresh_night("row", "sarah", d, 0.5) for d in hits)

    def test_phase_differs_across_rows_so_the_server_does_not_all_refresh_at_once(self):
        from shortlist.engine.rows import _is_refresh_night, _refresh_period_days

        period = _refresh_period_days(0.5)
        # Different (row, owner) keys land on different phase days within the period (crc-spread), so
        # the whole roster never re-curates on the same night. Search from day 1 (0 always refreshes).
        phases = {
            next(d for d in range(1, period + 1) if _is_refresh_night(f"row{n}", f"user{n}", d, 0.5)) for n in range(8)
        }
        assert len(phases) > 1


class TestAffinityBeatsRating:
    """The Pitt bug (beta.2 feedback): a medical drama's row filled with The Sandman, Servant,
    Torchwood and King & Conqueror — all genuinely in TMDB's lists for it, all near the bottom.

    TMDB's own ordering was the similarity signal and it was being discarded, leaving the average
    vote as the only tiebreak. The fix is only real if a WORSE-RATED, closer title now wins.
    """

    @staticmethod
    def _candidate(title: str, rating: float, affinity: float) -> Candidate:
        return Candidate(
            tmdb_id=abs(hash(title)) % 100000,
            title=title,
            media_type=MediaType.SHOW,
            rating=rating,
            affinity=affinity,
            sources={"tmdb_similar"},
            seeds=[Seed(tmdb_id=250307, title="The Pitt", media_type=MediaType.SHOW, weight=1.0)],
        )

    def test_a_close_match_outranks_a_better_rated_distant_one(self):
        # Real numbers: ER is TMDB's #1 recommendation for The Pitt; Torchwood is partway down
        # /similar, and rates higher than several of the medical dramas above it.
        er = self._candidate("ER", rating=7.8, affinity=1.0)
        torchwood = self._candidate("Torchwood", rating=7.3, affinity=0.43)
        sandman = self._candidate("The Sandman", rating=7.8, affinity=0.40)

        assert score(er) > score(torchwood)
        assert score(er) > score(sandman)
        assert [c.title for c in pre_rank([sandman, torchwood, er], keep=2)] == ["ER", "Torchwood"]

    def test_rating_alone_no_longer_decides(self):
        """The exact inversion that produced the bug: same seed, one better rated but far less
        similar. Before affinity, the higher rating simply won."""
        close = self._candidate("Chicago Med", rating=8.3, affinity=1.0)
        distant = self._candidate("Traitors", rating=9.0, affinity=0.32)

        assert score(close) > score(distant), "a 9.0 from the tail must not beat an 8.3 from the top"

    def test_a_source_with_no_ranking_is_not_penalised(self):
        """discover / Trakt / the LLM sources have no list position to offer. Defaulting them to 0
        would zero them out — the same mistake the multiplicative seed weight made originally."""
        llm_pick = Candidate(tmdb_id=1, title="AI pick", media_type=MediaType.SHOW, rating=7.0, sources={"llm_library"})

        assert llm_pick.affinity == 1.0
        assert score(llm_pick) > 0


class TestPaddingFloor:
    """A row is allowed to come up short. Filling it from the tail is how "Because you watched The
    Pitt" ended up over four titles that had nothing to do with it."""

    @staticmethod
    def _pool(affinities: list[float]) -> list[Candidate]:
        return [
            Candidate(
                tmdb_id=i,
                title=f"T{i}",
                media_type=MediaType.SHOW,
                rating=8.0,
                affinity=a,
                sources={"tmdb_similar"},
                rating_key=1000 + i,
                seeds=[Seed(tmdb_id=250307, title="The Pitt", media_type=MediaType.SHOW, weight=1.0)],
            )
            for i, a in enumerate(affinities)
        ]

    def test_a_short_row_beats_a_padded_one(self):
        from shortlist.engine.rows import _pad_picks

        padded = _pad_picks([], self._pool([0.9, 0.8, 0.2, 0.1]), k=4)

        assert [p.title for p in padded] == ["T0", "T1"], "the tail must not be delivered"

    def test_a_source_without_a_ranking_is_still_allowed_to_fill(self):
        """discover / Trakt / LLM picks sit at the neutral 1.0 — they are deliberate, not tail."""
        from shortlist.engine.rows import _pad_picks

        pool = self._pool([1.0, 1.0])
        for c in pool:
            c.sources = {"llm_library"}

        assert len(_pad_picks([], pool, k=2)) == 2


class TestRowProvenanceLogging:
    """The log has to answer "why is this here?" on its own — the reported case was diagnosed by
    querying TMDB by hand because nothing in the log said where a pick came from."""

    @staticmethod
    def _picks() -> list[Pick]:
        return [
            Pick(
                tmdb_id=1,
                rating_key=10,
                title="Chicago Med",
                rank=1,
                reason="Because you watched The Pitt",
                media_type=MediaType.SHOW,
                seed_title="The Pitt",
                sources=["tmdb_similar"],
                affinity=0.92,
            )
        ]

    @staticmethod
    def _captured(fn, level="DEBUG") -> str:
        """loguru does not route through stdlib logging, so `caplog` sees nothing — capture with a
        real sink, exactly as the file sink the Logs view reads would."""
        lines: list[str] = []
        sink_id = logger.add(lines.append, level=level, format="{message}")
        try:
            fn()
        finally:
            logger.remove(sink_id)
        return "".join(lines)

    def test_it_records_the_seed_source_and_strength_of_every_pick(self):
        from shortlist.engine.rows import _log_row_provenance

        logged = self._captured(
            lambda: _log_row_provenance(
                UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED),
                RowSpec(slug="picked", name_template="", size=1),
                SimpleNamespace(title="TV Shows"),
                self._picks(),
                [],
                1,
            )
        )
        assert "Chicago Med" in logged
        assert "The Pitt" in logged, "the seed must be in the log, not just the UI"
        assert "tmdb_similar" in logged, "which source suggested it"
        assert "0.92" in logged, "how strong the claim was"

    def test_a_short_row_says_why_it_is_short(self):
        """A deliberately-short row must not read as a failure."""
        from shortlist.engine.rows import _log_row_provenance

        pool = [
            Candidate(tmdb_id=9, title="Torchwood", media_type=MediaType.SHOW, affinity=0.28),
            Candidate(tmdb_id=8, title="The Sandman", media_type=MediaType.SHOW, affinity=0.26),
        ]

        logged = self._captured(
            lambda: _log_row_provenance(
                UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED),
                RowSpec(slug="picked", name_template="", size=5),
                SimpleNamespace(title="TV Shows"),
                self._picks(),
                pool,
                5,
            ),
            level="INFO",
        )

        assert "too loosely related" in logged
        assert "Torchwood" in logged, "naming the closest rejection makes the cut auditable"
