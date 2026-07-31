"""JSON shapes rendered by more than one router.

These lived in ``api/users.py`` as private helpers, which left ``api/user_rows.py`` importing
``users._pick_dict`` — one router reaching for another's underscore name, which is a statement that
the helper was in the wrong place rather than that the rule should be bent. Anything two routers
render belongs here.
"""

from __future__ import annotations

from shortlist.engine.models import UserType
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.db.models import DEFAULT_SLUG, PickRow, User, iso_utc

#: Every response model in this package sets it. A Pydantic response model does not merely DESCRIBE a
#: response, it FILTERS it — a key the model forgot to declare is dropped from the payload, silently,
#: in production. `extra="allow"` turns the model back into documentation: undeclared keys pass
#: through untouched, so the worst a stale model can do is under-describe the schema.


class UserPickOut(PassthroughModel):
    """One delivered pick as the USER pages render it — the response shape of :func:`pick_dict`.

    Not ``PickOut``: the run detail publishes its own, narrower pick shape under that name
    (``api/schemas_runs.py``), and two models sharing a name make FastAPI fall back to
    fully-qualified schema names for both — which the SPA then generates as
    ``shortlist__server__api__serializers__PickOut``.
    """

    rank: int
    title: str
    reason: str
    media_type: str
    collection_slug: str
    library: str
    section_key: str
    seed_title: str | None
    sources: list[str]
    affinity: float


class UserOut(PassthroughModel):
    """One person — the response shape of :func:`user_dict`."""

    id: int
    plex_account_id: int
    username: str
    slug: str
    nickname: str
    friendly_name: str
    display_name: str
    avatar_url: str
    #: The engine's own enum rather than a Literal repeated here: `users.user_type` is only ever
    #: written from `UserType`, so the two can never drift apart. It is a `StrEnum`, so the payload is
    #: still the plain word ("shared") the SPA already reads.
    user_type: UserType
    restricted: bool
    restriction_profile: str
    enabled: bool
    cold_start: bool
    request_tag: str
    # Free-form JSON, deliberately left untyped here: which keys exist varies by DATA, not by branch
    # (`history_depth` appears after the first watch sync, `paused` only once someone is paused), and
    # a model with defaults would INVENT the absent ones into every payload. `UserPrefs` in
    # `api/users.py` is the shape a client may WRITE; what is stored is whatever an install accrued.
    prefs: dict
    history_depth: int
    last_run_at: str | None
    hit_rate: float | None
    preview_titles: list[str]


def pick_dict(pick: PickRow) -> dict:
    """One delivered pick, as the Users and per-user-rows pages render it.

    Args:
        pick: The stored pick row.

    Returns:
        The pick's rank, title, reason, row/library placement and provenance.
    """
    return {
        "rank": pick.rank,
        "title": pick.title,
        "reason": pick.reason,
        "media_type": pick.media_type,
        "collection_slug": pick.collection_slug or DEFAULT_SLUG,  # legacy blank rows are the default row
        "library": pick.library or "",
        "section_key": pick.section_key or "",
        "seed_title": pick.seed_title,
        # Provenance: which source(s) surfaced it, and how strongly they vouched. Blank/1.0 on rows
        # written before 0035, which the UI renders as "not recorded" rather than "a perfect match".
        "sources": [s for s in (pick.sources or "").split(",") if s],
        "affinity": pick.affinity,
    }


def user_dict(
    user: User,
    history_depth: int,
    last_run_at,
    hit_rate: float | None,
    preview_titles: list[str] | None = None,
) -> dict:
    """One person, as the Users list and the user-detail page render them.

    Args:
        user: The user row.
        history_depth: How many distinct watched titles we last read for them.
        last_run_at: When the most recent run that included them finished, or None.
        hit_rate: Watched-over-delivered across their whole history, or None with no picks yet.
        preview_titles: A few of their most recent pick titles, for the dashboard card.

    Returns:
        The serialized user.
    """
    return {
        "id": user.id,
        "plex_account_id": user.plex_account_id,
        "username": user.username,
        "slug": user.slug,
        "nickname": user.nickname or "",
        # What Tautulli calls them, when it has its own name for them — the default a blank
        # nickname falls back to, shown in the UI so the field's placeholder can be honest.
        "friendly_name": user.friendly_name or "",
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "user_type": user.user_type,
        "restricted": user.restricted,
        # "" when no parental preset is set. A managed account WITHOUT one is an ordinary user that
        # gets rows and privacy filters; one WITH a preset sees no collections at all and Plex refuses
        # to filter it. The UI needs the difference to say anything true (#20).
        "restriction_profile": user.restriction_profile or "",
        "enabled": user.enabled,
        "cold_start": user.cold_start,
        "request_tag": user.request_tag or "",
        "prefs": user.prefs or {},
        "history_depth": history_depth,
        "last_run_at": iso_utc(last_run_at),
        "hit_rate": hit_rate,
        # A few of their most recent pick titles, for a real preview strip on the dashboard card.
        "preview_titles": preview_titles or [],
    }
