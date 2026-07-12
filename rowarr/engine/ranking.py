"""Heuristic pre-rank: seed_frequency x rating x seed weight. Also the full ranking in None-mode."""

from __future__ import annotations

from rowarr.engine.models import Candidate


def score(candidate: Candidate) -> float:
    seed_weight = max((s.weight for s in candidate.seeds), default=0.0)
    rating = candidate.rating or 5.0  # unrated titles get a neutral prior, not zero
    return candidate.seed_frequency * rating * (1.0 + seed_weight)


def pre_rank(candidates: list[Candidate], keep: int) -> list[Candidate]:
    """Top `keep` candidates by heuristic score (ties broken by rating, then title for stability)."""
    return sorted(candidates, key=lambda c: (-score(c), -c.rating, c.title))[:keep]
