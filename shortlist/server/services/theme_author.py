"""Author a theme (#138) from a brief with ONE AI call, then check it against TMDB.

The model proposes a name, rules, tags, genres and titles. Nothing it says is trusted: titles resolve
through TMDB search (a made-up one resolves to nothing), tags through TMDB's keyword search, genres through
the engine's genre list, and the rules are applied by ``load_theme`` from TMDB's own data.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.curator.base import Curator, taste_summary
from shortlist.engine.models import MediaType, RowLimits, UserProfile, slugify
from shortlist.engine.seasons import CollectionReader
from shortlist.engine.themes import GENRE_IDS_BY_NAME, ThemePick, ThemeSpec, load_theme

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
    '"drop_tags": [str], "drop_genres": [str], '
    '"titles": [{"title": str, "year": int, "media": "movie" or "show", "reason": str}]}. '
    "Give about 60 titles. Real, released titles only, spelled exactly as released, each with its release "
    'year and whether it is a "movie" or a "show"; never invent a title. Each reason is at most 12 words. '
    "Set a rule only when the brief asks for that limit, otherwise null. min_rating is on TMDB's 0 to 10 "
    "scale (7.5 is a good film, never 75). max_runtime is in minutes. tags are short TMDB keyword phrases; "
    "genres are TMDB genre names. drop_tags and drop_genres are for changing an existing theme only: list "
    "the names from the current theme that no longer fit, otherwise leave them empty."
)

# Room for ~60 titles with reasons; the provider default (2048 on Anthropic) cuts the list off mid-object.
_REPLY_TOKENS = 8000
_UNREADABLE = "The AI's answer was not a theme I could read. Try again, or reword the brief."
_CUT_OFF = "The AI's answer was cut off or unreadable. Try again, or pick a provider that gives longer replies."
_MAX_REASON = 160
_MAX_TITLES = 60
_MAX_TAGS = 10
_MAX_GENRES = 10
_MAX_NAME = 60
_MAX_PHRASE = 120
_MAX_BRIEF = 1000
_MAX_TOTAL_TAGS = 20
_MAX_RUNTIME = 600
_MIN_YEAR, _MAX_YEAR = 1850, 2200
_MEDIA_NAMES = {"movie": MediaType.MOVIE, "show": MediaType.SHOW}
_MARKUP = re.compile(r"[*_`#\[\]{}<>|~\\]")


class ThemeAuthorError(Exception):
    """The theme could not be written; ``str(error)`` is plain English for the owner.

    ``answered`` is True when the provider sent a reply back (so a call was spent) and ``tokens`` is what that
    reply cost. A failure before any reply leaves both at their defaults.
    """

    tokens: int = 0
    answered: bool = False


@dataclass(frozen=True)
class ThemeStats:
    """How the AI's proposal fared: titles named, found on TMDB, held by the libraries, left after rules."""

    named: int
    resolved: int
    in_library: int
    after_rules: int
    unwatched_median: int | None
    #: The AI's reply was cut off at the token cap and only the titles before the cut were kept.
    truncated: bool = False
    #: Titles that needed a running-time check, and how many got one. Fewer checked than total means a
    #: preview bounded its lookups; the nightly run checks the rest.
    runtime_total: int = 0
    runtime_checked: int = 0
    #: How many of the AI's own titles are in the row after the rules: on the server and not limited out.
    #: ``in_library`` also counts every tag and genre match, so it says nothing about what the AI found.
    ai_kept: int = 0


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
    unchanged: list[str]
    added_count: int
    removed_count: int
    tags_added: list[str] = field(default_factory=list)
    tags_removed: list[str] = field(default_factory=list)
    genres_added: list[str] = field(default_factory=list)
    genres_removed: list[str] = field(default_factory=list)
    #: How many titles the theme names before and after the change.
    before_count: int = 0
    after_count: int = 0


def author_theme(
    *,
    brief: str,
    media: MediaType | tuple[MediaType, ...],
    curator: Curator,
    tmdb: TmdbClient,
    plex: CollectionReader,
    library_index: dict[MediaType, dict[int, int]],
    profile: UserProfile | None = None,
    current: ThemeSpec | None = None,
    guidance: str = "",
    current_tag_names: Mapping[int, str] | None = None,
    change: str = "",
    max_details: int | None = None,
) -> ThemeDraft:
    """Write a theme from ``brief``, or refine ``current`` by ``change``.

    A refinement keeps ``brief`` (the owner's original description) as context and sends ``change`` as the
    extra instruction, so the stored description is never overwritten by what the owner asked to change.
    The tags and genres ``current`` has, hand-added ones included, carry over; only names the AI lists as
    ``drop_tags`` / ``drop_genres`` leave.

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
    change = change.strip()[:_MAX_BRIEF] if current is not None else ""
    tag_names = dict(current_tag_names or {})
    user = _user_message(brief, change, medias, profile, current, tag_names, tmdb)
    try:
        raw = curator.complete(system, user, max_tokens=_REPLY_TOKENS)
    except Exception as exc:
        # Class name only: an SDK's message can carry a fragment of the key.
        logger.warning("theme author: AI call failed ({})", type(exc).__name__)
        raise ThemeAuthorError(
            "The AI provider didn't respond. Check the provider in Settings and try again."
        ) from None
    tokens = int(getattr(curator, "last_tokens", 0) or 0)
    if not (raw or "").strip():
        empty = ThemeAuthorError("The AI did not answer. Try again in a moment.")
        empty.tokens, empty.answered = tokens, tokens > 0
        raise empty
    try:
        proposal, truncated = _parse(raw)
    except ThemeAuthorError as unreadable:
        unreadable.tokens, unreadable.answered = tokens, True
        raise

    picks, titles, named, asked_kinds = _resolve_titles(proposal, medias, tmdb)
    tags = _resolve_tags(proposal, tmdb)
    genres = tuple(g for g in _strings(proposal.get("genres")) if g.strip().lower() in GENRE_IDS_BY_NAME)[:_MAX_GENRES]
    if current is not None:
        tags, genres = _carry_over(proposal, current, tags, genres, tag_names)
    name = _clean(str(proposal.get("name") or ""))[:_MAX_NAME].strip() or _clean(brief)[:40] or "Themed row"
    kept = [p for p in current.picks if p.origin != "ai" and (p.media, p.tmdb_id) not in titles] if current else []
    kept_collections = current.collections if current else ()
    if not (picks or tags or genres or kept or kept_collections):
        nothing = ThemeAuthorError(
            "The AI didn't suggest anything Shortlist could find. Try describing the row differently."
        )
        nothing.tokens, nothing.answered = tokens, True
        raise nothing
    # A kind the AI named nothing for stays out: tag and genre matches alone would fill that library with filler.
    # A kind it NAMED stays in even when matching rejected or failed every title: only a kind the AI truly left
    # out is dropped, and a library holding none of the theme's own titles is skipped later instead.
    named_kinds = {p.media for p in picks} | {p.media for p in kept} | asked_kinds
    covered = tuple(m for m in medias if m in named_kinds) or medias
    spec = ThemeSpec(
        slug=current.slug if current else (slugify(name) or "theme"),
        name=current.name if current else name,
        emoji=_emoji(proposal.get("emoji")) or (current.emoji if current else None),
        media=covered,
        tags=tags,
        genres=genres,
        excluded_genres=current.excluded_genres if current else (),
        collections=kept_collections,
        picks=tuple(picks) + tuple(kept),
        rules=_rules(proposal.get("rules")),
        min_votes=current.min_votes if current else None,
    )
    loaded = load_theme(tmdb, plex, spec, library_index, max_details=max_details)
    after_rules = sum(len(found) for found in loaded.titles.ids.values())
    held = loaded.held
    # Rules come from TMDB, so a pick the rules drop must not be offered as the AI's reason for a row.
    kept_reasons = {(p.media, p.tmdb_id): p.reason for p in picks if (p.media, p.tmdb_id) in loaded.reasons}
    ai_kept = sum(1 for p in picks if p.tmdb_id in loaded.titles.ids[p.media])
    stats = ThemeStats(
        named=named,
        resolved=len(picks),
        in_library=held,
        after_rules=after_rules,
        unwatched_median=None,
        truncated=truncated,
        runtime_total=loaded.runtime_total,
        runtime_checked=loaded.runtime_checked,
        ai_kept=ai_kept,
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


def diff_themes(
    old: ThemeSpec,
    new: ThemeSpec,
    titles: dict[tuple[MediaType, int], str] | None = None,
    tag_names: Mapping[int, str] | None = None,
) -> ThemeDiff:
    """What a refinement changed: whether the rules moved, and which picks, tags and genres came or went.

    ``titles`` names picks by (media, id) and ``tag_names`` tags by id; one it does not know is listed by its id.
    """
    names = titles or {}
    tag_label = tag_names or {}
    before = {(p.media, p.tmdb_id) for p in old.picks}
    after = {(p.media, p.tmdb_id) for p in new.picks}

    def label(key: tuple[MediaType, int]) -> str:
        return names.get(key) or str(key[1])

    def tag(tag_id: int) -> str:
        return tag_label.get(tag_id) or str(tag_id)

    added = sorted(label(k) for k in after - before)
    removed = sorted(label(k) for k in before - after)
    unchanged = sorted(label(k) for k in before & after)
    genres_before = {g.strip().lower(): g for g in old.genres}
    genres_after = {g.strip().lower(): g for g in new.genres}
    return ThemeDiff(
        rules_changed=old.rules.fingerprint() != new.rules.fingerprint(),
        added=added,
        removed=removed,
        unchanged=unchanged,
        added_count=len(added),
        removed_count=len(removed),
        tags_added=sorted(tag(t) for t in set(new.tags) - set(old.tags)),
        tags_removed=sorted(tag(t) for t in set(old.tags) - set(new.tags)),
        genres_added=sorted(genres_after[g] for g in genres_after.keys() - genres_before.keys()),
        genres_removed=sorted(genres_before[g] for g in genres_before.keys() - genres_after.keys()),
        before_count=len(before),
        after_count=len(after),
    )


def _carry_over(
    proposal: dict,
    current: ThemeSpec,
    tags: tuple[int, ...],
    genres: tuple[str, ...],
    tag_names: Mapping[int, str],
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """A refinement's tags and genres: what the theme has, plus the AI's, minus what the AI says to drop.

    The AI is only asked for what to ADD, so a tag or genre it did not repeat — or one the owner added by
    hand — would otherwise vanish on Keep, silently changing which titles the row can draw on.
    """
    dropped_tags = {p.lower() for p in _strings(proposal.get("drop_tags"))[:_MAX_TAGS]}
    dropped_genres = {p.lower() for p in _strings(proposal.get("drop_genres"))[:_MAX_GENRES]}
    gone_tags = {tag_id for tag_id, name in tag_names.items() if name.strip().lower() in dropped_tags}
    added_tags = [t for t in dict.fromkeys(tags) if t not in gone_tags]
    carried_tags = [t for t in dict.fromkeys(current.tags) if t not in gone_tags and t not in added_tags]
    added_genres = {g.strip().lower(): g for g in genres if g.strip().lower() not in dropped_genres}
    carried_genres = {
        g.strip().lower(): g
        for g in current.genres
        if g.strip().lower() not in dropped_genres and g.strip().lower() not in added_genres
    }
    return (
        _cap_carried_first(carried_tags, added_tags, _MAX_TOTAL_TAGS),
        _cap_carried_first(list(carried_genres.values()), list(added_genres.values()), _MAX_GENRES),
    )


def _cap_carried_first[T](carried: list[T], added: list[T], cap: int) -> tuple[T, ...]:
    """Carried-over items then the AI's additions, trimmed from the carried items so no addition is lost."""
    added = added[:cap]
    return (*carried[: cap - len(added)], *added)


def _user_message(
    brief: str,
    change: str,
    medias: tuple[MediaType, ...],
    profile: UserProfile | None,
    current: ThemeSpec | None,
    current_tag_names: Mapping[int, str],
    tmdb: TmdbClient,
) -> str:
    parts = []
    if brief:
        parts.append(f"Brief: <brief>{brief}</brief>")
    if current is not None and change:
        parts.append(f"Change to make: <change>{change}</change>")
    parts.append("Media: " + " and ".join("movies" if m is MediaType.MOVIE else "shows" for m in medias))
    if current is not None:
        parts.append(
            "Current theme (change it as asked, keeping what still fits):\n"
            + _describe(current, current_tag_names, tmdb)
        )
    if profile is not None and profile.history:
        parts.append("Tailor it to this person's taste.\n" + taste_summary(profile, 20))
    return "\n\n".join(parts)


def _describe(spec: ThemeSpec, tag_names: Mapping[int, str], tmdb: TmdbClient) -> str:
    titles = []
    for pick in spec.picks:
        item = tmdb.list_item(pick.tmdb_id, pick.media)
        if item:
            titles.append(str(item.get("title") or item.get("name") or ""))
    rules = {k: v for k, v in vars(spec.rules).items() if v is not None}
    described: dict[str, object] = {"name": spec.name, "genres": list(spec.genres), "rules": rules, "titles": titles}
    tags = [tag_names[t] for t in spec.tags if t in tag_names]
    if tags:
        described["tags"] = tags
    return json.dumps(described)


def _parse(raw: str) -> tuple[dict, bool]:
    """The JSON object in ``raw`` and whether it had to be salvaged from a cut-off reply.

    Tolerates a ```json fence and prose around it. A reply that stops mid-way through the titles array is
    cut back to its last complete title (see ``_salvage_truncated``); anything else unreadable raises.
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        raise ThemeAuthorError(_UNREADABLE)
    try:
        data = json.loads(text[start : end + 1]) if end > start else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data, False
    if data is None:
        salvaged = _salvage_truncated(text[start:])
        if salvaged is not None:
            return salvaged, True
        raise ThemeAuthorError(_CUT_OFF)
    raise ThemeAuthorError(_UNREADABLE)


def _salvage_truncated(text: str) -> dict | None:
    """``text`` (a JSON object that stops mid-way) cut back to its last complete title, or None.

    Conservative: only when the cut falls inside the ``titles`` array, everything before that array is
    complete, and the name and at least one whole title survive. Quotes are tracked so a brace inside a
    reason is not mistaken for the end of a title.
    """
    match = re.search(r'"titles"\s*:\s*\[', text)
    if match is None or not _balanced_to_depth_one(text[: match.start()]):
        return None
    last_complete = None
    depth = 0
    in_string = escaped = False
    for i in range(match.end(), len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            if depth == 0:
                return None  # the array closed itself: this is not a cut inside it
            depth -= 1
            if depth == 0 and ch == "}":
                last_complete = i
    if last_complete is None:
        return None
    try:
        data = json.loads(text[: last_complete + 1] + "]}")
    except ValueError:
        return None
    titles = data.get("titles") if isinstance(data, dict) else None
    if not (isinstance(titles, list) and titles and isinstance(data.get("name"), str) and data["name"].strip()):
        return None
    return data


def _balanced_to_depth_one(prefix: str) -> bool:
    """Whether ``prefix`` is inside the top-level object only, with no string left open."""
    depth = 0
    in_string = escaped = False
    for ch in prefix:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return depth == 1 and not in_string


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
    # json.loads accepts Infinity and NaN, and int(float("inf")) raises OverflowError.
    if not math.isfinite(value):
        return None
    try:
        return kind(value)
    except (OverflowError, ValueError):
        return None


def _bounded(value: int | float | None, low: float, high: float) -> int | float | None:
    """``value``, or None when it is outside what the editor and the API accept."""
    return value if value is not None and low <= value <= high else None


def _rules(value: object) -> RowLimits:
    """The AI's limits, with every out-of-range answer dropped rather than stored.

    A model can write ``min_rating: 75`` for TMDB's 0-10 scale, or years the wrong way round. None of it is
    trusted: the API's own bounds refuse such a theme on save and would fail the preview that returned it.
    """
    raw = value if isinstance(value, dict) else {}
    runtime = _number(raw.get("max_runtime"), int)
    min_year = _bounded(_number(raw.get("min_year"), int), _MIN_YEAR, _MAX_YEAR)
    max_year = _bounded(_number(raw.get("max_year"), int), _MIN_YEAR, _MAX_YEAR)
    if min_year is not None and max_year is not None and min_year > max_year:
        min_year = max_year = None
    return RowLimits(
        max_runtime=None if runtime is None or runtime <= 0 else min(runtime, _MAX_RUNTIME),
        min_year=min_year,
        max_year=max_year,
        min_rating=_bounded(_number(raw.get("min_rating"), float), 0, 10),
    )


def _resolve_titles(
    proposal: dict, medias: tuple[MediaType, ...], tmdb: TmdbClient
) -> tuple[list[ThemePick], dict[tuple[MediaType, int], str], int, set[MediaType]]:
    """Resolve the AI's titles. The last item is every kind the AI named a title for, found or not."""
    raw = proposal.get("titles")
    entries = [
        e for e in (raw if isinstance(raw, list) else []) if isinstance(e, dict) and isinstance(e.get("title"), str)
    ]
    entries = [e for e in entries if e["title"].strip()][:_MAX_TITLES]
    picks: list[ThemePick] = []
    titles: dict[tuple[MediaType, int], str] = {}
    seen: set[tuple[MediaType, int]] = set()
    asked_kinds: set[MediaType] = set()
    failed_kinds: set[MediaType] = set()
    searches = 0
    for entry in entries:
        # The AI says which kind each title is: searching both would resolve "Severance" to the 2006 film.
        # An entry that doesn't say, or says something else, is left unresolved rather than guessed.
        media = _MEDIA_NAMES.get(str(entry.get("media") or "").strip().lower())
        if media is None or media not in medias or searches >= _MAX_TITLES:
            continue
        searches += 1
        asked_kinds.add(media)
        year = _number(entry.get("year"), int)
        title = entry["title"].strip()[:_MAX_PHRASE]
        try:
            hit = _best_match(tmdb.search_all(title, media), title, year)
            key = (media, int(hit["id"])) if hit else None
        except Exception:
            logger.warning("theme author: TMDB search failed for a title")
            failed_kinds.add(media)
            continue
        if key is None or key in seen:
            continue
        seen.add(key)
        reason = _clean(str(entry.get("reason") or ""))[:_MAX_REASON].strip()
        picks.append(ThemePick(tmdb_id=key[1], media=media, origin="ai", reason=reason or None))
        titles[key] = str(hit.get("title") or hit.get("name") or title)
    return picks, titles, len(entries), asked_kinds | failed_kinds


_ARTICLES = frozenset({"the", "a", "an"})
_NUMBER_WORDS = {
    word: str(number)
    for number, word in enumerate(
        (
            *("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"),
            *("eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen"),
            *("nineteen", "twenty"),
        )
    )
}
_SUBTITLE = re.compile(r"\s*:\s*|\s+-\s+")


def _fold(title: str, *, numbers: bool = True) -> str:
    """A title as comparable words: no accents, case, punctuation or articles; "&" is "and"; numbers are digits
    (unless ``numbers`` is off)."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", title) if not unicodedata.combining(c))
    plain = re.sub(r"['\u2019]", "", plain.casefold().replace("&", " and "))
    words = re.sub(r"[^\w\s]", " ", plain).split()
    return " ".join((_NUMBER_WORDS.get(w, w) if numbers else w) for w in words if w not in _ARTICLES)


def _main_title(title: str) -> str:
    return _fold(_SUBTITLE.split(title, maxsplit=1)[0])


def _title_closeness(asked: str, name: str, *, original: bool = False) -> int:
    """How well one title name matches the asked one: 3 the same (``exact``), then looser: 2 the same once number
    words are digits, or one contains the other with a small length gap, 1 the same before a subtitle
    ("Rogue One" / "Rogue One: A Star Wars Story"). 0 is not the same. An original-language name is never exact."""
    want, got = _fold(asked), _fold(name)
    if not want or not got:
        return 0
    if _fold(asked, numbers=False) == _fold(name, numbers=False):
        return 2 if original else 3
    if want == got:
        return 2
    shorter, longer = sorted((want, got), key=len)
    if shorter in longer and (len(longer) - len(shorter)) <= 0.3 * len(longer):
        return 2
    main = _main_title(asked)
    return 1 if main and main == _main_title(name) else 0


def _match_rank(asked: str, year: int | None, hit: dict) -> tuple[int, int] | None:
    """How well a TMDB hit is the title the AI named, or None when it is not that title.

    ``TmdbClient.search`` ranks by year but never filters, so its top result can be anything ("The Italian Job"
    resolved to a docuseries). An exact title match needs no year, but when both sides have one they may differ
    by 1. Any looser match (a subtitle, containment, number words, an original-language name) is only believed
    when both sides HAVE a year and they agree within 1. Better is lower, for ``min``.
    """
    closeness = max(
        _title_closeness(asked, str(hit.get(field) or ""), original=field.startswith("original"))
        for field in ("title", "name", "original_title", "original_name")
    )
    if closeness == 0:
        return None
    date = str(hit.get("release_date") or hit.get("first_air_date") or "")
    hit_year = int(date[:4]) if date[:4].isdigit() else None
    if year is None or hit_year is None:
        return (-closeness, 0) if closeness == 3 else None
    gap = abs(hit_year - year)
    return (-closeness, gap) if gap <= 1 else None


def _best_match(results: list[dict], asked: str, year: int | None) -> dict | None:
    ranked = [(rank, i, hit) for i, hit in enumerate(results) if (rank := _match_rank(asked, year, hit)) is not None]
    return min(ranked, key=lambda r: (r[0], r[1]))[2] if ranked else None


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
