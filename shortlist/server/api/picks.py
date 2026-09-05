"""Artwork for delivered picks, proxied from the Plex Media Server.

**The PMS, not TMDB, and that is the whole design.** A `Pick` is built at four places in the engine
and only one of them has a TMDB `Candidate` to take a `poster_path` from — so a TMDB-backed row list
would leave every cold-start and every shared row blank, which are precisely the rows a picture helps
most. All four carry a `rating_key`, and a pick is in the delivery library by construction, so the
server the owner already runs answers for every one of them: no new column, no migration, no backfill
gap, and the art matches whatever Kometa or TMM put on the item rather than silently differing from
it. The request inbox keeps TMDB because an inbox title is by definition *not* on the server yet.

The owner's Plex token never leaves this process (rule 9): the SPA asks over its own session cookie
and this handler does the PMS read with the token in a header. A `rating_key` is not a secret — it
names an item on a server the owner owns, and every other endpoint they can call exposes far more.
"""

from __future__ import annotations

import hashlib
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from starlette.concurrency import run_in_threadpool

from shortlist.server.auth import require_owner
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/picks", tags=["picks"], dependencies=[Depends(require_owner)])

#: Five minutes, then revalidate. `private` because these bytes are one owner's library, not a public
#: CDN's.
#:
#: NOT a week, and the reasoning matters. The ETag carries the artwork's own stamp, but that stamp
#: comes from `_THUMB_MEMO`, which is only refreshed when a read FAILS — so on its own the ETag
#: cannot notice new artwork, and a week-long `max-age` means the browser never asks. What actually
#: happened when the owner replaced a poster was: the memoised stamp 404s at the PMS, this answers
#: 502, the memo is dropped, and the NEXT load is correct — one broken tile, not free invalidation.
#: A short max-age with revalidation lets the ETag do the job it was credited with: the browser asks,
#: the memo is re-resolved, and a changed stamp returns new bytes instead of a 304.
_CACHE_CONTROL = "private, max-age=300, must-revalidate"

#: How long a resolved thumb path is trusted before it is looked up again. Bounds "the owner changed
#: the artwork and one tile broke" to five minutes rather than to a process restart.
_THUMB_MEMO_TTL_S = 300

#: `rating_key -> (thumb path, resolved at)`. Resolving the path is a PMS metadata read; serving the
#: bytes is a second one. Memoising the first halves the round-trips for a list the owner scrolls
#: back to, and the timestamp bounds how long a path survives the owner changing the artwork.
#:
#: Deliberately NOT a byte cache. `poster_assets` exists for the handful of row posters the owner
#: uploaded; a per-title image cache would grow with the library and has no eviction story. The
#: browser cache is the byte cache, and it is already better at it.
_THUMB_MEMO: dict[int, tuple[str, float]] = {}

#: Bounded so a long-running server cannot accumulate one entry per library item. Small because the
#: working set is "the posters on the page in front of the owner", not the library.
_THUMB_MEMO_MAX = 4096


#: The live client, keyed on a DIGEST of the credentials that built it. Re-linking a server changes
#: the digest and builds a new client, so nothing can hand back one pointed at the wrong PMS.
#:
#: A digest rather than the values, because this is a module global: keying it on the token would put
#: the token in plaintext in anything that reprs module state — a traceback, a debugger, a heap dump.
#: The client holds its own copy either way; there is no reason for a second one here (rule 9).
_CLIENT: dict[str, object] = {}

#: Guards the check-then-set on `_CLIENT`. Every PMS call below runs in the threadpool, so two
#: concurrent poster requests really are two OS threads here — without this, a page of ten posters
#: could build ten `PlexServer`s (ten `GET /` handshakes) and keep the last one, which is the
#: opposite of what the cache is for.
_CLIENT_LOCK = threading.Lock()


def _credentials_digest(url: str, token: str) -> str:
    return hashlib.sha256(f"{url}\0{token}".encode()).hexdigest()


def _plex_client(url: str, token: str, timeout: int):
    """A PlexClient for these credentials, cached. Blocking — call it in a threadpool.

    Kept between requests, unlike every other `_plex_client` in this codebase, because this endpoint
    is called once per PICTURE rather than once per page: constructing a `PlexServer` costs a `GET /`
    handshake (measured), so a rebuild per image made a ten-poster list pay ten of them on top of the
    reads it actually needed. Only `fetch_items` and a raw artwork GET are used here, neither of which
    touches the client's per-run section/collection caches, so there is nothing to go stale.

    Takes plain values rather than a `SettingsStore` so the caller can read settings on the event
    loop and hand this thread nothing that belongs to a SQLAlchemy session.
    """
    from shortlist.engine.clients.plex_pms import PlexClient

    key = _credentials_digest(url, token)
    with _CLIENT_LOCK:
        if key not in _CLIENT:
            # One entry: a re-link supersedes the old credentials rather than accumulating beside
            # them. The thumb memo goes with it — its paths are ratingKeys on the OLD server, and the
            # same key on a new one names a different item.
            _CLIENT.clear()
            _THUMB_MEMO.clear()
            _CLIENT[key] = PlexClient(url, token, timeout=timeout)
        return _CLIENT[key]


def _memo_thumb(rating_key: int, thumb: str) -> None:
    if len(_THUMB_MEMO) >= _THUMB_MEMO_MAX:
        _THUMB_MEMO.clear()
    _THUMB_MEMO[rating_key] = (thumb, time.monotonic())


def _memoised_thumb(rating_key: int) -> str | None:
    """The remembered path, or None once it is older than the TTL."""
    entry = _THUMB_MEMO.get(rating_key)
    if entry is None or time.monotonic() - entry[1] > _THUMB_MEMO_TTL_S:
        return None
    return entry[0]


@router.get("/{rating_key}/poster")
async def pick_poster(rating_key: int, request: Request) -> Response:
    """One delivered pick's artwork, streamed from the PMS.

    404 rather than a placeholder image when the item or its artwork is gone: a pick's ratingKey goes
    stale when a title is removed and re-added, and the SPA already renders a tile of the right size
    for that. Answering with a picture would make a missing item indistinguishable from a real one.
    502 when the PMS itself failed, for the same reason — an outage is not "this title has no art".

    Args:
        rating_key: The pick's Plex ratingKey. `0` means the pipeline never matched the title.
        request: The live request, for the app state and `If-None-Match`.

    Returns:
        The image bytes with the PMS's own content type, or a `304` when the browser's copy is
        current.

    Raises:
        HTTPException: `404` when there is no artwork to serve, `502` when Plex could not be read.
    """
    # `picker.py` stores `c.rating_key or 0`, so `0` is a real value meaning "never matched". Answered
    # without touching Plex: on a page of unmatched picks this is the difference between one round-trip
    # each and none at all.
    if rating_key <= 0:
        raise HTTPException(status_code=404, detail="this pick was never matched to a library item")

    # Settings are a local SQLite read; the session never leaves this thread. Everything after this
    # is network I/O and belongs in the threadpool.
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        url, token = str(store.get("plex.url") or ""), str(store.get("plex.token") or "")
        timeout = int(store.get("plex.timeout_s") or 45)
    if not url or not token:
        raise HTTPException(status_code=404, detail="Plex isn't connected")

    if_none_match = request.headers.get("if-none-match")
    result = await run_in_threadpool(_fetch_poster, rating_key, url, token, timeout, if_none_match)
    if result is None:
        raise HTTPException(status_code=404, detail="no artwork for this item")
    if isinstance(result, str):  # an ETag alone: the browser's copy is current
        return Response(status_code=304, headers={"ETag": result, "Cache-Control": _CACHE_CONTROL})
    body, content_type, etag = result
    return Response(body, media_type=content_type, headers={"ETag": etag, "Cache-Control": _CACHE_CONTROL})


def _fetch_poster(
    rating_key: int, url: str, token: str, timeout: int, if_none_match: str | None
) -> tuple[bytes, str, str] | str | None:
    """The whole PMS conversation for one poster, off the event loop.

    Every step here BLOCKS: building the client is a plexapi `GET /` handshake, `item_thumb_path`
    uses `requests`, and `read_artwork` goes through `http_retry`, which `time.sleep`s between its
    retries. Run inline on an `async def` handler, one slow or unreachable PMS would stall the whole
    ASGI loop — SSE run progress and every other API call — for `plex.timeout_s` per image, and a
    pick list fires ten to twenty of these at once. Same reason `collections.get_poster_image` and
    `system`'s Plex probes are already off the loop.

    Args:
        rating_key: The item's Plex ratingKey.
        url: The PMS base URL.
        token: The owner's Plex token.
        timeout: Per-read timeout in seconds.
        if_none_match: The browser's `If-None-Match`, if it sent one.

    Returns:
        `(bytes, content_type, etag)` to serve, the ETag alone for a `304`, or None when there is no
        artwork to serve.

    Raises:
        HTTPException: `502` when the PMS could not be read at all.
    """
    thumb = _memoised_thumb(rating_key)
    try:
        client = _plex_client(url, token, timeout)
        if thumb is None:
            thumb = client.item_thumb_path(rating_key)
            if thumb is None:
                return None
            _memo_thumb(rating_key, thumb)

        # The trailing segment of a Plex thumb path is the artwork's own stamp, so an ETag built from
        # it changes when the artwork does. Weak, because the PMS may re-encode.
        etag = f'W/"{rating_key}-{thumb.rsplit("/", 1)[-1]}"'
        if if_none_match == etag:
            return etag

        body, content_type = client.read_artwork(thumb)
    except Exception as e:
        # A stale memo (the item was re-added with new art) reads as a failure, so it is dropped
        # rather than left to fail every future request for this key.
        _THUMB_MEMO.pop(rating_key, None)
        logger.debug("could not read artwork for ratingKey={} ({})", rating_key, type(e).__name__)
        raise HTTPException(status_code=502, detail="Plex could not be read") from e
    return body, content_type, etag
