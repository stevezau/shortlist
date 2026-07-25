"""The code-based picker that replaced the LLM curate step: reason templates + pick assembly.

`build_picks` itself is exercised end-to-end in test_pipeline.py; here we pin the reason WORDING,
whose three branches (genre / bare-seed / seedless) are otherwise only hit incidentally.
"""

from shortlist.engine.models import MediaType, Seed
from shortlist.engine.picker import build_picks, reason_for
from tests.conftest import make_candidate


def _seed() -> Seed:
    return Seed(tmdb_id=1, title="Dune", media_type=MediaType.MOVIE, weight=1.0)


class TestReasonFor:
    def test_genres_are_preferred_when_the_candidate_has_them(self):
        """The primary reason format: name the shared tastes AND the seeding title."""
        candidate = make_candidate(2, "Arrival", seeds=[_seed()], genres=["Sci-Fi", "Drama"])
        assert reason_for(candidate) == "Because you liked sci-fi, drama like Dune"

    def test_at_most_two_genres_are_named(self):
        candidate = make_candidate(2, "Arrival", seeds=[_seed()], genres=["Sci-Fi", "Drama", "Action"])
        assert reason_for(candidate) == "Because you liked sci-fi, drama like Dune"

    def test_bare_seed_when_no_genres(self):
        candidate = make_candidate(2, "Arrival", seeds=[_seed()], genres=[])
        assert reason_for(candidate) == "Because you watched Dune"

    def test_seedless_reason_names_the_source_it_came_from(self):
        """A seedless pick has no "because you watched X", but its reason must still be TRUE to its
        source — the old blanket "Popular in your library" was wrong for a web pick and contradicted
        the "suggested by AI web search" provenance shown right beneath it."""
        web = make_candidate(2, "Ron's Gone Wrong", seeds=[], genres=["Animation"], sources={"llm_web"})
        assert reason_for(web) == "Suggested by AI web search"

        discover = make_candidate(3, "Arrival", seeds=[], genres=["Sci-Fi"], sources={"tmdb_discover"})
        assert reason_for(discover) == "In genres you watch a lot"

        cold = make_candidate(4, "Heat", seeds=[], genres=[], sources={"cold_start"})
        assert reason_for(cold) == "Popular on this server"

    def test_seedless_with_no_recognised_source_gets_a_safe_default(self):
        """Never fall back to a line that claims a provenance the pick doesn't have."""
        candidate = make_candidate(2, "Arrival", seeds=[], genres=["Sci-Fi"], sources=set())
        assert reason_for(candidate) == "Matched to your taste"


class TestBuildPicks:
    def test_zero_k_yields_no_picks(self):
        pool = [make_candidate(i, f"t{i}") for i in range(3)]
        assert build_picks(pool, k=0) == []

    def test_each_pick_is_ranked_from_one_and_carries_its_reason(self):
        pool = [
            make_candidate(10, "A", seeds=[_seed()], genres=["Sci-Fi"], rating_key=100),
            make_candidate(11, "B", seeds=[_seed()], genres=[], rating_key=110),
        ]
        picks = build_picks(pool, k=2)
        assert [p.rank for p in picks] == [1, 2]
        assert all(p.reason for p in picks), "every pick explains itself"
        assert {p.tmdb_id for p in picks} == {10, 11}
