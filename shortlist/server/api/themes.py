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
from shortlist.engine.models import MediaType
from shortlist.server.api.seasons import off_loop, plex_reader
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, Theme
from shortlist.server.schema_base import PassthroughModel
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
from shortlist.server.services.theme_models import (
    CollectionIO,
    RulesIO,
    TagIO,
    TagNames,
    ThemeIn,
    ThemePickIO,
    ThemeSaveIn,
    refuse_unusable,
    row_view,
    spec_view,
)
from shortlist.server.services.theme_store import pick_titles, spec_from_row
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/themes", tags=["themes"], dependencies=[Depends(require_owner)])

_NO_AI = "Writing a theme needs an AI provider. Add one in Settings, then try again."
_NO_TMDB = "Add a TMDB API key in Settings first."
PAUSED = "AI is paused for this row. Resume it from the row's menu to write or refine its theme."
_NO_PROVIDERS = ("", "none", "null")
_MAX_GUIDANCE = 4000
#: Running-time lookups a preview makes before it stops and leaves the rest to the nightly run.
PREVIEW_MAX_DETAILS = 400
_STATS_KEYS = ("named", "resolved", "in_library", "after_rules", "ai_kept")


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
    #: An unsaved list to refine by ``change``, for a row that has not been saved yet. ``current_theme_id`` wins.
    current_draft: ThemeIn | None = None
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
        if body.current_theme_id is not None or body.current_draft is not None:
            if body.current_theme_id is not None:
                stored = _stored(session, body.current_theme_id)
            else:
                stored = _unsaved_row(session, body.current_draft)
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
        plex = plex_reader(state)
        index = library_index(plex, state.sessions, media=body.media, library_keys=library_keys)
        names = TagNames(tmdb)
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
                max_details=PREVIEW_MAX_DETAILS,
            )
        except ThemeAuthorError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        return draft, names.seen

    draft, seen = await off_loop(write, "theme authoring")
    titles = {**old_titles, **draft.titles}
    tag_names = {**known_tags, **seen}
    diff = diff_themes(current, draft.spec, titles, tag_names) if current is not None else None
    return {
        "draft": spec_view(draft.spec, brief=draft.brief, origin="ai", titles=titles, tag_names=tag_names),
        "stats": dataclasses.asdict(draft.stats),
        "diff": None if diff is None else dataclasses.asdict(diff),
        "tokens": draft.tokens,
    }


@router.post("", status_code=201, response_model=ThemeOut)
async def create_theme(body: ThemeSaveIn, request: Request) -> dict:
    """Save a theme. Its slug is made from its name now; its hash is worked out here."""
    state = request.app.state
    with state.sessions() as session:
        refuse_unusable(body.draft)
        row = _save(session, state, body)
        session.commit()
        return row_view(row)


@router.put("/{theme_id}", response_model=ThemeOut)
async def update_theme(theme_id: int, body: ThemeSaveIn, request: Request) -> dict:
    """Replace a theme's contents (a hand edit, or an AI refinement the owner kept), keeping its slug."""
    state = request.app.state
    with state.sessions() as session:
        row = _stored(session, theme_id)
        refuse_unusable(body.draft)
        row = _save(session, state, body, existing=row)
        session.commit()
        return row_view(row)


@router.get("/{theme_id}", response_model=ThemeOut)
async def get_theme(theme_id: int, request: Request) -> dict:
    with request.app.state.sessions() as session:
        return row_view(_stored(session, theme_id))


def _provider(store: SettingsStore) -> str:
    return str(store.get("curator.provider") or "").strip().lower()


def _stored(session: Session, theme_id: int) -> Theme:
    row = session.get(Theme, theme_id)
    if row is None:
        raise HTTPException(status_code=404, detail="theme not found")
    return row


def _unsaved_row(session: Session, draft: ThemeIn) -> Theme:
    """A draft as the stored row it would become, never added to the session, so it is read like a saved theme."""
    row = Theme(slug=theme_store.unique_slug(session, draft.name))
    theme_store.write_theme(row, ThemeSaveIn(draft=draft))
    return row


def _row_libraries(session: Session, collection_id: int | None) -> list[str]:
    """The libraries of the row a preview is for. 409 when that row's AI is paused; 404 when it is not a row."""
    if collection_id is None:
        return []
    collection = session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    # Pause blocks AI spend (preview); hand edits and 0-token saves stay legitimate.
    if collection.ai_paused:
        raise HTTPException(status_code=409, detail=PAUSED)
    return [str(k) for k in collection.library_keys or []]


def _save(session: Session, state, body: ThemeSaveIn, *, existing: Theme | None = None) -> Theme:
    """`theme_store.save_theme`, with its refusals as the HTTP answers they have always been."""
    try:
        return theme_store.save_theme(session, state.secrets, body, existing=existing)
    except theme_store.RowPaused:
        raise HTTPException(status_code=409, detail=PAUSED) from None
    except theme_store.TitleClash as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except LookupError:
        raise HTTPException(status_code=404, detail="collection not found") from None
