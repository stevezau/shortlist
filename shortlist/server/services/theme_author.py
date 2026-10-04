"""Author a theme (#138) from a brief with ONE AI call, then check it against TMDB.

The model proposes a name, rules, tags, genres and titles. Nothing it says is trusted: titles resolve
through TMDB search (a made-up one resolves to nothing), tags through TMDB's keyword search, genres through
the engine's genre list, and the rules are applied by ``load_theme`` from TMDB's own data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.curator.base import Curator, taste_summary
from shortlist.engine.models import MediaType, RowLimits, UserProfile, slugify
from shortlist.engine.seasons import _CollectionReader
from shortlist.engine.themes import _MOVIE_GENRE_IDS, ThemePick, ThemeSpec, load_theme

__all__ = [
    "BUILD_SYSTEM_GUIDANCE",
    "BUILD_SYSTEM_MECHANICS",
    "ThemeAuthorError",
    "ThemeDiff",
    "ThemeDraft",
    "ThemeStats",
    "author_theme",
    "diff_themes",
]

# The editable half: what makes a good theme. The owner may replace it.
BUILD_SYSTEM_GUIDANCE = (
    "You curate themed rows of films and TV for a home media server. From the owner's brief, design one "
    "themed row: a short evocative name, one emoji, and the titles that best fit the theme. Favour titles "
    "that fit the brief closely over famous ones, and mix well-known picks with a few lesser-known ones. "
)

# The locked half: the schema the code parses and the rules that keep titles real. Never replaced by an
# owner's guidance, because a prompt without it returns prose the parser cannot read.
BUILD_SYSTEM_MECHANICS = (
    "Respond with ONLY one JSON object, no prose and no markdown, shaped exactly like "
    '{"name": str, "emoji": str, "rules": {"max_runtime": int or null, "min_year": int or null, '
    '"max_year": int or null, "min_rating": number or null}, "tags": [str], "genres": [str], '
    '"titles": [{"title": str, "year": int, "reason": str}]}. '
    "Give about 60 titles. Real, released titles only, spelled exactly as released, each with its release "
    "year; never invent a title. Each reason is at most 12 words. Set a rule only when the brief asks for "
    "that limit, otherwise null. tags are short TMDB keyword phrases; genres are TMDB genre names."
)

_MAX_REASON = 160
_MAX_TITLES = 60
_MAX_TAGS = 10
_MAX_GENRES = 10
_MAX_NAME = 60
_MAX_PHRASE = 120
_MAX_BRIEF = 1000
_MARKUP = re.compile(r"[*_`#\[\]{}<>|~\\]")


class ThemeAuthorError(Exception):
    """The theme could not be written; ``str(error)`` is plain English for the owner."""


@dataclass(frozen=True)
class ThemeStats:
    """How the AI's proposal fared: titles named, found on TMDB, held by the libraries, left after rules."""

    named: int
    resolved: int
    in_library: int
    after_rules: int
    unwatched_median: int | None


@dataclass(frozen=True)
class ThemeDraft:
    spec: ThemeSpec
    brief: str
    stats: ThemeStats
    tokens: int
    ai_reasons: dict[tuple[MediaType, int], str]
    titles: dict[tuple[MediaType, int], str] = field(default_factory=dict)


@dataclass(frozen=True)
class ThemeDiff:
    rules_changed: bool
    added: list[str]
    removed: list[str]
    added_count: int
    removed_count: int


def author_theme(
    *,
    brief: str,
    media: MediaType | tuple[MediaType, ...],
    curator: Curator,
    tmdb: TmdbClient,
    plex: _CollectionReader,
    library_index: dict[MediaType, dict[int, int]],
    profile: UserProfile | None = None,
    current: ThemeSpec | None = None,
    guidance: str = "",
) -> ThemeDraft:
    """Write a theme from ``brief``, or refine ``current`` by it.

    ``profile`` is the person a personal theme is for; None means a shared theme, which sends no watch
    history. Only titles and years go to the model, never an account name.

    Raises:
        ThemeAuthorError: no AI provider, or the model's answer was empty or unusable.
    """
    medias = (media,) if isinstance(media, MediaType) else tuple(media)
    if getattr(curator, "name", "") == "none" or getattr(curator, "can_complete", True) is False:
        raise ThemeAuthorError("Writing a theme needs an AI provider. Add one in Settings, then try again.")
    system = (guidance.strip() or BUILD_SYSTEM_GUIDANCE.strip()) + " " + BUILD_SYSTEM_MECHANICS
    brief = brief.strip()[:_MAX_BRIEF]
    user = _user_message(brief, medias, profile, current, tmdb)
    try:
        raw = curator.complete(system, user)
    except Exception as exc:
        # Class name only: an SDK's message can carry a fragment of the key.
        logger.warning("theme author: AI call failed ({})", type(exc).__name__)
        raise ThemeAuthorError(
            "The AI provider didn't respond. Check the provider in Settings and try again."
        ) from None
    tokens = int(getattr(curator, "last_tokens", 0) or 0)
    if not (raw or "").strip():
        raise ThemeAuthorError("The AI did not answer. Try again in a moment.")
    proposal = _parse(raw)

    picks, titles, named = _resolve_titles(proposal, medias, tmdb)
    tags = _resolve_tags(proposal, tmdb)
    genres = tuple(g for g in _strings(proposal.get("genres")) if g.strip().lower() in _MOVIE_GENRE_IDS)[:_MAX_GENRES]
    if not picks and not tags and not genres:
        raise ThemeAuthorError(
            "The AI didn't suggest anything Shortlist could find. Try describing the row differently."
        )
    name = _clean(str(proposal.get("name") or ""))[:_MAX_NAME].strip() or _clean(brief)[:40] or "Themed row"
    kept = [p for p in current.picks if p.origin != "ai" and (p.media, p.tmdb_id) not in titles] if current else []
    spec = ThemeSpec(
        slug=current.slug if current else (slugify(name) or "theme"),
        name=current.name if current else name,
        emoji=_emoji(proposal.get("emoji")) or (current.emoji if current else None),
        media=medias,
        tags=tags,
        genres=genres,
        excluded_genres=current.excluded_genres if current else (),
        collections=current.collections if current else (),
        picks=tuple(picks) + tuple(kept),
        rules=_rules(proposal.get("rules")),
        min_votes=current.min_votes if current else None,
    )
    loaded = load_theme(tmdb, plex, spec, library_index)
    after_rules = sum(len(found) for found in loaded.titles.ids.values())
    held = sum(len(found) for found in loaded.titles.in_library.values())
    # Rules come from TMDB, so a pick the rules drop must not be offered as the AI's reason for a row.
    kept_reasons = {(p.media, p.tmdb_id): p.reason for p in picks if (p.media, p.tmdb_id) in loaded.reasons}
    stats = ThemeStats(
        named=named, resolved=len(picks), in_library=held, after_rules=after_rules, unwatched_median=None
    )
    logger.info("theme authored: {} named, {} resolved, {} after rules", named, len(picks), after_rules)
    return ThemeDraft(
        spec=spec,
        brief=brief,
        stats=stats,
        tokens=tokens,
        ai_reasons={key: reason for key, reason in kept_reasons.items() if reason},
        titles=titles,
    )


def diff_themes(old: ThemeSpec, new: ThemeSpec, titles: dict[tuple[MediaType, int], str] | None = None) -> ThemeDiff:
    """What a refinement changed: whether the rules moved, and which picks came or went.

    ``titles`` names picks by (media, id); a pick it does not know is listed by its id.
    """
    names = titles or {}
    before = {(p.media, p.tmdb_id) for p in old.picks}
    after = {(p.media, p.tmdb_id) for p in new.picks}

    def label(key: tuple[MediaType, int]) -> str:
        return names.get(key) or str(key[1])

    added = sorted(label(k) for k in after - before)
    removed = sorted(label(k) for k in before - after)
    return ThemeDiff(
        rules_changed=old.rules.fingerprint() != new.rules.fingerprint(),
        added=added,
        removed=removed,
        added_count=len(added),
        removed_count=len(removed),
    )


def _user_message(
    brief: str,
    medias: tuple[MediaType, ...],
    profile: UserProfile | None,
    current: ThemeSpec | None,
    tmdb: TmdbClient,
) -> str:
    parts = [
        f"Brief: <brief>{brief}</brief>",
        "Media: " + " and ".join("movies" if m is MediaType.MOVIE else "shows" for m in medias),
    ]
    if current is not None:
        parts.append("Current theme (refine it by the brief, keeping what still fits):\n" + _describe(current, tmdb))
    if profile is not None and profile.history:
        parts.append("Tailor it to this person's taste.\n" + taste_summary(profile, 20))
    return "\n\n".join(parts)


def _describe(spec: ThemeSpec, tmdb: TmdbClient) -> str:
    titles = []
    for pick in spec.picks:
        item = tmdb.list_item(pick.tmdb_id, pick.media)
        if item:
            titles.append(str(item.get("title") or item.get("name") or ""))
    rules = {k: v for k, v in vars(spec.rules).items() if v is not None}
    return json.dumps(
        {"name": spec.name, "tag_ids": list(spec.tags), "genres": list(spec.genres), "rules": rules, "titles": titles}
    )


def _parse(raw: str) -> dict:
    """The JSON object in ``raw``, tolerating a ```json fence and prose around it."""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ThemeAuthorError("The AI's answer was not a theme I could read. Try again, or reword the brief.")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        raise ThemeAuthorError(
            "The AI's answer was not a theme I could read. Try again, or reword the brief."
        ) from None
    if not isinstance(data, dict):
        raise ThemeAuthorError("The AI's answer was not a theme I could read. Try again, or reword the brief.")
    return data


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip()[:_MAX_PHRASE] for v in value if isinstance(v, str) and v.strip()]


def _clean(text: str) -> str:
    return " ".join(_MARKUP.sub(" ", text).split())


def _emoji(value: object) -> str | None:
    return value.strip()[:8] if isinstance(value, str) and value.strip() else None


def _number(value: object, kind: type) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return kind(value)


def _rules(value: object) -> RowLimits:
    raw = value if isinstance(value, dict) else {}
    return RowLimits(
        max_runtime=_number(raw.get("max_runtime"), int),
        min_year=_number(raw.get("min_year"), int),
        max_year=_number(raw.get("max_year"), int),
        min_rating=_number(raw.get("min_rating"), float),
    )


def _resolve_titles(
    proposal: dict, medias: tuple[MediaType, ...], tmdb: TmdbClient
) -> tuple[list[ThemePick], dict[tuple[MediaType, int], str], int]:
    raw = proposal.get("titles")
    entries = [
        e for e in (raw if isinstance(raw, list) else []) if isinstance(e, dict) and isinstance(e.get("title"), str)
    ]
    entries = [e for e in entries if e["title"].strip()][:_MAX_TITLES]
    picks: list[ThemePick] = []
    titles: dict[tuple[MediaType, int], str] = {}
    seen: set[tuple[MediaType, int]] = set()
    for entry in entries:
        year = _number(entry.get("year"), int)
        title = entry["title"].strip()[:_MAX_PHRASE]
        for media in medias:
            try:
                hit = tmdb.search(title, media, year=year)
                key = (media, int(hit["id"])) if hit else None
            except Exception:
                logger.warning("theme author: TMDB search failed for a title")
                continue
            if key is None or key in seen:
                continue
            seen.add(key)
            reason = _clean(str(entry.get("reason") or ""))[:_MAX_REASON].strip()
            picks.append(ThemePick(tmdb_id=key[1], media=media, origin="ai", reason=reason or None))
            titles[key] = str(hit.get("title") or hit.get("name") or title)
            break
    return picks, titles, len(entries)


def _resolve_tags(proposal: dict, tmdb: TmdbClient) -> tuple[int, ...]:
    ids: list[int] = []
    for phrase in _strings(proposal.get("tags"))[:_MAX_TAGS]:
        try:
            found = tmdb.search_keywords(phrase, limit=5)
        except Exception:
            logger.warning("theme author: TMDB keyword search failed")
            continue
        exact = [t for t in found if str(t.get("name", "")).lower() == phrase.strip().lower()]
        match = (exact or found or [None])[0]
        if match and int(match["id"]) not in ids:
            ids.append(int(match["id"]))
    return tuple(ids)
