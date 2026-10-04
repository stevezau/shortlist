"""Explore rows (#138): give each person a new theme on a schedule, built a day ahead.

A daily job walks every enabled explore row and each person in its audience. Per (row, person) it does at most
one of: promote the "up next" theme, author the next theme, author the current theme (none yet), or nothing.
It writes the database only. The row's title and picks change in the row's own run, which reads whichever theme
the DB holds as `current` for that person, so the job's clock is never load-bearing.

A rotation never empties a row: any failure leaves the person's current theme alone, logs one event, and the
next pass tries again. Every target is its own transaction, so a failure rolls back only that target.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, UserProfile
from shortlist.engine.web_guidance import AiInstructions
from shortlist.server.api.themes import ThemeIn, ThemeSaveIn, _refuse_unusable, _spec_view, _TagNames
from shortlist.server.db.models import (
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    Event,
    Theme,
    ThemeHistory,
    User,
)
from shortlist.server.services.audit import add_audit
from shortlist.server.services.library_index import library_index
from shortlist.server.services.theme_author import BUILD_SYSTEM_GUIDANCE, ThemeAuthorError, ThemeDraft, author_theme
from shortlist.server.services.theme_store import (
    RowPaused,
    ThemeStoreError,
    TitleClash,
    add_row_tokens,
    reject_person_title_clash,
    save_theme,
    spec_from_row,
)

#: The next theme is built this many days before it starts, so the owner can look at it and change it.
NEXT_LEAD_DAYS = 1
DEFAULT_THEME_DAYS = 7
_RECENT_NAMES = 6
_CLASH_NAMES = 3
_CLASH_EVENTS_READ = 50
_DEFAULT_BRIEF = "Choose a theme this person would love from what they watch."

Action = Literal["authored_current", "authored_next", "promoted", "kept", "skipped_paused", "failed"]


@dataclass(frozen=True)
class RotationOutcome:
    collection_id: int
    user_id: int
    action: Action


_TARGET_LOCKS: dict[tuple[int, int], threading.Lock] = {}
_TARGET_LOCKS_GUARD = threading.Lock()


def _target_lock(collection_id: int, user_id: int) -> threading.Lock:
    """One lock per (row, person). The kind reads only, so the job queue may run two passes at once; both would
    see "no current theme", both would call the AI, and both would insert one. The second now waits, then reads
    what the first committed and finds nothing to do."""
    with _TARGET_LOCKS_GUARD:
        return _TARGET_LOCKS.setdefault((collection_id, user_id), threading.Lock())


def rotate_themes(
    sessions: Callable[[], Session],
    *,
    now: datetime,
    secrets=None,
    unavailable: str = "",
    author=author_theme,
    curator=None,
    tmdb=None,
    plex=None,
    tools: Callable[[], AuthoringTools] | None = None,
    profile_for: Callable[[Session, int], UserProfile],
) -> list[RotationOutcome]:
    """Run one pass over every enabled explore row and each person in its audience.

    ``tools`` builds the AI provider, TMDB client and Plex reader the first time a person actually needs a theme
    written (it overrides ``curator``/``tmdb``/``plex``/``unavailable``). A pass that only promotes, or finds
    nothing to do, never connects to anything.
    """
    moment = _naive_utc(now)
    targets = rotation_targets(sessions)
    tools = _once(tools) if tools is not None else None
    outcomes: list[RotationOutcome] = []
    for collection_id, user_id in targets:
        spent: list[int] = []
        try:
            with _target_lock(collection_id, user_id), sessions() as session:
                action = _rotate_one(
                    session,
                    collection_id,
                    user_id,
                    moment,
                    sessions=sessions,
                    secrets=secrets,
                    unavailable=unavailable,
                    author=author,
                    curator=curator,
                    tmdb=tmdb,
                    plex=plex,
                    tools=tools,
                    profile_for=profile_for,
                    spent=spent,
                )
                session.commit()
        except Exception as e:
            action = "failed"
            _log_failure(sessions, collection_id, user_id, e, tokens=sum(spent))
        outcomes.append(RotationOutcome(collection_id, user_id, action))
    return outcomes


def _once[T](build: Callable[[], T]) -> Callable[[], T]:
    """``build`` called at most once, its failure remembered too: a Plex that is down is tried once a pass, not
    once a person."""
    box: list = []

    def get() -> T:
        if not box:
            try:
                box.append((build(), None))
            except Exception as e:
                box.append((None, e))
        value, error = box[0]
        if error is not None:
            raise error
        return value

    return get


@dataclass(frozen=True)
class AuthoringTools:
    """What authoring a theme needs. ``unavailable`` says why it cannot happen, with the HTTP status a route uses."""

    curator: object | None = None
    tmdb: object | None = None
    plex: object | None = None
    unavailable: str = ""
    status: int = 503


def authoring_tools(state) -> AuthoringTools:
    """The AI provider, TMDB client and Plex reader the way the themes API builds them. Connects to Plex."""
    from shortlist.engine.curator import make_curator
    from shortlist.server.services.context_builder import curator_kwargs
    from shortlist.server.settings_store import SettingsStore

    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        provider = str(store.get("curator.provider") or "").strip().lower()
        if provider in ("", "none", "null"):
            return AuthoringTools(
                unavailable="Writing a theme needs an AI provider. Add one in Settings, then try again.", status=422
            )
        try:
            curator = make_curator(provider, **curator_kwargs(store.get))
        except Exception as e:
            # Class name only: an SDK's message can carry a fragment of the key.
            logger.warning("theme authoring: could not set up the AI provider ({})", type(e).__name__)
            return AuthoringTools(
                unavailable="The AI provider isn't set up properly. Check it in Settings.", status=422
            )
    tmdb = state.run_service.build_tmdb_only()
    if tmdb is None:
        return AuthoringTools(unavailable="Add a TMDB API key in Settings first.")
    plex = state.run_service.build_plex_reader()
    if plex is None:
        return AuthoringTools(unavailable="Plex isn't connected yet.")
    return AuthoringTools(curator=curator, tmdb=tmdb, plex=plex)


def audience_users(session: Session, collection: Collection) -> list[User]:
    """The people a per-person row builds for: enabled, still on the server, and in the subset if it has one."""
    query = select(User).where(User.enabled.is_(True), User.departed_at.is_(None), User.removed_at.is_(None))
    if collection.audience == "subset":
        query = query.where(
            User.id.in_(select(CollectionAudience.user_id).where(CollectionAudience.collection_id == collection.id))
        )
    return list(session.scalars(query.order_by(User.id)))


def rotating_users(session: Session, collection: Collection) -> list[User]:
    """The audience, minus anyone who muted the row: it builds nothing for them, so no theme is written."""
    muted = set(
        session.scalars(
            select(CollectionUserOverride.user_id).where(
                CollectionUserOverride.collection_id == collection.id, CollectionUserOverride.muted.is_(True)
            )
        )
    )
    return [u for u in audience_users(session, collection) if u.id not in muted]


def recent_theme_names(session: Session, collection_id: int, user_id: int, limit: int = _RECENT_NAMES) -> list[str]:
    """The names of the person's newest themes on this row, newest first, whatever their state."""
    query = (
        select(ThemeHistory.theme_name)
        .where(ThemeHistory.collection_id == collection_id, ThemeHistory.user_id == user_id)
        .order_by(ThemeHistory.started_at.desc(), ThemeHistory.id.desc())
        .limit(limit)
    )
    return [name for name in session.scalars(query) if name]


def promote_next(session: Session, collection_id: int, user_id: int, now: datetime) -> None:
    """Make the person's up-next theme current: the old current becomes `past`, the clock starts at ``now``."""
    rows = _history(session, collection_id, user_id)
    upcoming = rows["next"]
    if upcoming is None or upcoming.theme_id is None:
        raise ValueError("there is no up-next theme to promote")
    _retire_all_current(session, collection_id, user_id)
    days = _theme_days(session.get(Collection, collection_id))
    upcoming.state = "current"
    upcoming.started_at = _naive_utc(now)
    upcoming.due_at = upcoming.started_at + timedelta(days=days)


def theme_guidance(instructions: AiInstructions | None) -> str:
    """The guidance to send the author for a row's AI instructions; "" lets the author use its own default."""
    if instructions is None:
        return ""
    if instructions.mode == "own":
        return instructions.text
    if instructions.mode == "add":
        return f"{BUILD_SYSTEM_GUIDANCE.strip()} {instructions.text}"
    return ""


def explore_brief_for(session: Session, collection: Collection, user_id: int) -> str:
    """What the author is asked: the row's brief (or the default), then the themes to steer clear of."""
    brief = (collection.explore_brief or "").strip() or _DEFAULT_BRIEF
    names = list(
        dict.fromkeys(
            [
                *_clash_names(session, collection.id, user_id),
                *_sibling_theme_names(session, collection, user_id),
                *recent_theme_names(session, collection.id, user_id),
            ]
        )
    )
    return f"{brief}\nAvoid these recent theme names: {', '.join(names)}." if names else brief


def _clash_names(session: Session, collection_id: int, user_id: int) -> list[str]:
    """Themes an earlier try for this person was refused for (a title clash), newest first, so the next try
    does not propose them again. Read from the failure events: a refused theme is never stored."""
    events = session.scalars(
        select(Event)
        .where(Event.scope == "theme.rotate", Event.level == "error")
        .order_by(Event.id.desc())
        .limit(_CLASH_EVENTS_READ)
    )
    names = [
        str(e.message["clash"])
        for e in events
        if e.message.get("collection_id") == collection_id
        and e.message.get("user_id") == user_id
        and e.message.get("clash")
    ]
    return names[:_CLASH_NAMES]


def _sibling_theme_names(session: Session, collection: Collection, user_id: int) -> list[str]:
    """The themes this person's OTHER AI rows wear now or have queued, so two rows are not given one theme name."""
    names: list[str] = []
    others = session.scalars(
        select(Collection).where(
            Collection.id != collection.id,
            Collection.enabled.is_(True),
            Collection.build == "per_person",
            Collection.theme_id.is_not(None),
        )
    )
    for other in others:
        if other.theme_mode == "explore":
            names += session.scalars(
                select(ThemeHistory.theme_name).where(
                    ThemeHistory.collection_id == other.id,
                    ThemeHistory.user_id == user_id,
                    ThemeHistory.state.in_(("current", "next")),
                )
            )
        else:
            base = session.get(Theme, other.theme_id)
            if base is not None:
                names.append(base.name)
    return [n for n in names if n]


def author_for_person(
    session: Session,
    *,
    sessions: Callable[[], Session],
    secrets,
    unavailable: str = "",
    collection: Collection,
    user_id: int,
    author,
    curator,
    tmdb,
    plex,
    profile_for: Callable[[Session, int], UserProfile],
    spent: list[int] | None = None,
    max_details: int | None = None,
) -> Theme:
    """Write a new theme for one person on one row and save it through the theme store (tokens charged to the row).

    The caller owns the commit. Raises `ThemeAuthorError` or `ThemeStoreError` and leaves nothing half-saved.
    ``unavailable`` is why authoring cannot happen at all (no provider, no TMDB key); it is raised as the error.
    ``spent`` receives the tokens the AI call cost the moment it returns, so a caller can still charge them when
    the save that follows fails and rolls back.
    """
    if unavailable:
        raise ThemeAuthorError(unavailable)
    base = session.get(Theme, collection.theme_id) if collection.theme_id is not None else None
    media = _row_media(collection, base)
    index = library_index(
        plex,
        sessions,
        media=collection.media or "both",
        library_keys=[str(k) for k in collection.library_keys or []],
    )
    names = _TagNames(tmdb)
    draft = author(
        brief=explore_brief_for(session, collection, user_id),
        media=media,
        curator=curator,
        tmdb=names,
        plex=plex,
        library_index=index,
        profile=profile_for(session, user_id),
        guidance=theme_guidance(AiInstructions.from_stored(collection.prompt)),
        max_details=max_details,
    )
    if spent is not None:
        spent.append(draft.tokens)
    try:
        body = _save_in(collection, draft, names.seen)
        _refuse_unusable(body.draft)
    except HTTPException as e:
        raise ThemeAuthorError(str(e.detail)) from None
    except ValidationError:
        raise ThemeAuthorError("The AI's theme could not be saved. Try again.") from None
    theme = save_theme(session, secrets, body)
    reject_person_title_clash(session, secrets, collection, user_id, theme)
    return theme


def _rotate_one(
    session,
    collection_id,
    user_id,
    now,
    *,
    sessions,
    secrets,
    unavailable,
    author,
    curator,
    tmdb,
    plex,
    tools,
    profile_for,
    spent,
) -> Action:
    collection = session.get(Collection, collection_id)
    rows = _history(session, collection_id, user_id)
    current, upcoming = rows["current"], rows["next"]
    deps = {
        "sessions": sessions,
        "secrets": secrets,
        "unavailable": unavailable,
        "author": author,
        "curator": curator,
        "tmdb": tmdb,
        "plex": plex,
        "tools": tools,
        "profile_for": profile_for,
        "spent": spent,
    }
    days = _theme_days(collection)
    has_next = upcoming is not None and upcoming.theme_id is not None
    has_current = current is not None and current.theme_id is not None

    if has_current and not _due_to_promote(current, days, now):
        if has_next or not _due_to_author_next(current, days, now):
            return "kept"
        return _author(session, collection, user_id, "next", now, current, **deps)
    if has_next:
        if upcoming_theme := session.get(Theme, upcoming.theme_id):
            # Another of their rows may have taken this title since it was queued.
            try:
                reject_person_title_clash(session, secrets, collection, user_id, upcoming_theme)
            except TitleClash as clash:
                # Discarded, not kept to fail every night: they keep the current theme, the name goes on the
                # avoid line, and the next pass writes a replacement.
                session.delete(upcoming)
                add_audit(
                    session,
                    "theme.rotate",
                    "error",
                    collection_id=collection_id,
                    user_id=user_id,
                    action="discarded",
                    detail=str(clash),
                    clash=clash.theme_name,
                )
                return "failed"
        promote_next(session, collection_id, user_id, now)
        add_audit(
            session,
            "theme.rotate",
            "info",
            collection_id=collection_id,
            user_id=user_id,
            action="promoted",
            theme=upcoming.theme_name,
        )
        return "promoted"
    return _author(session, collection, user_id, "current", now, current, **deps)


def _author(session, collection, user_id, state: str, now, current, **deps) -> Action:
    # The owner may have switched the row off, or off Explore, while earlier people were being written.
    session.refresh(collection)
    if not collection.enabled or collection.theme_mode != "explore":
        return "kept"
    if collection.ai_paused:
        add_audit(
            session,
            "theme.rotate",
            "info",
            collection_id=collection.id,
            user_id=user_id,
            action="skipped_paused",
            detail="AI is paused for this row, so its theme was not changed.",
        )
        return "skipped_paused"
    tools = deps.pop("tools")
    if tools is not None:
        built = tools()
        deps.update(unavailable=built.unavailable, curator=built.curator, tmdb=built.tmdb, plex=built.plex)
    try:
        theme = author_for_person(session, collection=collection, user_id=user_id, **deps)
    except RowPaused:
        # Paused mid-pass: the AI call already ran, so its cost stands.
        if deps["spent"]:
            add_row_tokens(session, collection, sum(deps["spent"]))
        return "skipped_paused"
    days = _theme_days(collection)
    if state == "current":
        _retire_current_and_stale_next(session, collection.id, user_id)
        session.add(_history_row(collection.id, user_id, theme, "current", now, now + timedelta(days=days)))
        return "authored_current"
    queue_next(session, collection, user_id, theme, now, checked=True)
    return "authored_next"


def queue_next(
    session: Session,
    collection: Collection,
    user_id: int,
    theme: Theme,
    now: datetime,
    *,
    secrets=None,
    checked: bool = False,
) -> ThemeHistory:
    """Make ``theme`` the person's up-next theme, replacing any they had. It starts when the current one ends.

    Raises `TitleClash` when it would title this row what another of the person's rows already wears, unless
    the caller has ``checked`` that already (`author_for_person` does, before it saves the theme).
    """
    if not checked:
        reject_person_title_clash(session, secrets, collection, user_id, theme)
    moment = _naive_utc(now)
    session.query(ThemeHistory).filter(
        ThemeHistory.collection_id == collection.id, ThemeHistory.user_id == user_id, ThemeHistory.state == "next"
    ).delete()
    current = _history(session, collection.id, user_id)["current"]
    starts = current.started_at + timedelta(days=_theme_days(collection)) if current is not None else moment
    row = _history_row(collection.id, user_id, theme, "next", moment, starts)
    session.add(row)
    return row


def _due_to_promote(current: ThemeHistory, days: int, now: datetime) -> bool:
    # Judged on the DAY, not the instant: `started_at` is the sub-second clock of the pass that promoted it, so
    # an exact comparison slips the switch a whole day whenever this pass starts a moment earlier than that one.
    return now.date() >= (current.started_at + timedelta(days=days)).date()


def _due_to_author_next(current: ThemeHistory, days: int, now: datetime) -> bool:
    return now.date() >= (current.started_at + timedelta(days=days - NEXT_LEAD_DAYS)).date()


def _history_row(collection_id, user_id, theme: Theme, state: str, started_at, due_at) -> ThemeHistory:
    return ThemeHistory(
        collection_id=collection_id,
        user_id=user_id,
        theme_id=theme.id,
        theme_name=theme.name,
        state=state,
        started_at=started_at,
        due_at=due_at,
    )


def _history(session: Session, collection_id: int, user_id: int) -> dict[str, ThemeHistory | None]:
    """The person's newest `current` and `next` rows on this row."""
    found: dict[str, ThemeHistory | None] = {"current": None, "next": None}
    query = (
        select(ThemeHistory)
        .where(
            ThemeHistory.collection_id == collection_id,
            ThemeHistory.user_id == user_id,
            ThemeHistory.state.in_(("current", "next")),
        )
        .order_by(ThemeHistory.started_at.desc(), ThemeHistory.id.desc())
    )
    for row in session.scalars(query):
        found.setdefault(row.state, None)
        if found[row.state] is None:
            found[row.state] = row
    return found


def _retire_current_and_stale_next(session: Session, collection_id: int, user_id: int) -> None:
    _retire_all_current(session, collection_id, user_id)
    _drop_stale_next(session, collection_id, user_id)


def _retire_all_current(session: Session, collection_id: int, user_id: int) -> None:
    """Every `current` row, not just the newest, so a duplicate left by an earlier race heals."""
    for row in session.scalars(
        select(ThemeHistory).where(
            ThemeHistory.collection_id == collection_id,
            ThemeHistory.user_id == user_id,
            ThemeHistory.state == "current",
        )
    ):
        row.state = "past"


def _drop_stale_next(session: Session, collection_id: int, user_id: int) -> None:
    """A `next` whose theme was deleted points at nothing; it is replaced, not kept."""
    stale = _history(session, collection_id, user_id)["next"]
    if stale is not None and stale.theme_id is None:
        session.delete(stale)


def _theme_days(collection: Collection) -> int:
    return collection.theme_days or DEFAULT_THEME_DAYS


def _row_media(collection: Collection, base: Theme | None) -> tuple[MediaType, ...]:
    if base is not None and base.media:
        return spec_from_row(base).media
    if collection.media in ("movie", "show"):
        return (MediaType(collection.media),)
    return (MediaType.MOVIE, MediaType.SHOW)


def _save_in(collection: Collection, draft: ThemeDraft, tag_names: dict[int, str]) -> ThemeSaveIn:
    """The author's draft as the save the themes API takes, so both paths store a theme the same way."""
    view = _spec_view(draft.spec, brief=draft.brief, origin="ai", titles=draft.titles, tag_names=tag_names)
    view["brief"] = (collection.explore_brief or "").strip() or _DEFAULT_BRIEF
    return ThemeSaveIn(
        draft=ThemeIn.model_validate(view),
        tokens=draft.tokens,
        collection_id=collection.id,
        stats={k: v for k, v in dataclasses.asdict(draft.stats).items() if isinstance(v, int)},
    )


def rotation_targets(sessions) -> list[tuple[int, int]]:
    with sessions() as session:
        rows = session.scalars(
            select(Collection).where(
                Collection.enabled.is_(True),
                Collection.theme_id.is_not(None),
                Collection.theme_mode == "explore",
                Collection.build == "per_person",
            )
        )
        return [(c.id, u.id) for c in rows for u in rotating_users(session, c)]


def _log_failure(sessions, collection_id: int, user_id: int, error: Exception, *, tokens: int = 0) -> None:
    # A ThemeAuthorError is plain English by contract; anything else is its class name only, since an SDK's
    # message can carry a fragment of a key.
    detail = str(error) if isinstance(error, ThemeAuthorError | ThemeStoreError) else type(error).__name__
    logger.warning("theme rotation: collection {} user {} kept its theme ({})", collection_id, user_id, detail)
    try:
        with sessions() as session:
            if tokens:
                # The AI call happened, so its cost stands even though the save rolled back.
                collection = session.get(Collection, collection_id)
                if collection is not None:
                    add_row_tokens(session, collection, tokens)
            clash = {"clash": error.theme_name} if isinstance(error, TitleClash) and error.theme_name else {}
            add_audit(
                session,
                "theme.rotate",
                "error",
                collection_id=collection_id,
                user_id=user_id,
                action="failed",
                detail=detail,
                tokens=tokens,
                **clash,
            )
            session.commit()
    except Exception:
        logger.exception("theme rotation: could not record the failure for collection {}", collection_id)


def _naive_utc(moment: datetime) -> datetime:
    """The DB keeps these timestamps as naive UTC."""
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment
