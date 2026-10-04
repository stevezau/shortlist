"""Themes API (#138): author a theme from a brief with the AI, preview it, save and edit it. Owner-only.

A preview spends AI tokens and stores nothing; a save stores the theme and records what it cost. The hash
that says whether a theme's contents changed is always computed here from the contents, never read from the
request. Keys never leave the settings store: no response, event or error carries one.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from shortlist.engine.curator import make_curator
from shortlist.engine.models import MediaType, RowLimits
from shortlist.engine.themes import _MOVIE_GENRE_IDS, ThemeSpec, theme_content_hash
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.api.seasons import CollectionIO, TagIO, _off_loop, _plex
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, Theme
from shortlist.server.services import theme_store
from shortlist.server.services.context_builder import curator_kwargs
from shortlist.server.services.library_index import library_index
from shortlist.server.services.theme_author import (
    BUILD_SYSTEM_GUIDANCE,
    BUILD_SYSTEM_MECHANICS,
    ThemeAuthorError,
    author_theme,
    diff_themes,
)
from shortlist.server.services.theme_store import pick_titles, spec_from_row
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/themes", tags=["themes"], dependencies=[Depends(require_owner)])

_NO_AI = "Writing a theme needs an AI provider. Add one in Settings, then try again."
_NO_TMDB = "Add a TMDB API key in Settings first."
_PAUSED = "AI is paused for this row. Resume it from the row's menu to write or refine its theme."
_NO_PROVIDERS = ("", "none", "null")
_MAX_GUIDANCE = 4000
#: Running-time lookups a preview makes before it stops and leaves the rest to the nightly run.
_PREVIEW_MAX_DETAILS = 400
_STATS_KEYS = ("named", "resolved", "in_library", "after_rules", "ai_kept")


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


class ThemeOut(PassthroughModel):
    id: int | None
    slug: str
    name: str
    emoji: str | None
    brief: str
    origin: str
    media: list[str]
    tags: list[TagIO]
    genres: list[str]
    excluded_genres: list[str]
    collections: list[CollectionIO]
    picks: list[ThemePickIO]
    rules: RulesIO
    content_hash: str
    ai_tokens: int
    stats: dict[str, int]
    #: When the one extra AI call that extended this theme's list was made; null until then.
    topped_up_at: datetime | None = None


class PreviewIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    #: What the row is about. Required for a new theme; a refinement uses the stored theme's own and ignores this.
    brief: str = Field(default="", max_length=1000)
    #: What to change, for a refinement (``current_theme_id`` set). The stored brief is kept as it was.
    change: str = Field(default="", max_length=1000)
    media: Literal["movie", "show", "both"] = "both"
    #: A stored theme to refine by ``change``; omitted writes a new one from ``brief``.
    current_theme_id: int | None = None
    #: The AI row this is for: its libraries scope the counts, and a paused row is refused.
    collection_id: int | None = None
    #: The owner's wording for what makes a good theme; empty keeps Shortlist's. The locked mechanics stay.
    #: Roomy enough for the owner's own 2,000 characters on top of Shortlist's default wording.
    guidance: str = Field(default="", max_length=_MAX_GUIDANCE)


class ThemeStatsOut(PassthroughModel):
    named: int
    resolved: int
    in_library: int
    after_rules: int
    unwatched_median: int | None
    truncated: bool = False
    runtime_total: int = 0
    runtime_checked: int = 0
    ai_kept: int = 0


class ThemeDiffOut(PassthroughModel):
    rules_changed: bool
    added: list[str]
    removed: list[str]
    unchanged: list[str]
    added_count: int
    removed_count: int
    tags_added: list[str]
    tags_removed: list[str]
    genres_added: list[str]
    genres_removed: list[str]
    before_count: int
    after_count: int


class PreviewOut(PassthroughModel):
    draft: ThemeOut
    stats: ThemeStatsOut
    diff: ThemeDiffOut | None
    tokens: int


class CapabilitiesOut(PassthroughModel):
    ai: bool


class PromptsOut(PassthroughModel):
    guidance: str
    mechanics: str


@router.get("/capabilities", response_model=CapabilitiesOut)
async def capabilities(request: Request) -> dict:
    """Whether an AI provider is set, so the editor can hide the half that needs one."""
    state = request.app.state
    with state.sessions() as session:
        provider = _provider(SettingsStore(session, state.secrets))
    return {"ai": provider not in _NO_PROVIDERS}


@router.get("/prompts", response_model=PromptsOut)
async def prompts() -> dict:
    """The system prompt "Build the list" sends: the guidance an owner may replace, and the mechanics they may not."""
    return {"guidance": BUILD_SYSTEM_GUIDANCE.strip(), "mechanics": BUILD_SYSTEM_MECHANICS}


@router.post("/preview", response_model=PreviewOut)
async def preview_theme(body: PreviewIn, request: Request) -> dict:
    """Write or refine a theme from a brief with one AI call, without saving anything.

    409 while the row's AI is paused, 422 without an AI provider (neither makes a call).
    """
    state = request.app.state
    medias = (MediaType.MOVIE, MediaType.SHOW) if body.media == "both" else (MediaType(body.media),)
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        library_keys = _row_libraries(session, body.collection_id)
        if _provider(store) in _NO_PROVIDERS:
            raise HTTPException(status_code=422, detail=_NO_AI)
        try:
            curator = make_curator(_provider(store), **curator_kwargs(store.get))
        except Exception as e:
            # Class name only: an SDK's message can carry a fragment of the key.
            logger.warning("theme preview: could not set up the AI provider ({})", type(e).__name__)
            raise HTTPException(
                status_code=422, detail="The AI provider isn't set up properly. Check it in Settings."
            ) from None
        current, old_titles, known_tags, brief = None, {}, {}, body.brief
        if body.current_theme_id is not None:
            stored = _stored(session, body.current_theme_id)
            current, old_titles = spec_from_row(stored), pick_titles(stored)
            known_tags = {int(t["id"]): t["name"] for t in stored.tags}
            # The stored description stays the row's description: what the owner types here is the change.
            brief = stored.brief or ""
            if not body.change.strip():
                raise HTTPException(status_code=422, detail="Say what to change about the list.")
        elif not body.brief.strip():
            raise HTTPException(status_code=422, detail="Describe the row first.")
    tmdb = state.run_service.build_tmdb_only()
    if tmdb is None:
        raise HTTPException(status_code=503, detail=_NO_TMDB)

    def write() -> tuple:
        plex = _plex(state)
        index = library_index(plex, state.sessions, media=body.media, library_keys=library_keys)
        names = _TagNames(tmdb)
        try:
            draft = author_theme(
                brief=brief,
                change=body.change,
                media=medias,
                curator=curator,
                tmdb=names,
                plex=plex,
                library_index=index,
                current=current,
                guidance=body.guidance,
                current_tag_names=known_tags,
                max_details=_PREVIEW_MAX_DETAILS,
            )
        except ThemeAuthorError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        return draft, names.seen

    draft, seen = await _off_loop(write, "theme authoring")
    titles = {**old_titles, **draft.titles}
    tag_names = {**known_tags, **seen}
    diff = diff_themes(current, draft.spec, titles, tag_names) if current is not None else None
    return {
        "draft": _spec_view(draft.spec, brief=draft.brief, origin="ai", titles=titles, tag_names=tag_names),
        "stats": dataclasses.asdict(draft.stats),
        "diff": None if diff is None else dataclasses.asdict(diff),
        "tokens": draft.tokens,
    }


@router.post("", status_code=201, response_model=ThemeOut)
async def create_theme(body: ThemeSaveIn, request: Request) -> dict:
    """Save a theme. Its slug is made from its name now; its hash is worked out here."""
    state = request.app.state
    with state.sessions() as session:
        _refuse_unusable(body.draft)
        row = _save(session, state, body)
        session.commit()
        return _row_view(row)


@router.put("/{theme_id}", response_model=ThemeOut)
async def update_theme(theme_id: int, body: ThemeSaveIn, request: Request) -> dict:
    """Replace a theme's contents (a hand edit, or an AI refinement the owner kept), keeping its slug."""
    state = request.app.state
    with state.sessions() as session:
        row = _stored(session, theme_id)
        _refuse_unusable(body.draft)
        row = _save(session, state, body, existing=row)
        session.commit()
        return _row_view(row)


@router.get("/{theme_id}", response_model=ThemeOut)
async def get_theme(theme_id: int, request: Request) -> dict:
    with request.app.state.sessions() as session:
        return _row_view(_stored(session, theme_id))


class _TagNames:
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


def _provider(store: SettingsStore) -> str:
    return str(store.get("curator.provider") or "").strip().lower()


def _stored(session: Session, theme_id: int) -> Theme:
    row = session.get(Theme, theme_id)
    if row is None:
        raise HTTPException(status_code=404, detail="theme not found")
    return row


def _row_libraries(session: Session, collection_id: int | None) -> list[str]:
    """The libraries of the row a preview is for. 409 when that row's AI is paused; 404 when it is not a row."""
    if collection_id is None:
        return []
    collection = session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    # Pause blocks AI spend (preview); hand edits and 0-token saves stay legitimate (`_spend_on`).
    if collection.ai_paused:
        raise HTTPException(status_code=409, detail=_PAUSED)
    return [str(k) for k in collection.library_keys or []]


def _save(session: Session, state, body: ThemeSaveIn, *, existing: Theme | None = None) -> Theme:
    """`theme_store.save_theme`, with its refusals as the HTTP answers they have always been."""
    try:
        return theme_store.save_theme(session, state.secrets, body, existing=existing)
    except theme_store.RowPaused:
        raise HTTPException(status_code=409, detail=_PAUSED) from None
    except theme_store.TitleClash as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except LookupError:
        raise HTTPException(status_code=404, detail="collection not found") from None


def _refuse_unusable(draft: ThemeIn) -> None:
    """422 for a theme that would select nothing, or names a genre TMDB does not have."""
    if not (draft.tags or draft.genres or draft.collections or draft.picks):
        raise HTTPException(status_code=422, detail="Add at least one tag, genre, collection or title.")
    unknown = [g for g in (*draft.genres, *draft.excluded_genres) if g.strip().lower() not in _MOVIE_GENRE_IDS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"TMDB has no genre called “{unknown[0]}”.")
    low, high = draft.rules.min_year, draft.rules.max_year
    if low is not None and high is not None and low > high:
        raise HTTPException(status_code=422, detail="The earliest year can't be later than the latest year.")


def _rules_view(row_rules: dict) -> dict:
    return {k: row_rules.get(k) for k in ("max_runtime", "min_year", "max_year", "min_rating", "min_votes")}


def _row_view(row: Theme) -> dict:
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
        "rules": _rules_view(row.rules or {}),
        "content_hash": row.content_hash,
        "ai_tokens": row.ai_tokens or 0,
        "stats": dict(row.stats or {}),
        "topped_up_at": row.topped_up_at,
    }


def _spec_view(
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
