"""Curator protocol, prompt building, and the hallucination-proof output validator.

The LLM is an editor, not an oracle: it only ever re-ranks candidates verified to exist in
the library. Any tmdb_id it returns that wasn't in its input is dropped and logged.
"""

from __future__ import annotations

import json
from string import Template
from typing import Protocol

from loguru import logger

from shortlist.engine.models import Candidate, Pick, PromptConfig, UserProfile

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


def log_curate_request(provider: str, model: str, system: str, user: str, n_candidates: int, k: int) -> None:
    """Observe one outgoing curate call. Prompts are titles+years only (no PII, no secrets — the
    api_key lives inside the SDK client and is never passed here), so the full text is safe to log.
    DEBUG shows the shape; TRACE adds the full prompt for anyone debugging the recipe itself.
    """
    logger.debug("curate → {} · {} · {} candidates → top {}", provider, model, n_candidates, k)
    logger.trace("curate prompt · {}\n── system ──\n{}\n── user ──\n{}", provider, system, user)


def log_curate_response(
    provider: str, model: str, n_picks: int, tokens: int, elapsed_s: float, raw: str | None = None
) -> None:
    """Observe one curate reply: pick count, token spend, and latency at DEBUG; raw text at TRACE."""
    logger.debug("curate ← {} · {} · {} picks · {} tokens · {:.2f}s", provider, model, n_picks, tokens, elapsed_s)
    if raw is not None:
        logger.trace("curate reply · {}\n{}", provider, raw[:4000])


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


_WEB_SYSTEM = (
    "You are a film and TV recommender with live web search. Based on what this person recently "
    "watched, search the web for {k} current, well-reviewed titles they'd most likely want to watch "
    "next — 'what to watch next' picks, recent releases, and critically-loved titles similar in "
    "taste. Prefer real, findable titles over obscure guesses. Respond with ONLY a JSON array of up "
    'to {k} objects, each {{"title": str, "year": int or null, "media": "movie" or "show"}}. No prose.'
)


def build_web_prompt(profile: UserProfile, seeds: list, k: int) -> tuple[str, str]:
    """(system, user) prompts for a web-search recommendation call (the ``llm_web`` source).

    Unlike ``build_prompts`` (which re-ranks a fixed candidate list), this asks the model to propose
    NEW titles via web search; the caller resolves each to a real TMDB id and library-verifies it, so
    a hallucinated title simply resolves to nothing rather than reaching a row.
    """
    liked = [getattr(s, "title", "") for s in seeds if getattr(s, "title", "")][:20]
    if not liked:
        liked = [w.title for w in sorted(profile.history, key=lambda w: w.watched_at, reverse=True)[:20]]
    body = "\n".join(f"- {t}" for t in liked) or "- (no history yet — recommend broadly popular titles)"
    system = _WEB_SYSTEM.format(k=k)
    user = f"They recently enjoyed:\n{body}\n\nRecommend up to {k} titles to watch next."
    return system, user


def parse_web_titles(text: str, limit: int) -> list[dict]:
    """Pull the JSON array of ``{title, year, media}`` out of a model's (possibly chatty) reply.

    Tolerant by design: the model is asked for pure JSON but web-search answers sometimes wrap it in
    prose, so we fall back to the outermost ``[...]`` slice. Every item is normalised; anything
    unparseable yields an empty list (the source then simply contributes nothing).
    """
    raw = (text or "").strip()
    data: object = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if 0 <= start < end:
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, list):
        logger.warning("llm_web: could not parse a title list from the model reply")
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        media = "show" if str(item.get("media") or "").lower() in ("show", "tv", "series") else "movie"
        year = item.get("year")
        out.append({"title": title, "year": int(year) if isinstance(year, int) else None, "media": media})
        if len(out) >= limit:
            break
    return out


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
