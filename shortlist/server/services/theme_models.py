"""The theme shapes services build and the themes API serves: what a save carries, how a stored or unsaved
theme reads back, and the refusals both share.

Below both the HTTP and assistant transports, so neither imports the other.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from shortlist.engine.models import MediaType, RowLimits
from shortlist.engine.themes import GENRE_IDS_BY_NAME, ThemeSpec, theme_content_hash
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.db.models import Theme


class TagIO(PassthroughModel):
    """A TMDB tag (keyword), with its name so the editor can show it without asking TMDB."""

    id: int
    name: str


class CollectionIO(PassthroughModel):
    """A Plex collection, by library and title — never ratingKey: Kometa recreates its seasonal ones each year."""

    section_key: str
    section_title: str
    title: str


class RulesIO(PassthroughModel):
    """A theme's hard limits. A missing or null one is no limit."""

    max_runtime: int | None = Field(default=None, ge=1)
    min_year: int | None = Field(default=None, ge=1850, le=2200)
    max_year: int | None = Field(default=None, ge=1850, le=2200)
    min_rating: float | None = Field(default=None, ge=0, le=10)
    min_votes: int | None = Field(default=None, ge=0)


class ThemePickIO(PassthroughModel):
    """A title a theme names by TMDB id; ``origin`` says whether the AI or the owner chose it."""

    tmdb_id: int
    media: Literal["movie", "show"]
    origin: Literal["ai", "owner"] = "owner"
    reason: str | None = Field(default=None, max_length=160)
    title: str = ""
    year: int | None = None


class ThemeIn(BaseModel):
    """A theme to store. Undeclared fields (a hash echoed back from a preview) are ignored, never stored."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    emoji: str | None = Field(default=None, max_length=8)
    brief: str = Field(default="", max_length=1000)
    origin: Literal["ai", "manual"] = "manual"
    media: list[Literal["movie", "show"]] = Field(min_length=1, max_length=2)
    tags: list[TagIO] = Field(default_factory=list, max_length=20)
    genres: list[str] = Field(default_factory=list, max_length=10)
    excluded_genres: list[str] = Field(default_factory=list, max_length=10)
    collections: list[CollectionIO] = Field(default_factory=list, max_length=10)
    picks: list[ThemePickIO] = Field(default_factory=list, max_length=200)
    rules: RulesIO = Field(default_factory=RulesIO)


class ThemeSaveIn(BaseModel):
    """A save: the theme, what the AI call that wrote it cost, and the row it was written for."""

    draft: ThemeIn
    #: Tokens the authoring call spent (0 for a hand edit). Added to the theme's and the row's running totals.
    tokens: int = Field(default=0, ge=0)
    collection_id: int | None = None
    #: The preview's counts, kept to show beside the theme.
    stats: dict[str, int] = Field(default_factory=dict)


class TagNames:
    """A TMDB client that remembers the tag names it was asked about, so a draft's tags can be shown by name."""

    def __init__(self, tmdb) -> None:
        self._tmdb = tmdb
        self.seen: dict[int, str] = {}

    def search_keywords(self, query: str, limit: int = 10) -> list[dict]:
        found = self._tmdb.search_keywords(query, limit=limit)
        self.seen.update({int(t["id"]): str(t["name"]) for t in found})
        return found

    def __getattr__(self, name: str):
        return getattr(self._tmdb, name)


def refuse_unusable(draft: ThemeIn) -> None:
    """422 for a theme that would select nothing, or names a genre TMDB does not have."""
    if not (draft.tags or draft.genres or draft.collections or draft.picks):
        raise HTTPException(status_code=422, detail="Add at least one tag, genre, collection or title.")
    unknown = [g for g in (*draft.genres, *draft.excluded_genres) if g.strip().lower() not in GENRE_IDS_BY_NAME]
    if unknown:
        raise HTTPException(status_code=422, detail=f"TMDB has no genre called “{unknown[0]}”.")
    low, high = draft.rules.min_year, draft.rules.max_year
    if low is not None and high is not None and low > high:
        raise HTTPException(status_code=422, detail="The earliest year can't be later than the latest year.")


def rules_view(row_rules: dict) -> dict:
    return {k: row_rules.get(k) for k in ("max_runtime", "min_year", "max_year", "min_rating", "min_votes")}


def row_view(row: Theme) -> dict:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "emoji": row.emoji,
        "brief": row.brief,
        "origin": row.origin,
        "media": list(row.media or []),
        "tags": list(row.tags or []),
        "genres": list(row.genres or []),
        "excluded_genres": list(row.excluded_genres or []),
        "collections": list(row.collections or []),
        "picks": list(row.picks or []),
        "rules": rules_view(row.rules or {}),
        "content_hash": row.content_hash,
        "ai_tokens": row.ai_tokens or 0,
        "stats": dict(row.stats or {}),
        "topped_up_at": row.topped_up_at,
    }


def spec_view(
    spec: ThemeSpec,
    *,
    brief: str,
    origin: str,
    titles: dict[tuple[MediaType, int], str],
    tag_names: dict[int, str],
) -> dict:
    """An unsaved theme as `ThemeOut`: no id, no tokens, and the hash its contents would get."""
    rules: RowLimits = spec.rules
    return {
        "id": None,
        "slug": spec.slug,
        "name": spec.name,
        "emoji": spec.emoji,
        "brief": brief,
        "origin": origin,
        "media": [m.value for m in spec.media],
        "tags": [{"id": t, "name": tag_names.get(t, str(t))} for t in spec.tags],
        "genres": list(spec.genres),
        "excluded_genres": list(spec.excluded_genres),
        "collections": [
            {"section_key": c.section_key, "section_title": "", "title": c.title} for c in spec.collections
        ],
        "picks": [
            {
                "tmdb_id": p.tmdb_id,
                "media": p.media.value,
                "origin": p.origin,
                "reason": p.reason,
                "title": titles.get((p.media, p.tmdb_id), ""),
                "year": None,
            }
            for p in spec.picks
        ],
        "rules": {
            "max_runtime": rules.max_runtime,
            "min_year": rules.min_year,
            "max_year": rules.max_year,
            "min_rating": rules.min_rating,
            "min_votes": spec.min_votes,
        },
        "content_hash": theme_content_hash(spec),
        "ai_tokens": 0,
        "stats": {},
    }
