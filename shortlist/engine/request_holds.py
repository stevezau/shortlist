"""The owner's "don't request these automatically" genres and TMDB tags.

A movie with ANY picked genre or tag is held for the inbox rather than auto-sent. Only the automatic
pass consults this: sending a held title from the inbox is the owner's own decision, so it goes.
"""

from collections.abc import Iterable

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import MediaType, MissingTitle

# The leading words of every hold's inbox reason; `requests.QUEUE_REASON_PREFIXES` carries them so a
# held title classifies as a queue note, never as a failed send.
HOLD_REASON_PREFIX = "held by your request filter"

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


def match_hold(tmdb: TmdbClient, tmdb_id: int, *, genres: frozenset[int], tags: frozenset[int]) -> str:
    """What holds this movie — ``genre Music`` or ``tag “concert film”`` — or "" when nothing does.

    Genres are read first and keywords only when a tag is picked and no genre matched, so a genre-only
    filter never fetches keywords.

    Raises:
        Exception: whatever the TMDB read raised, or ValueError for a payload with no genre list (a 404
            reads as ``{}``, which is not evidence the movie is allowed).
    """
    if genres:
        raw = tmdb.details(tmdb_id, MediaType.MOVIE).get("genres")
        if not isinstance(raw, list):
            raise ValueError("TMDB returned no genre list")
        for genre in raw:
            if isinstance(genre, dict) and genre.get("id") in genres:
                return f"genre {genre.get('name') or genre['id']}"
    if tags:
        for tag_id, name in tmdb.movie_keywords(tmdb_id).items():
            if tag_id in tags:
                return f"tag “{name}”"
    return ""


def hold_reason(tmdb: TmdbClient, title: MissingTitle, *, genres: frozenset[int], tags: frozenset[int]) -> str:
    """The request pass's verdict on one title: :func:`match_hold`, or a hold when TMDB can't say.

    A failed read holds the movie (the owner asked not to auto-send these, and "couldn't check" is not
    a yes); the next run checks it again. The error is logged, never put in the reason: client
    exceptions can carry an authenticated URL. Shows are never held — the picks are movie genres.
    """
    if title.media_type is not MediaType.MOVIE or not (genres or tags):
        return ""
    try:
        return match_hold(tmdb, title.tmdb_id, genres=genres, tags=tags)
    except Exception as e:
        logger.warning(
            "requests: couldn't read TMDB genres/tags for {!r} ({}) — holding it", title.title, type(e).__name__
        )
        return "couldn't read its genres and tags from TMDB; it's checked again next run"
