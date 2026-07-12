from rowarr.engine.models import MediaType, Seed
from rowarr.engine.ranking import pre_rank, score
from tests.conftest import make_candidate


def seed(tmdb_id: int, weight: float) -> Seed:
    return Seed(tmdb_id=tmdb_id, title=f"seed{tmdb_id}", media_type=MediaType.MOVIE, weight=weight)


class TestScore:
    def test_seed_frequency_multiplies(self):
        one = make_candidate(1, "One", seeds=[seed(1, 1.0)])
        two = make_candidate(2, "Two", seeds=[seed(1, 1.0), seed(2, 1.0)])
        assert score(two) > score(one)

    def test_unrated_gets_neutral_prior_not_zero(self):
        unrated = make_candidate(1, "Unrated", rating=0.0)
        assert score(unrated) > 0


class TestPreRank:
    def test_keeps_top_n_by_score(self):
        cands = [make_candidate(i, f"m{i}", rating=float(i)) for i in range(1, 6)]
        top = pre_rank(cands, keep=2)
        assert [c.tmdb_id for c in top] == [5, 4]

    def test_stable_tiebreak_by_title(self):
        a = make_candidate(1, "Alpha", rating=7.0)
        b = make_candidate(2, "Beta", rating=7.0)
        assert [c.title for c in pre_rank([b, a], keep=2)] == ["Alpha", "Beta"]
