"""Curator protocol and web-search prompt building.

The AI providers have ONE job here: FIND titles to watch next via web search. They never rank a
candidate list or write a row's reasons — that is done in code (see ``ranking`` and ``picker``).
Every title a provider proposes is resolved to a real TMDB id and library-verified downstream, so a
hallucinated title simply resolves to nothing rather than reaching a row.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Protocol

from loguru import logger

from shortlist.engine.history import distinct_recent
from shortlist.engine.models import UserProfile


class ThreadLocalTokens:
    """A per-thread token counter, used as a class attribute on each provider curator.

    A curator is one shared instance per run, but its `last_tokens` is written inside a web-search
    call and read immediately after at the call site. When users are handled on parallel threads, a
    plain instance attribute would let one thread's call clobber another's count between its write and
    read. Storing per-thread makes each thread see the value its own last call set — no lock, no
    change at the read sites (which still just read `curator.last_tokens`)."""

    def __init__(self):
        self._local = threading.local()

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(self._local, "value", 0)

    def __set__(self, obj, value):
        self._local.value = value


class CuratorError(RuntimeError):
    """Provider call failed; the pipeline degrades to heuristic mode instead of failing the user."""


class Curator(Protocol):
    name: str
    # True when this provider can search the web itself (a native web-search tool) and so implements
    # ``recommend_web``. False for local/offline providers (Ollama) and NullCurator — they can still
    # power the ``llm_web`` source via an external search provider (Exa) feeding ``complete``.
    supports_native_web_search: bool
    # Output-token count from THIS thread's most recent web-search call, for per-run accounting. A
    # ThreadLocalTokens descriptor on the network providers; a plain 0 on NullCurator (no LLM call).
    last_tokens: int

    def complete(self, system: str, user: str) -> str:
        """Plain text completion — no tools, no schema. Powers the external-search ``llm_web`` path,
        where the app has already done the web search and just needs the model to pick titles from the
        results. Degrades to an empty string on a provider error (the source's own guard is the backstop).
        """
        ...


def taste_summary(profile: UserProfile, max_titles: int = 20) -> str:
    """Compact history summary for the prompt. Titles+years only — no PII.

    Distinct titles: a show's episodes collapse to the one show, so a binge of 20 episodes counts
    once and the model sees ``max_titles`` real, varied titles rather than the same show repeated.
    """
    recent = distinct_recent(profile.history, max_titles)
    lines = [f"- {w.title}" + (f" ({w.year})" if w.year else "") for w in recent]
    return "Recently watched (most recent first):\n" + "\n".join(lines)


# Note what this prompt does NOT ask for: tmdb_id or imdb_id. Measured 2026-09-02 against the live
# curator, only 4 of 10 proposed tmdb_ids and 6 of 10 imdb_ids were correct — and a wrong id resolves
# to a REAL but unrelated title ("Black Mirror" → "Wild China"), which reaches someone's row. A wrong
# *title* simply fails to resolve and vanishes, so title+year is the safer contract. See
# `.claude/docs/llm-web-search-upgrade.md` §3.
#
# The year is demanded rather than requested, and the prompt says why: the resolver disambiguates on
# it. At Anthropic's old `max_uses=3` only 4 of 12 proposals carried one.
# The native-search prompt. Two things in it are load-bearing and were both missing:
#
# 1. THE YEAR. A model has no reliable idea what today is, so "current" and "recent" anchor to its
#    training cutoff — which is exactly the wrong end. Measured on a 2026 run: gpt-4o-mini and
#    gpt-5-mini each returned 12 titles and NOT ONE was from 2024 or later.
# 2. AN EXPLICIT INSTRUCTION TO SEARCH. The tool being attached is not an instruction to use it.
#
# Neither is a guess. Asked "best-reviewed TV shows that premiered in 2026" — a question that names
# the year and cannot be answered from memory — both models fired real searches and returned 2026
# titles with citations. The tool works; the old prompt simply never asked it to look forward.
#
# Gemini is the exception and no prompt fixes it: it declines to search for this task under every
# phrasing tried, including `tool_config mode="ANY"`. See GoogleCurator.recommend_web.
_WEB_SYSTEM = (
    "You are a film and TV recommender with live web search. Today is in {year}, which is LATER than "
    "your training cutoff — so your own knowledge of what is new is out of date. Search the web "
    "before answering rather than recommending from memory. Search for what 'what to watch next' "
    "articles, critics' best-of lists and review sites are recommending in {year} and {last_year}. "
    "Based on what this person recently watched, give {k} titles they'd most likely want to watch "
    "next. Strongly prefer titles released in {last_year} or {year}; include something older only "
    "when it is an unusually good match for their taste. Rules: (1) never recommend a title they "
    "already watched, or another season or sequel of one; (2) ALWAYS give the exact release year — "
    "it is used to look the title up, and a missing year means the recommendation is discarded; "
    "(3) use the exact title as released, not a description of it; (4) only titles ALREADY RELEASED "
    "and watchable now — never announced, upcoming or unaired ones; (5) name a series by its series "
    "title alone, never 'Season 2' or 'Part 3'. Prefer real, findable titles over "
    'obscure guesses. Respond with ONLY a JSON array of up to {k} objects, each {{"title": str, '
    '"year": int, "media": "movie" or "show"}}. No prose.'
)


def build_web_prompt(profile: UserProfile, seeds: list, k: int, *, year: int | None = None) -> tuple[str, str]:
    """(system, user) prompts for a web-search recommendation call (the ``llm_web`` source).

    Asks the model to propose NEW titles via web search; the caller resolves each to a real TMDB id
    and library-verifies it, so a hallucinated title simply resolves to nothing rather than reaching
    a row.

    Args:
        profile: The person being recommended for — used only when no seeds carry a title.
        seeds: Their weighted recent watches; the first 20 titles anchor the request.
        k: How many titles to ask for.
        year: The current year, injected into the prompt because a model cannot be trusted to know
            it. Defaults to today's. Tests pin it so the prompt is deterministic.

    Returns:
        ``(system, user)`` — the system prompt carrying the rules, and the user prompt carrying the
        watch list.
    """
    liked = [getattr(s, "title", "") for s in seeds if getattr(s, "title", "")][:20]
    if not liked:
        liked = [w.title for w in sorted(profile.history, key=lambda w: w.watched_at, reverse=True)[:20]]
    body = "\n".join(f"- {t}" for t in liked) or "- (no history yet — recommend broadly popular titles)"
    now = year if year is not None else datetime.now(UTC).year
    system = _WEB_SYSTEM.format(k=k, year=now, last_year=now - 1)
    user = (
        f"They recently enjoyed:\n{body}\n\nSearch the web for what to watch next, then recommend "
        f"up to {k} titles. Favour things released in {now - 1} or {now}."
    )
    return system, user


_WEB_RAG_SYSTEM = (
    "You are a film and TV recommender. Below are excerpts from recent web articles about what to "
    "watch. Based on what this person recently enjoyed, pick the {k} titles mentioned in these "
    "articles they'd most likely want to watch next. Prefer real, well-reviewed, findable titles. "
    "Give the exact release year wherever the article states it — it is used to look the title up. "
    'Respond with ONLY a JSON array of up to {k} objects, each {{"title": str, "year": int or null, '
    '"media": "movie" or "show"}}. No prose.'
)

_WEB_PICK_SYSTEM = (
    "You are a film and TV recommender. Below is a list of titles that recent web articles "
    "recommend as things to watch next. Based on what this person recently enjoyed, "
    "pick the {k} they'd most likely want to watch next. Choose only from the list — do not add "
    "titles of your own. Keep each title and year exactly as written; they are used to look the "
    'title up. Respond with ONLY a JSON array of up to {k} objects, each {{"title": str, "year": '
    'int or null, "media": "movie" or "show"}}. No prose.'
)


def build_web_query_for_title(title: str) -> str:
    """A web-search query for a SINGLE watched title — the per-title external-search path.

    One query per title (vs one blended query for a whole watchlist) is both more precise — an
    eclectic watcher's kids films and prestige dramas don't muddy each other — and CACHEABLE across
    users: two people who both watched this title need the same search, so it runs once server-wide
    (Exa bills per search). Falls back to a generic query for an empty title.
    """
    clean = (title or "").strip()
    if not clean:
        return "best new well-reviewed movies and TV shows to watch right now"
    return f"what to watch next if you liked {clean} — similar recent, well-reviewed movies and TV shows"


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


def build_web_pick_prompt(profile: UserProfile, candidates: list, k: int) -> tuple[str, str]:
    """(system, user) prompts for picking from titles the SEARCH PROVIDER already extracted.

    The third and cheapest shape of the ``llm_web`` prompt. ``build_web_prompt`` asks a model to go
    and search; ``build_web_rag_prompt`` hands it article prose to read; this hands it a plain list
    of titles, because Exa's ``outputSchema`` did the reading server-side inside the price of the
    search. A candidate costs ~20 tokens here against ~200 for a prose block, which is why the
    caller's cap can be an order of magnitude larger and every seed's findings actually reach the
    model.

    The caller resolves each returned title to TMDB and library-verifies it, so a title the model
    invents rather than picks from the list reaches no row.
    """
    system = _WEB_PICK_SYSTEM.format(k=k)
    lines = []
    for c in candidates:
        year = getattr(c, "year", None)
        lines.append(
            f"- {getattr(c, 'title', '')}" + (f" ({year})" if year else "") + f" [{getattr(c, 'media', 'movie')}]"
        )
    context = "\n".join(lines) or "(no titles found)"
    user = f"{taste_summary(profile)}\n\nTitles recommended by recent articles:\n{context}\n\nPick up to {k}."
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
    # A provider answering under a JSON schema returns the array wrapped in an object, because a
    # bare top-level array is not expressible in OpenAI's strict Structured Outputs (the root must
    # be an object). Unwrap it, so the same parser serves the schema'd and the chatty replies.
    if isinstance(data, dict):
        data = data.get("titles")
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
