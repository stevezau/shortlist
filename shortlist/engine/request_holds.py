"""The owner's "don't request these automatically" genres and TMDB tags, for movies and shows apart.

A title with ANY picked genre or tag for its kind is held for the inbox rather than auto-sent, unless it
meets one of that kind's exceptions (a picked production company, or a rating at or above a bar). Only
the automatic pass consults this: sending a held title from the inbox is the owner's own decision, so it
goes.
"""

from collections.abc import Iterable

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import HoldFilter, MediaType, MissingTitle

# The leading words of every hold's inbox reason; `requests.QUEUE_REASON_PREFIXES` carries them so a
# held title classifies as a queue note, never as a failed send.
HOLD_REASON_PREFIX = "held by your request filter"

# TMDB's TV genre list (`/genre/tv/list`). It is not the movie list: shows have one genre for what films
# split in two (Action & Adventure, Sci-Fi & Fantasy, War & Politics), and Kids, News, Reality, Soap and
# Talk exist only for shows. A movie genre id on a show's picks would match nothing, ever.
SHOW_GENRE_IDS = frozenset(
    {10759, 16, 35, 80, 99, 18, 10751, 10762, 9648, 10763, 10764, 10765, 10766, 10767, 10768, 37}
)

_DOCUMENTARY = 99
# Genres that make a film a story rather than footage. Comedy is left out on purpose: a stand-up
# special is Comedy alone on TMDB, and it is exactly what an owner holding "stand-up comedy" means.
_STORY_GENRES = frozenset({28, 12, 16, 80, 18, 10751, 14, 27, 9648, 10749, 878, 53, 10752, 37})


def is_story_film(genre_ids: Iterable[int]) -> bool:
    """Whether TMDB's genres say this is a story film: a story genre, and not a documentary.

    The settings preview flags a held story film, because that is how a pick reads broader than meant
    (the tag "concert" is on A Star Is Born).
    """
    genres = set(genre_ids)
    return bool(genres & _STORY_GENRES) and _DOCUMENTARY not in genres


def match_hold(
    tmdb: TmdbClient, tmdb_id: int, media_type: MediaType, *, genres: frozenset[int], tags: frozenset[int]
) -> str:
    """What holds this title — ``genre Music`` or ``tag “concert film”`` — or "" when nothing does.

    Genres are read first and keywords only when a tag is picked and no genre matched, so a genre-only
    filter never fetches keywords. ``genres`` must be ids from ``media_type``'s own TMDB list.

    Raises:
        Exception: whatever the TMDB read raised, or ValueError for a payload with no genre list (a 404
            reads as ``{}``, which is not evidence the title is allowed).
    """
    if genres:
        raw = tmdb.details(tmdb_id, media_type).get("genres")
        if not isinstance(raw, list):
            raise ValueError("TMDB returned no genre list")
        for genre in raw:
            if isinstance(genre, dict) and genre.get("id") in genres:
                return f"genre {genre.get('name') or genre['id']}"
    if tags:
        keywords = tmdb.movie_keywords(tmdb_id) if media_type is MediaType.MOVIE else tmdb.show_keywords(tmdb_id)
        for tag_id, name in keywords.items():
            if tag_id in tags:
                return f"tag “{name}”"
    return ""


def match_exception(tmdb: TmdbClient, tmdb_id: int, media_type: MediaType, rating: float, holds: HoldFilter) -> str:
    """What lets a matched title go anyway — ``rated 8.7`` or ``studio A24`` — or "" when nothing does.

    ``rating`` is the title's rating on the request pass's chosen source, the one its floors read. It is
    checked first because it costs nothing; a company is read from the same cached detail payload the
    genre check uses, so a genre-only filter pays no extra request for it.

    Raises:
        Exception: whatever the TMDB read raised, or ValueError for a payload with no company list.
    """
    if holds.except_min_rating is not None and rating >= holds.except_min_rating:
        return f"rated {rating:.1f}"
    if holds.except_companies:
        raw = tmdb.details(tmdb_id, media_type).get("production_companies")
        if not isinstance(raw, list):
            raise ValueError("TMDB returned no company list")
        for company in raw:
            if isinstance(company, dict) and company.get("id") in holds.except_companies:
                return f"studio {company.get('name') or company['id']}"
    return ""


def hold_reason(tmdb: TmdbClient, title: MissingTitle, holds: HoldFilter) -> str:
    """The request pass's verdict on one title: what holds it, or "" when it may go out on its own.

    ``holds`` are the picks for the title's own kind (`RequestConfig.holds_for`). A failed read holds the
    title (the owner asked not to auto-send these, and "couldn't check" is not a yes); the next run checks
    it again. A failed exception read keeps the match's hold for the same reason. Errors are logged,
    never put in the reason: client exceptions can carry an authenticated URL.
    """
    if not holds.picked:
        return ""
    try:
        reason = match_hold(tmdb, title.tmdb_id, title.media_type, genres=holds.genres, tags=holds.tags)
    except Exception as e:
        logger.warning(
            "requests: couldn't read TMDB genres/tags for {!r} ({}) — holding it", title.title, type(e).__name__
        )
        return "couldn't read its genres and tags from TMDB; it's checked again next run"
    if not reason:
        return ""
    try:
        exception = match_exception(tmdb, title.tmdb_id, title.media_type, title.rating, holds)
    except Exception as e:
        logger.warning("requests: couldn't read TMDB studios for {!r} ({}) — holding it", title.title, type(e).__name__)
        return reason
    if exception:
        logger.debug("requests: {!r} matches {} but goes anyway — {}", title.title, reason, exception)
        return ""
    return reason
