"""Build a row from a ranked candidate pool — the one path every run uses, with no LLM in it.

The AI providers are used only to FIND titles (web search). Everything after that is code: ranking
(``ranking.score``/``pre_rank``), spreading picks across tastes (``ranking.diversify_by_seed``), and
the per-pick "why" line. This module is where the pool becomes the final, ordered list of Picks.

Reasons are templates built from data the pool already carries (the seeding title + its genres).
They're shown only on the owner's dashboard and run report — never written to Plex — so a plain,
honest template is all they need to be; there is nothing an LLM could add here worth a token.
"""

from __future__ import annotations

from shortlist.engine import ranking
from shortlist.engine.models import Candidate, Pick

# Why a seedless pick is here, by the source that produced it. A seedless candidate has no "because
# you watched X" to point at, but the reason must still be TRUE to its source — the old blanket
# "Popular in your library" was wrong for all three (web picks aren't from the library at all) and
# contradicted the "suggested by AI web search" provenance line shown right beneath it.
_SEEDLESS_REASON = {
    "llm_web": "Suggested by AI web search",
    "tmdb_discover": "In genres you watch a lot",
    "cold_start": "Popular on this server",
}
_SEEDLESS_REASON_DEFAULT = "Matched to your taste"


def reason_for(candidate: Candidate) -> str:
    """A one-line "why you're seeing this" built from the candidate's own data.

    Prefers the genres it shares with the seeding title ("Because you liked sci-fi, action like
    Dune"), falls back to the bare seed title, and — for a seedless pick (discover / web /
    cold-start) — to a per-source line that matches how it was actually found.
    """
    seed = candidate.top_seed
    if not seed:
        for source, reason in _SEEDLESS_REASON.items():
            if source in candidate.sources:
                return reason
        return _SEEDLESS_REASON_DEFAULT
    if candidate.genres:
        genres = ", ".join(candidate.genres[:2]).lower()
        return f"Because you liked {genres} like {seed.title}"
    return f"Because you watched {seed.title}"


def build_picks(candidates: list[Candidate], k: int) -> list[Pick]:
    """The top ``k`` picks for a row: spread across the tastes that seeded them, each with a reason.

    ``candidates`` is the already-ranked pool (``ranking.pre_rank`` output — best first). This is the
    final selection step that used to be the LLM curate call: ``diversify_by_seed`` keeps one
    heavily-watched title from swallowing the whole row, and the top-scoring pick still leads.
    """
    chosen = ranking.diversify_by_seed(candidates, k) if k > 0 else []
    picks: list[Pick] = []
    for c in chosen:
        seed = c.top_seed
        picks.append(
            Pick(
                tmdb_id=c.tmdb_id,
                rating_key=c.rating_key or 0,
                title=c.title,
                rank=len(picks) + 1,
                reason=reason_for(c),
                media_type=c.media_type,
                seed_tmdb_id=seed.tmdb_id if seed else None,
                seed_title=seed.title if seed else None,
                sources=sorted(c.sources),
                affinity=c.affinity,
            )
        )
    return picks
