"""Curator protocol, prompt building, and the hallucination-proof output validator.

The LLM is an editor, not an oracle: it only ever re-ranks candidates verified to exist in
the library. Any tmdb_id it returns that wasn't in its input is dropped and logged.
"""

from __future__ import annotations

import json
import threading
from string import Template
from typing import Protocol

from loguru import logger

from shortlist.engine.models import Candidate, Pick, PromptConfig, UserProfile

MAX_REASON_LEN = 90


class ThreadLocalTokens:
    """A per-thread token counter, used as a class attribute on each provider curator.

    A curator is one shared instance per run, but its `last_tokens` is written inside `curate` and
    read immediately after at the call site. When users are curated on parallel threads, a plain
    instance attribute would let one thread's `curate` clobber another's count between its write and
    read. Storing per-thread makes each thread see the value its own last `curate` set — no lock, no
    change at the read sites (which still just read `curator.last_tokens`)."""

    def __init__(self):
        self._local = threading.local()

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(self._local, "value", 0)

    def __set__(self, obj, value):
        self._local.value = value


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

# The built-in skeletons rendered as EDITABLE full prompts: $-style variables (matching the custom
# `template` renderer in build_prompts, which uses string.Template), and NO {tone}/{guidance} injection
# points (an owner writing the whole prompt supplies the wording themselves). Offered as the starting
# point in the "write the whole prompt" UI; build_prompts still appends _CONTRACT, so the safety rules
# can't be edited away.
_PERSONAL_TEMPLATE = (
    "You curate a personal movie/TV recommendation row for one user of a private media server. "
    "From the candidate list, pick the $k titles this user is most likely to watch next, ranked "
    "best first. Every candidate is already verified to be available. For each pick give one natural, "
    "specific reason under $max_reason_len characters, phrased like 'Because you watched X'."
)
_SHARED_TEMPLATE = (
    "You curate a 'popular on this server' movie/TV row shown to everyone on a private media server. "
    "From the candidate list, pick the $k titles most worth surfacing to the whole group, ranked best "
    "first. Every candidate is already verified to be available. For each pick give one short reason "
    "under $max_reason_len characters framed around broad, shared appeal (e.g. 'A lot of people here "
    "are watching this') — never 'because you watched', since this row is not personal to one viewer."
)


def default_prompt_template(shared: bool = False) -> str:
    """The built-in curation prompt as an editable ``$``-style template — the starting point offered to
    an owner who wants to write the whole prompt themselves. Variables: ``$k`` (row size), ``$username``,
    ``$max_reason_len``. The safety contract (``_CONTRACT``) is still appended at render time, so it can
    never be edited out."""
    return _SHARED_TEMPLATE if shared else _PERSONAL_TEMPLATE


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
    # True when this provider can search the web itself (a native web-search tool) and so implements
    # ``recommend_web``. False for local/offline providers (Ollama) and NullCurator — they can still
    # power the ``llm_web`` source via an external search provider (Exa) feeding ``complete``.
    supports_native_web_search: bool
    # Output-token count from THIS thread's most recent curate call, for per-run accounting. A
    # ThreadLocalTokens descriptor on the network providers; a plain 0 on NullCurator (no LLM call).
    last_tokens: int

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        """Rank the top k candidates with a one-line reason each.

        Implementations must only return tmdb_ids present in `candidates`.
        """
        ...

    def complete(self, system: str, user: str) -> str:
        """Plain text completion — no tools, no schema. Powers the external-search ``llm_web`` path,
        where the app has already done the web search and just needs the model to pick titles from the
        results. Degrades to an empty string on a provider error (the source's own guard is the backstop).
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


_WEB_RAG_SYSTEM = (
    "You are a film and TV recommender. Below are excerpts from recent web articles about what to "
    "watch. Based on what this person recently enjoyed, pick the {k} titles mentioned in these "
    "articles they'd most likely want to watch next. Prefer real, well-reviewed, findable titles. "
    'Respond with ONLY a JSON array of up to {k} objects, each {{"title": str, "year": int or null, '
    '"media": "movie" or "show"}}. No prose.'
)


def build_web_query(profile: UserProfile, seeds: list) -> str:
    """A natural-language web-search query built from what this person recently enjoyed.

    Used by the EXTERNAL-search ``llm_web`` path (Exa): the app runs this query, then hands the
    results to any curator. Falls back to recent history titles, then to a generic query when a
    person has no history yet (cold start).
    """
    liked = [getattr(s, "title", "") for s in seeds if getattr(s, "title", "")][:8]
    if not liked:
        liked = [w.title for w in sorted(profile.history, key=lambda w: w.watched_at, reverse=True)[:8]]
    if not liked:
        return "best new well-reviewed movies and TV shows to watch right now"
    return "what to watch next if you liked " + ", ".join(liked) + " — recent, well-reviewed movies and TV shows"


def build_web_rag_prompt(profile: UserProfile, results: list, k: int) -> tuple[str, str]:
    """(system, user) prompts for recommending titles from web-search RESULTS the app already fetched.

    Unlike ``build_web_prompt`` (which asks a native-search model to search for itself), this embeds
    the article snippets we retrieved so an offline/local model can recommend from them. The caller
    resolves each returned title to TMDB and library-verifies it, so a bad title reaches no row.
    """
    system = _WEB_RAG_SYSTEM.format(k=k)
    blocks = [f"## {getattr(r, 'title', '')}\n{(getattr(r, 'text', '') or '')[:800]}" for r in results]
    context = "\n\n".join(blocks) or "(no web results found)"
    user = f"{taste_summary(profile)}\n\nWeb articles:\n{context}\n\nRecommend up to {k} titles to watch next."
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
