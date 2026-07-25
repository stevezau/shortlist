"""Curator protocol and web-search prompt building.

The AI providers have ONE job here: FIND titles to watch next via web search. They never rank a
candidate list or write a row's reasons — that is done in code (see ``ranking`` and ``picker``).
Every title a provider proposes is resolved to a real TMDB id and library-verified downstream, so a
hallucinated title simply resolves to nothing rather than reaching a row.
"""

from __future__ import annotations

import json
import threading
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


_WEB_SYSTEM = (
    "You are a film and TV recommender with live web search. Based on what this person recently "
    "watched, search the web for {k} current, well-reviewed titles they'd most likely want to watch "
    "next — 'what to watch next' picks, recent releases, and critically-loved titles similar in "
    "taste. Prefer real, findable titles over obscure guesses. Respond with ONLY a JSON array of up "
    'to {k} objects, each {{"title": str, "year": int or null, "media": "movie" or "show"}}. No prose.'
)


def build_web_prompt(profile: UserProfile, seeds: list, k: int) -> tuple[str, str]:
    """(system, user) prompts for a web-search recommendation call (the ``llm_web`` source).

    Asks the model to propose NEW titles via web search; the caller resolves each to a real TMDB id
    and library-verifies it, so a hallucinated title simply resolves to nothing rather than reaching
    a row.
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
