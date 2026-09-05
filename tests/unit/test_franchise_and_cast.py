"""Wave 3 items 3-5: per-signal attribution, franchise awareness, and the IDF-discounted cast signal.

All three dials default OFF, so any test here that does not set one is also asserting that an
existing install is untouched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shortlist.engine import candidates as candidates_mod
from shortlist.engine import ranking
from shortlist.engine.models import Attribution, Candidate, MediaType, Seed
from shortlist.engine.picker import reason_for


def seed(tmdb_id: int, title: str, media_type: MediaType = MediaType.MOVIE) -> Seed:
    return Seed(tmdb_id=tmdb_id, title=title, media_type=media_type, weight=1.0)


def candidate(tmdb_id: int, *, title: str = "c", media_type: MediaType = MediaType.MOVIE, seeds=None) -> Candidate:
    return Candidate(
        tmdb_id=tmdb_id,
        title=title,
        media_type=media_type,
        rating=7.0,
        seeds=seeds if seeds is not None else [seed(1, "Dune")],
    )


class TestFranchiseFactor:
    def test_neutral_when_off_or_not_a_member(self):
        assert ranking.franchise_factor(True, 0.0) == 1.0
        assert ranking.franchise_factor(False, 1.0) == 1.0

    def test_boosts_a_member_at_full_strength(self):
        assert ranking.franchise_factor(True, 1.0) == pytest.approx(1 + ranking.FRANCHISE_BOOST_MAX)

    def test_a_sequel_never_beats_a_much_better_match_on_the_boost_alone(self):
        """The bound is the point. "Continues the same story" is strong evidence, not a trump card —
        a mediocre sequel must still lose to a clearly better-matched title."""
        weak_sequel = Candidate(
            tmdb_id=1, title="Sequel", media_type=MediaType.MOVIE, rating=4.0, in_seed_franchise=True
        )
        strong_other = Candidate(tmdb_id=2, title="Other", media_type=MediaType.MOVIE, rating=9.0)

        assert ranking.score(weak_sequel, franchise=1.0) < ranking.score(strong_other, franchise=1.0)


class TestMarkFranchiseMembers:
    def _tmdb(self, *, collection=None, members=()):
        tmdb = MagicMock()
        tmdb.details.return_value = {"belongs_to_collection": collection}
        tmdb.collection_members.return_value = set(members)
        return tmdb

    def test_a_sequel_found_through_its_seeds_collection_is_flagged(self):
        pool = [candidate(101, title="Dune Part Two"), candidate(999, title="Unrelated")]
        tmdb = self._tmdb(collection={"id": 7, "name": "Dune Collection"}, members=[101])

        candidates_mod.mark_franchise_members(pool, tmdb)

        assert pool[0].in_seed_franchise is True
        assert pool[1].in_seed_franchise is False

    def test_it_records_which_seed_and_which_franchise(self):
        pool = [candidate(101)]
        tmdb = self._tmdb(collection={"id": 7, "name": "Dune Collection"}, members=[101])

        candidates_mod.mark_franchise_members(pool, tmdb)

        assert pool[0].attributions == [Attribution("franchise", "Dune", 1, "Dune Collection")]

    def test_shows_are_never_flagged_and_cost_no_calls(self):
        """TMDB has no `belongs_to_collection` for TV. A show must not be looked up at all — and a
        coincidental id match across the movie/TV namespaces must not flag it either."""
        pool = [candidate(101, media_type=MediaType.SHOW, seeds=[seed(1, "Dune", MediaType.SHOW)])]
        tmdb = self._tmdb(collection={"id": 7, "name": "X"}, members=[101])

        candidates_mod.mark_franchise_members(pool, tmdb)

        assert pool[0].in_seed_franchise is False
        tmdb.details.assert_not_called()

    def test_a_seed_in_no_collection_costs_no_member_lookup(self):
        pool = [candidate(101)]
        tmdb = self._tmdb(collection=None)

        candidates_mod.mark_franchise_members(pool, tmdb)

        assert pool[0].in_seed_franchise is False
        tmdb.collection_members.assert_not_called()

    def test_an_unreachable_seed_costs_the_signal_not_the_run(self):
        pool = [candidate(101)]
        tmdb = MagicMock()
        tmdb.details.side_effect = RuntimeError("tmdb down")

        candidates_mod.mark_franchise_members(pool, tmdb)  # must not raise

        assert pool[0].in_seed_franchise is False


class TestCastIdf:
    def test_a_prolific_actor_is_discounted_far_below_a_rare_one(self):
        """The failure this exists to prevent: someone in 40 of 50 pooled titles makes all 40 look
        related to each other."""
        pool = [{"Prolific"} for _ in range(40)] + [{"Prolific", "Rare"}] + [{"Prolific"} for _ in range(9)]

        idf = candidates_mod.cast_idf(pool)

        assert idf["Rare"] > idf["Prolific"] * 2

    def test_an_empty_pool_yields_no_weights(self):
        assert candidates_mod.cast_idf([]) == {}

    def test_no_shared_cast_scores_zero(self):
        assert candidates_mod.cast_overlap_score({"A"}, {"B"}, {"A": 4.0, "B": 4.0}) == 0.0

    def test_one_rare_shared_lead_saturates(self):
        assert candidates_mod.cast_overlap_score({"Rare"}, {"Rare"}, {"Rare": 5.0}) == 1.0

    def test_a_ubiquitous_shared_name_contributes_only_a_fraction(self):
        score = candidates_mod.cast_overlap_score({"Prolific"}, {"Prolific"}, {"Prolific": 1.0})

        assert 0.0 < score < 0.5


class TestEnrichCastAffinity:
    def _tmdb(self, casts: dict[int, list[str]]):
        tmdb = MagicMock()
        tmdb.top_cast.side_effect = lambda tid, _mt, _n=5: casts.get(tid, [])
        return tmdb

    def test_it_orders_but_never_changes_membership(self):
        """Mirrors TestRankAgainstPoolOrdersOnly. A re-ranking step that quietly drops a candidate is
        invisible in a diff and catastrophic in a row."""
        ranked = [candidate(10), candidate(11), candidate(12)]
        before = {c.tmdb_id for c in ranked}
        tmdb = self._tmdb({1: ["Lead"], 10: ["Lead"], 11: [], 12: ["Someone"]})

        candidates_mod.enrich_cast_affinity(ranked, tmdb, [seed(1, "Dune")])

        assert {c.tmdb_id for c in ranked} == before
        assert len(ranked) == 3

    def test_a_shared_lead_raises_the_overlap_and_is_attributed(self):
        ranked = [candidate(10)]
        tmdb = self._tmdb({1: ["Timothee Chalamet"], 10: ["Timothee Chalamet"]})

        candidates_mod.enrich_cast_affinity(ranked, tmdb, [seed(1, "Dune")])

        assert ranked[0].cast_overlap > 0
        assert ranked[0].attributions[0].signal == "cast"
        assert ranked[0].attributions[0].detail == "Timothee Chalamet"

    def test_no_shared_cast_leaves_the_candidate_untouched(self):
        ranked = [candidate(10)]
        tmdb = self._tmdb({1: ["A"], 10: ["B"]})

        candidates_mod.enrich_cast_affinity(ranked, tmdb, [seed(1, "Dune")])

        assert ranked[0].cast_overlap == 0.0
        assert ranked[0].attributions == []

    def test_an_unreadable_cast_list_is_no_signal_not_a_failure(self):
        ranked = [candidate(10)]
        tmdb = MagicMock()
        tmdb.top_cast.side_effect = RuntimeError("tmdb down")

        candidates_mod.enrich_cast_affinity(ranked, tmdb, [seed(1, "Dune")])  # must not raise

        assert ranked[0].cast_overlap == 0.0


class TestScoreStaysBackwardsCompatible:
    def test_every_new_dial_is_exactly_one_at_its_default(self):
        c = Candidate(
            tmdb_id=1,
            title="x",
            media_type=MediaType.MOVIE,
            rating=7.0,
            genre_penalty=-2.0,
            in_seed_franchise=True,
            cast_overlap=1.0,
        )

        assert ranking.score(c) == pytest.approx(ranking.score(c, genre_avoidance=0.0, franchise=0.0, cast=0.0))

    def test_the_signals_are_carried_but_inert_until_a_dial_moves(self):
        """The signals ride on the candidate whether or not the owner asked for them, so the DIAL has
        to be what gates them — not whether the data happens to be present."""
        plain = Candidate(tmdb_id=1, title="x", media_type=MediaType.MOVIE, rating=7.0)
        loaded = Candidate(
            tmdb_id=1,
            title="x",
            media_type=MediaType.MOVIE,
            rating=7.0,
            in_seed_franchise=True,
            cast_overlap=1.0,
        )

        assert ranking.score(plain) == pytest.approx(ranking.score(loaded))


class TestReasonFor:
    def test_a_single_signal_reads_exactly_as_before(self):
        c = candidate(10, title="Arrival")
        c.genres = ["Sci-Fi"]

        assert reason_for(c) == "Because you watched sci-fi like Dune"

    def test_a_franchise_match_adds_a_clause(self):
        c = candidate(10)
        c.attributions.append(Attribution("franchise", "Dune", 1, "the Dune saga"))

        assert reason_for(c) == "Because you watched Dune — also part of the Dune saga"

    def test_a_cast_match_names_the_actor_and_the_seed(self):
        c = candidate(10)
        c.attributions.append(Attribution("cast", "Dune", 1, "Zendaya"))

        assert reason_for(c) == "Because you watched Dune — shares Zendaya with Dune"

    def test_similarity_alone_adds_nothing(self):
        """The sentence already said it; repeating it would pad every reason on the server."""
        c = candidate(10)
        c.attributions.append(Attribution("similarity", "Dune", 1))

        assert reason_for(c) == "Because you watched Dune"

    def test_it_stops_at_two_extra_causes(self):
        c = candidate(10)
        c.attributions.extend(
            [
                Attribution("franchise", "Dune", 1, "the Dune saga"),
                Attribution("cast", "Dune", 1, "Zendaya"),
                Attribution("cast", "Arrival", 2, "Someone Else"),
            ]
        )

        assert reason_for(c).count(" with ") + reason_for(c).count("part of") == 2

    def test_an_overlong_clause_is_dropped_rather_than_truncated_mid_word(self):
        c = candidate(10)
        c.attributions.append(Attribution("franchise", "Dune", 1, "x" * 300))

        assert reason_for(c) == "Because you watched Dune"


class TestTheDialsReachTheLayerRowsActuallyCalls:
    """`rows.py` calls `cut_for_recency`, never `score` directly.

    Every other test in this file drives `score` or the factor functions, so all of them passed while
    `cut_for_recency` accepted `franchise` and `cast` and forwarded neither — the dials were inert at
    every value, not just at their defaults, and the suite could not see it. That is a bug-blind
    LAYER rather than a bug-blind assertion: the tests were right about the thing they tested.
    """

    def _pool(self) -> list[Candidate]:
        plain = Candidate(tmdb_id=1, title="plain", media_type=MediaType.MOVIE, rating=7.0)
        sequel = Candidate(tmdb_id=2, title="sequel", media_type=MediaType.MOVIE, rating=7.0, in_seed_franchise=True)
        shared_cast = Candidate(
            tmdb_id=3, title="shares-cast", media_type=MediaType.MOVIE, rating=7.0, cast_overlap=1.0
        )
        return [plain, sequel, shared_cast]

    def _order(self, **dials) -> list[str]:
        return [c.title for c in ranking.cut_for_recency(self._pool(), [MediaType.MOVIE], 3, 0.0, 0, **dials)]

    def test_the_franchise_dial_changes_the_order_through_cut_for_recency(self):
        assert self._order(franchise=1.0)[0] == "sequel"

    def test_the_cast_dial_changes_the_order_through_cut_for_recency(self):
        assert self._order(cast=1.0)[0] == "shares-cast"

    def test_both_dials_off_leaves_the_order_alone(self):
        """The backward-compatibility half: identical ratings, so nothing may reorder on the signals
        alone when the dials are down."""
        assert self._order() == self._order(franchise=0.0, cast=0.0)

    def test_a_dial_at_full_strength_is_not_identical_to_off(self):
        """The assertion that would have caught the silent no-op: if turning a dial to 1.0 produces
        the same order as 0.0 on a pool built to be reordered by it, it is not wired up."""
        assert self._order(franchise=1.0) != self._order(franchise=0.0)
        assert self._order(cast=1.0) != self._order(cast=0.0)


class TestACarriedForwardPickKeepsItsRatingKey:
    """A reused pick is rebuilt from the database with `rating_key=0`, because the right key is
    per-library and only known once a section is chosen. Delivery corrected a COPY on its way to
    Plex, so Plex was always right — but the list that gets RECORDED kept the zero, and on a settled
    server that is most picks (94.6% of one real run).

    Nothing depended on the stored value until pick artwork started keying on it, at which point
    almost every row showed a placeholder instead of a poster. This is the regression test for that.
    """

    def _pick(self, *, rating_key: int, section_key: str = "1") -> object:
        from shortlist.engine.models import Pick

        return Pick(
            tmdb_id=42,
            rating_key=rating_key,
            title="Dune",
            rank=1,
            reason="because",
            media_type=MediaType.MOVIE,
            section_key=section_key,
        )

    def _ctx(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.section_index = {"1": {42: 998877}}
        return ctx

    def test_a_zero_key_is_resolved_from_that_sections_index(self):
        from shortlist.engine.rows import _with_resolved_rating_key

        resolved = _with_resolved_rating_key(self._ctx(), self._pick(rating_key=0))

        assert resolved.rating_key == 998877, "a carried-forward pick was recorded without its key"

    def test_a_real_key_is_never_overwritten(self):
        """The pick may be offered to several sections; only a MISSING key may be filled, or a row
        could be recorded against the wrong library's object."""
        from shortlist.engine.rows import _with_resolved_rating_key

        resolved = _with_resolved_rating_key(self._ctx(), self._pick(rating_key=123456))

        assert resolved.rating_key == 123456

    def test_a_title_absent_from_that_library_is_left_alone(self):
        from shortlist.engine.rows import _with_resolved_rating_key

        pick = self._pick(rating_key=0, section_key="9")  # a section with no index entry

        assert _with_resolved_rating_key(self._ctx(), pick).rating_key == 0

    def test_a_pick_with_no_section_is_left_alone(self):
        from shortlist.engine.rows import _with_resolved_rating_key

        assert _with_resolved_rating_key(self._ctx(), self._pick(rating_key=0, section_key="")).rating_key == 0


class TestCastEnrichmentIsIdempotentAndLossless:
    """Both cut sites enrich the SAME shared Candidate objects — `_candidate_pool` on the pool's cut,
    and `RowPolicy.cut_at_recency` on an overlapping subset for a row overriding recency."""

    def _tmdb(self, casts: dict[int, list[str]], *, fail: bool = False):
        tmdb = MagicMock()
        if fail:
            tmdb.top_cast.side_effect = RuntimeError("tmdb 429")
        else:
            tmdb.top_cast.side_effect = lambda tid, _mt, _n=5: casts.get(tid, [])
        return tmdb

    def test_enriching_twice_leaves_exactly_one_cast_reason(self):
        """Appending blindly produced "shares Zendaya with Dune, and shares Zendaya with Dune", and a
        third row overriding recency stacked a third copy."""
        c = candidate(10)
        tmdb = self._tmdb({1: ["Zendaya"], 10: ["Zendaya"]})

        candidates_mod.enrich_cast_affinity([c], tmdb, [seed(1, "Dune")])
        candidates_mod.enrich_cast_affinity([c], tmdb, [seed(1, "Dune")])

        assert len([a for a in c.attributions if a.signal == "cast"]) == 1
        assert reason_for(c).count("shares Zendaya") == 1

    def test_a_failed_second_read_does_not_strip_the_reason_it_cannot_rebuild(self):
        """The boost rides on `cast_overlap`, which survives a failed pass. Clearing the attribution
        first meant ranking kept the boost while the row stopped explaining it."""
        c = candidate(10)
        candidates_mod.enrich_cast_affinity([c], self._tmdb({1: ["Zendaya"], 10: ["Zendaya"]}), [seed(1, "Dune")])
        before = c.cast_overlap

        candidates_mod.enrich_cast_affinity([c], self._tmdb({}, fail=True), [seed(1, "Dune")])

        assert c.cast_overlap == before, "the boost survived"
        assert [a for a in c.attributions if a.signal == "cast"], "so the explanation must survive too"

    def test_a_second_pass_that_finds_nothing_shared_clears_the_reason(self):
        """The honest case: a real read that finds no shared cast must remove a stale claim."""
        c = candidate(10)
        candidates_mod.enrich_cast_affinity([c], self._tmdb({1: ["Zendaya"], 10: ["Zendaya"]}), [seed(1, "Dune")])

        candidates_mod.enrich_cast_affinity([c], self._tmdb({1: ["Zendaya"], 10: ["Someone Else"]}), [seed(1, "Dune")])

        assert [a for a in c.attributions if a.signal == "cast"] == []
