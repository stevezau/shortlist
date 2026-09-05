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
# contradicted the provenance line shown right beneath it. The web line no longer says "AI" either:
# with Exa the source runs with no AI provider at all, so claiming one on the row was untrue.
_SEEDLESS_REASON = {
    "llm_web": "Recommended on the web right now",
    "tmdb_discover": "In genres you watch a lot",
    "cold_start": "Popular on this server",
}
# Explicit precedence for a candidate whose sources include MORE THAN ONE of the above (e.g. found by
# both llm_web and tmdb_discover) — a plain `.items()` walk would pick whichever happened to be
# inserted first in the dict literal, silently coupling the reason shown to the user to source order.
_SEEDLESS_SOURCE_PRECEDENCE = ("llm_web", "tmdb_discover", "cold_start")
_SEEDLESS_REASON_DEFAULT = "Matched to your taste"


def reason_for(candidate: Candidate) -> str:
    """A one-line "why you're seeing this" built from the candidate's own data.

    Prefers the genres it shares with the seeding title ("Because you watched sci-fi, action like
    Dune"), falls back to the bare seed title, and — for a seedless pick (discover / web /
    cold-start) — to a per-source line that matches how it was actually found.

    "Watched", never "liked": a seed is a title from their history, weighted by watch count and
    recency. Ratings only ever REMOVE a seed (`history.disliked_seed_keys`) — nothing in the engine
    marks a title as liked, so claiming it would be a guess dressed up as a fact.
    """
    seed = candidate.top_seed
    if not seed:
        for source in _SEEDLESS_SOURCE_PRECEDENCE:
            if source in candidate.sources:
                return _SEEDLESS_REASON[source]
        return _SEEDLESS_REASON_DEFAULT
    if candidate.genres:
        genres = ", ".join(candidate.genres[:2]).lower()
        base = f"Because you watched {genres} like {seed.title}"
    else:
        base = f"Because you watched {seed.title}"
    return base + _extra_causes(candidate)


#: Beyond this the line stops being an explanation and becomes a list.
_MAX_EXTRA_CAUSES = 2
_REASON_MAX_CHARS = 180


def _extra_causes(candidate: Candidate) -> str:
    """The clause naming signals BEYOND the plain seed match, or "" when there is nothing to add.

    Only franchise and cast: "similarity" is what the sentence already said, and repeating it as
    "…and is similar to Dune" would pad every reason on the server for no information.

    Returns "" for the overwhelmingly common single-signal case, so the existing wording is
    byte-identical for every pick that has nothing extra to explain.
    """
    extras: list[str] = []
    for attribution in candidate.attributions:
        if attribution.signal == "franchise":
            name = attribution.detail or "the same series"
            extras.append(f"also part of {name}")
        elif attribution.signal == "cast" and attribution.detail:
            extras.append(f"shares {attribution.detail} with {attribution.seed_title}")
        if len(extras) == _MAX_EXTRA_CAUSES:
            break
    if not extras:
        return ""
    clause = " — " + ", and ".join(extras)
    # A row name is rendered next to this; an unbounded sentence wraps to three lines and buries the
    # part that mattered. Drop the extras entirely rather than truncate mid-word.
    return clause if len(clause) <= _REASON_MAX_CHARS else ""


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
                rating=c.rating,
                year=c.year,
            )
        )
    return picks
