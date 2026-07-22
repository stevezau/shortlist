"""Heuristic mode — the app is fully functional with zero LLM keys."""

from __future__ import annotations

from shortlist.engine.models import Candidate, Pick, UserProfile


class NullCurator:
    name = "none"
    supports_native_web_search = False  # not an LLM at all — the llm_web source no-ops for it
    last_tokens = 0  # no LLM call, so callers can read this uniformly without a getattr fallback

    def complete(self, system: str, user: str) -> str:
        """No model to call — heuristic mode contributes nothing to the external-search llm_web path."""
        return ""

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
                    media_type=c.media_type,
                    seed_tmdb_id=seed.tmdb_id if seed else None,
                    seed_title=seed.title if seed else None,
                    # Carried like every other curator does (see `validate_picks`). This is the
                    # DEFAULT curator, so forgetting it here would leave most installs with no
                    # record of where any of their picks came from.
                    sources=sorted(c.sources),
                    affinity=c.affinity,
                )
            )
        return picks
