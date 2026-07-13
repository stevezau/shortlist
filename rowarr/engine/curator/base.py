"""Curator protocol, prompt building, and the hallucination-proof output validator.

The LLM is an editor, not an oracle: it only ever re-ranks candidates verified to exist in
the library. Any tmdb_id it returns that wasn't in its input is dropped and logged.
"""

from __future__ import annotations

from string import Template
from typing import Protocol

from loguru import logger

from rowarr.engine.models import Candidate, Pick, PromptConfig, UserProfile

MAX_REASON_LEN = 90

# Tone presets steer the *wording* of the reasons. Each is a clause appended after the reason
# instruction (leading space included). "balanced" adds nothing — it's the default house voice.
TONE_PRESETS = {
    "balanced": "",
    "warm": " Write the reasons warmly and enthusiastically, like a friend who can't wait for them to watch it.",
    "concise": " Keep the reasons short and punchy — no filler, just the hook.",
    "cinephile": " Write for a film buff: nod to directors, genre lineage, or craft where it fits naturally.",
    "playful": " Give the reasons a light, playful wink — fun, never cheesy.",
}

# Appended to EVERY system prompt (even a fully custom template) so the non-negotiable contract
# survives any user edit. The hallucination validator enforces it regardless; this keeps quality up.
_CONTRACT = " Use only tmdb_id values from the candidate list; never invent titles. Keep each reason brief."

_PERSONAL_SKELETON = (
    "You curate a personal movie/TV recommendation row for one user of a private media "
    "server. From the candidate list, pick the {k} titles this user is most likely to "
    "watch next, ranked best first. Every candidate is already verified to be available. "
    "For each pick give one natural, specific reason under {max_reason_len} characters, "
    "phrased like 'Because you watched X'.{tone}{guidance}"
)

_SHARED_SKELETON = (
    "You curate a 'popular on this server' movie/TV row shown to everyone on a private media "
    "server. From the candidate list, pick the {k} titles most worth surfacing to the whole "
    "group, ranked best first. Every candidate is already verified to be available. For each "
    "pick give one short reason under {max_reason_len} characters framed around broad, shared "
    "appeal (e.g. 'A lot of people here are watching this') — never 'because you watched', since "
    "this row is not personal to one viewer.{tone}{guidance}"
)


class _SafeDict(dict):
    """format_map helper: unknown ``{placeholders}`` render empty instead of raising KeyError."""

    def __missing__(self, _key: str) -> str:
        return ""


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
    """Return (system, user) prompts for one curate call.

    The system prompt is assembled from the user's tunable recipe (``profile.prompt``): a tone
    preset + free-text guidance injected into a built-in skeleton, or a full custom template. The
    fixed contract (``_CONTRACT``) is always appended.

    A custom template uses ``$name`` variables (``string.Template``): ``$k``, ``$max_reason_len``,
    ``$guidance``, ``$tone``, ``$username``. ``safe_substitute`` leaves unknown ``$vars`` as-is and
    never raises, and the ``$`` grammar has no attribute/subscript access — so a curious owner's
    template can neither crash a run nor read Python internals.
    """
    cfg = profile.prompt or PromptConfig()
    fields = {
        "k": k,
        "max_reason_len": MAX_REASON_LEN,
        "tone": TONE_PRESETS.get(cfg.tone, ""),
        "guidance": f" {cfg.guidance.strip()}" if cfg.guidance.strip() else "",
        "username": profile.username,
    }
    if cfg.template.strip():
        system = Template(cfg.template).safe_substitute(fields)
    else:
        skeleton = _SHARED_SKELETON if cfg.shared else _PERSONAL_SKELETON
        system = skeleton.format_map(_SafeDict(fields))
    system = system + _CONTRACT

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
                media_type=c.media_type,
                seed_tmdb_id=seed.tmdb_id if seed else None,
                seed_title=seed.title if seed else None,
            )
        )
        if len(picks) == k:
            break
    return picks
