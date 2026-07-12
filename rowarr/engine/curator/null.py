"""Heuristic mode — the app is fully functional with zero LLM keys."""

from __future__ import annotations

from rowarr.engine.models import Candidate, Pick, UserProfile


class NullCurator:
    name = "none"

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        """Keep the heuristic pre-rank order; template reasons from each pick's top seed."""
        picks = []
        for c in candidates[:k]:
            seed = c.top_seed
            picks.append(
                Pick(
                    tmdb_id=c.tmdb_id,
                    rating_key=c.rating_key or 0,
                    title=c.title,
                    rank=len(picks) + 1,
                    reason=f"Because you watched {seed.title}" if seed else "Popular in your library",
                    seed_tmdb_id=seed.tmdb_id if seed else None,
                    seed_title=seed.title if seed else None,
                )
            )
        return picks
