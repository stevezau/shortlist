"""Curator protocol, prompt building, and the hallucination-proof output validator.

The LLM is an editor, not an oracle: it only ever re-ranks candidates verified to exist in
the library. Any tmdb_id it returns that wasn't in its input is dropped and logged.
"""

from __future__ import annotations

from typing import Protocol

from loguru import logger

from rowarr.engine.models import Candidate, Pick, UserProfile

MAX_REASON_LEN = 90


class CuratorError(RuntimeError):
    """Provider call failed; the pipeline degrades to heuristic mode instead of failing the user."""


class Curator(Protocol):
    name: str

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        """Rank the top k candidates with a one-line reason each.

        Implementations must only return tmdb_ids present in `candidates`.
        """
        ...


def picks_schema() -> dict:
    """JSON schema for structured output. Length caps are enforced client-side because
    structured-output schemas don't support string constraints."""
    return {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tmdb_id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["tmdb_id", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["picks"],
        "additionalProperties": False,
    }


def taste_summary(profile: UserProfile, max_titles: int = 20) -> str:
    """Compact history summary for the prompt. Titles+years only — no PII."""
    recent = sorted(profile.history, key=lambda w: w.watched_at, reverse=True)[:max_titles]
    lines = [f"- {w.title}" + (f" ({w.year})" if w.year else "") for w in recent]
    return "Recently watched (most recent first):\n" + "\n".join(lines)


def build_prompts(profile: UserProfile, candidates: list[Candidate], k: int) -> tuple[str, str]:
    """Return (system, user) prompts for one curate call."""
    system = (
        "You curate a personal movie/TV recommendation row for one user of a private media "
        "server. From the candidate list, pick the {k} titles this user is most likely to "
        "watch next, ranked best first. Every candidate is already verified to be available; "
        "never invent titles. For each pick give one natural, specific reason under "
        f"{MAX_REASON_LEN} characters, phrased like 'Because you watched X'. Use only "
        "tmdb_id values from the candidate list."
    ).format(k=k)
    cand_lines = []
    for c in candidates:
        seed = c.top_seed.title if c.top_seed else "?"
        genres = ", ".join(c.genres[:3])
        cand_lines.append(
            f"- tmdb_id={c.tmdb_id} | {c.title}"
            + (f" ({c.year})" if c.year else "")
            + f" | {genres} | suggested because they watched: {seed}"
        )
    user = f"{taste_summary(profile)}\n\nCandidates ({len(candidates)}):\n" + "\n".join(cand_lines)
    return system, user


def validate_picks(
    raw_picks: list[dict],
    candidates: list[Candidate],
    k: int,
    provider_name: str,
) -> list[Pick]:
    """Turn raw LLM output into Picks, dropping hallucinations and enforcing the reason cap."""
    by_id = {c.tmdb_id: c for c in candidates}
    picks = []
    seen = set()
    for raw in raw_picks:
        tmdb_id = raw.get("tmdb_id")
        if tmdb_id not in by_id:
            logger.warning("{}: dropped hallucinated tmdb_id {} (not in candidate set)", provider_name, tmdb_id)
            continue
        if tmdb_id in seen:
            continue
        seen.add(tmdb_id)
        c = by_id[tmdb_id]
        reason = str(raw.get("reason") or "").strip()[:MAX_REASON_LEN]
        seed = c.top_seed
        picks.append(
            Pick(
                tmdb_id=c.tmdb_id,
                rating_key=c.rating_key or 0,
                title=c.title,
                rank=len(picks) + 1,
                reason=reason or (f"Because you watched {seed.title}" if seed else "Picked for you"),
                seed_tmdb_id=seed.tmdb_id if seed else None,
                seed_title=seed.title if seed else None,
            )
        )
        if len(picks) == k:
            break
    return picks
