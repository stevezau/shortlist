"""PMS client (plexapi) — collection/library operations, restricted to what Shortlist owns.

Plex quirks encoded here (all live-verified in Phase 0, 2026-07-12):
- Plex fixes a collection's subtype from the items it is CREATED with and never revises it, so a
  mistyped collection must be rebuilt, never edited (see ``matches_section``).
- Plex title-cases new labels (``shortlist_x`` -> ``Shortlist_x``); callers must use the label
  *as stored*, so collection helpers always read labels back after writing.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise

import httpx
import requests
from loguru import logger
from plexapi.collection import Collection
from plexapi.exceptions import NotFound
from plexapi.library import LibrarySection
from plexapi.server import PlexServer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from shortlist.engine.clients import http_retry
from shortlist.engine.models import MediaType, OwnedRow, WatchedItem
from shortlist.engine.watch_replica import ItemState, OpKind, WatchState, WriteOp

# Label restrictions only apply on Home/Recommended/Related from this PMS build (PM-5174).
MIN_PMS_VERSION = (1, 43, 2, 10687)


@dataclass(frozen=True)
class PlayEvent:
    """One play from the server's own history log — who, what, exactly when.

    Deliberately not a `WatchedItem`: that models watched STATE for a title (counts, flags, a
    high-water timestamp), while this is a single event with no notion of how much was seen.
    """

    plex_account_id: int
    rating_key: int
    show_rating_key: int | None
    media_type: str
    viewed_at: datetime
    #: Plex's own row id, unique per event — the dedupe key, since the log repeats itself.
    history_key: str | None


@dataclass(frozen=True)
class WatchedRead:
    """One library's watched titles, and whether the read can be trusted to be COMPLETE for its window.

    `covers_window` exists because absence has two very different meanings. When the walk provably
    returned everything at or after the cutoff, a cached title missing from `items` was un-watched.
    When the walk was truncated — a server that omits `totalSize` and caps the container, a sort that
    was not honoured — the same absence means only "we did not read that far". Deleting on the second
    is data loss, so the flag travels with the items rather than being assumed by the caller.

    For a complete (`since=None`) read the window is the WHOLE library, and the flag is earned the
    same way: the server gave a `totalSize` and the walk reached it. The cache's periodic reconcile
    replaces a section outright from such a read, so an unproven one is the most destructive shape
    available — a single short page would drop every title behind it and report success.
    """

    items: list[WatchedItem]
    covers_window: bool
    #: Rows the server returned that carry no `tmdb://` guid, so nothing here could ever match them.
    #: Zero on a healthy library; a whole library's worth when it is matched with a legacy agent.
    dropped_no_guid: int = 0


class SectionNotShared(RuntimeError):
    """A library this person's token cannot see — the PMS answered 403.

    Not a failure: the owner simply hasn't shared that library with them, so "nothing watched there"
    is the *correct* answer, not a degraded one. Callers must treat it as a skip rather than an
    unreadable section — see `PlexPMS.watched_titles`.
    """


# Shortlist's invisible per-account title marker is exactly 64 zero-width chars (see
# delivery.row_marker). Checked locally here rather than imported to avoid a delivery↔client import
# cycle; the two definitions must stay in lockstep.
_MARKER_CHARS = ("​", "‌")


def has_shortlist_marker(title: str) -> bool:
    suffix = title[-64:]
    return len(suffix) == 64 and all(c in _MARKER_CHARS for c in suffix)


#: What `_watched_item` dates a row that carries no `lastViewedAt`. A show marked watched rather than
#: played has none, so this is a real and common value, not a corrupt one — see `_dates_from_episodes`.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


#: The identifier family Plex gives a COLLECTION's hub. Prefix only, and deliberately NOT a format.
#: The two shapes recorded off a real PMS disagree about everything after it:
#: `custom.collection.1.527794.527794` (section id, and the ratingKey DOUBLED —
#: `pms_hubs_shared_account.json`, PMS 1.43.3) and `custom.collection.571285` (no section id at all —
#: `pms_hubs_home.json`). plexapi's own `custom.collection.<sectionID>.<ratingKey>` (`collection.py`,
#: `visibility()`) is what it SYNTHESIZES when a hub is missing, and matches neither capture — so
#: anything stricter than the family name rejects a real collection and silently disables the guard.
#: Built-ins are a different family (`home.television.recentlyadded`, `movie.recentlyadded`).
#:
#: Both captures are `hubIdentifier` on `/hubs`, not `identifier` on `/hubs/sections/<key>/manage`,
#: which is what this actually reads — hence the fail-open note below. (The `custom.collection~68`
#: and bare `custom.collection` strings in those fixtures are `context` values, a different field.)
_COLLECTION_HUB_PREFIX = "custom.collection"


def is_collection_hub(hub) -> bool:
    """Whether a managed hub IS one of the library's collections, as opposed to a built-in Plex hub.

    By IDENTIFIER, never by title. Titles COLLIDE — "Top Rated" is both a stock Plex hub and a stock
    Kometa collection — so a built-in would be mistaken for a collection and refused as an anchor. And
    a title check has to be answered from ``section.collections()``, a listing that can come back
    SHORT; a truncated one would reclassify a real collection as a built-in and wave through the very
    burial issue #106 is about. The identifier travels on the hub itself, so neither applies.

    FAILS OPEN, and that is the intended direction. No fixture records
    ``/hubs/sections/<key>/manage`` itself (plex-safety rule 11) — the identifier captures we have are
    from ``/hubs`` — so if that endpoint ever names a collection differently this returns False, the
    hub reads as a built-in, and the row is placed exactly as it was before issue #106 was fixed. That
    is the old bug back, never a placement that works being refused.
    """
    return str(getattr(hub, "identifier", "") or "").startswith(_COLLECTION_HUB_PREFIX)


def can_anchor(hub) -> bool:
    """Whether a hub is something a row can be placed relative to — i.e. it HAS a position to sit
    next to. Only a collection is judged; see `is_collection_hub` for why a built-in never is.

    ONE definition, called by the engine's ordering pass AND by the editor's anchor picker
    (`api/system.library_collections`). They have to agree: `on_shelf` in the picker exists purely to
    predict what the ordering pass will do, so a disagreement greys out an anchor that places fine, or
    offers one that will be refused. This rule has been rewritten three times over issue #106 with the
    two copies kept in step by hand, which is a function's job, not a reviewer's.
    """
    return not is_collection_hub(hub) or is_promoted(hub)


def is_promoted(hub) -> bool:
    """Whether a managed hub is on ANY surface — shared Home, the owner's Home, or Recommended.

    ``managedHubs()`` lists every managed hub, promoted or not. A hub with all three flags off is
    invisible to everyone, so its position on the shelf is meaningless and moving it is pure churn.
    Any single flag is enough: a row on the owner's Home alone still has a visible position.
    """
    return any(
        bool(getattr(hub, flag, False))
        for flag in ("promotedToSharedHome", "promotedToOwnHome", "promotedToRecommended")
    )


def _marker_account(title: str) -> int | None:
    """The Plex account id this title's marker encodes, or None if it carries no marker.

    The local twin of ``delivery.marker_account`` — same reason ``_MARKER_CHARS`` is duplicated above:
    ``delivery`` imports from this module, so importing it back is a cycle. Kept as one function
    rather than inlined twice; ``log_title`` reads it too.
    """
    if not has_shortlist_marker(title):
        return None
    suffix = title[-64:]
    return sum((1 << bit) for bit, c in enumerate(suffix) if c == _MARKER_CHARS[1])


def log_title(title: str) -> str:
    """A collection title fit for a LOG LINE — never for matching or writing to Plex.

    The per-account marker is 64 zero-width characters, so a raw title renders as
    `✨ Movies Picked for You` followed by 64 invisible chars. Every delivery, promote and ordering
    line carried them: the log looked corrupted, wrapped absurdly, and — worst — two users' rows were
    IMPOSSIBLE to tell apart by eye, because the only thing distinguishing them is invisible.

    Strip the marker and print the account id it encodes instead. Same information, legible, and
    actually more useful: you can now see whose row a line is about.
    """
    account = _marker_account(title)
    if account is None:
        return title
    return f"{title[:-64]} [acct {account}]"


def parse_pms_version(version: str) -> tuple[int, ...]:
    """'1.43.3.10793-cd55560bb' -> (1, 43, 3, 10793)."""
    numbers = version.split("-")[0].split(".")
    return tuple(int(n) for n in numbers if n.isdigit())


def _tmdb_guid(item) -> int | None:
    """The item's TMDB id, or None. The one place the ``tmdb://`` guid grammar lives."""
    for guid in getattr(item, "guids", []):
        if guid.id.startswith("tmdb://"):
            try:
                return int(guid.id.removeprefix("tmdb://"))
            except ValueError:
                # A malformed guid must not raise out of a whole section scan — every other
                # tolerant spot in this file (label parsing, watched-item ids) skips a bad row
                # rather than failing the caller; this one didn't, and one bad guid killed it.
                continue
    return None


# A PMS call slower than this is logged at WARNING (the rest are DEBUG-timed). Delivery reads on a
# busy single-writer PMS are the dominant run cost, so making every slow one visible is how we tell
# lock-wait from real work — see _TimingHTTPAdapter.
_SLOW_PMS_S = 5.0


class _TimingHTTPAdapter(HTTPAdapter):
    """Times every PMS HTTP call (including its urllib3 retries) so the delivery path is not a black hole.

    plexapi talks to the PMS through ``requests`` DIRECTLY, bypassing the logged ``http_retry`` wrapper
    that instruments Tautulli/TMDB — so a slow ``collection.items()``/``addItems``/``fetch`` was
    completely invisible in the logs (SFLIX run 3: 465s per TV row with zero log lines, 2026-07-19).
    Logs method + path + status + duration; the query string is dropped so the ``X-Plex-Token`` never
    reaches the log (rule 9).
    """

    def send(self, request, **kwargs):
        start = time.monotonic()
        status: object = "ERR"
        try:
            response = super().send(request, **kwargs)
            status = response.status_code
            return response
        finally:
            duration = time.monotonic() - start
            path = request.path_url.split("?", 1)[0]  # drop the query string (carries X-Plex-Token)
            if duration >= _SLOW_PMS_S:
                logger.warning("PMS SLOW · {} {} -> {} in {:.1f}s", request.method, path, status, duration)
            else:
                logger.debug("PMS · {} {} -> {} in {:.2f}s", request.method, path, status, duration)


def _retrying_session() -> requests.Session:
    """A requests session that retries transient PMS failures (read/connect timeouts, 429, 5xx).

    plexapi talks to the PMS over ``requests``; without this a single slow response fails the whole
    run (SFLIX run 3 died on one 30s read timeout). Only idempotent methods are retried, so a
    collection create/label (POST/PUT) is never repeated — just the reads that dominate a run.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.5,  # waits ~0s, 1.5s, 3s between tries
        # urllib3 defaults this to 0.0 — no jitter at all. Every parallel user shares one PMS, so a
        # 429 or a slow read hits them together and they would otherwise retry in step.
        backoff_jitter=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = _TimingHTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


_PMS_TIMEOUTS = (
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ConnectionError,
)

#: Plex answering with a server error rather than dropping the connection. Same transient overload
#: as a read timeout — SFLIX 2026-09-06: `PUT /library/collections/687180/items` returned 500 after
#: exactly 10.0s while that very collection served eight GETs and a children read as 200 either
#: side of it — but it arrives as a `BadRequest`, not a timeout, so the retry ladder never saw it
#: and one wobble failed a whole user for the run.
_PMS_SERVER_ERROR_PREFIXES = ("(500)", "(502)", "(503)", "(504)")


def _is_transient_pms_error(error: BaseException) -> bool:
    """Whether a plexapi exception is a server-side wobble worth repeating.

    Anchored on the LEADING token, never ``"500" in``: plexapi formats the message as
    ``f'({status}) {codename}; {url} {errtext}'`` and that url carries the collection's own
    ratingKey, so a substring test matches keys like 1500 or 45002 — the mistake that once swallowed
    500s and 401s in ``delivery._rename_or_keep``.
    """
    return str(error).startswith(_PMS_SERVER_ERROR_PREFIXES)


# How many times `order_owned_hubs` will re-read the managed shelf and re-place whatever did not end
# up where it asked. A co-managing tool (agregarr, Kometa) reorders the same shelf on its own
# schedule, so a pass can genuinely lose a race; each retry re-reads first, so the next pass moves
# only what is still wrong. Three is enough for a shelf that converges and cheap for one that doesn't.
_HUB_ORDER_ATTEMPTS = 3


class CollectionRejectedItems(RuntimeError):
    """Plex answered 400 when items were ADDED to a collection — not to any other write on it.

    Observed on a real server: an empty collection of ours refused a batch of 30 valid shows AND a
    single one, while a sibling in the same library accepted the same item seconds later. The object
    itself was broken and stayed broken run after run. `delivery._deliver_one` treats this, and only
    this, as grounds to rebuild the row.
    """


def _retry_idempotent(
    operation: Callable[[], None],
    *,
    label: str,
    attempts: int = 4,
    already_done: type[BaseException] | tuple[type[BaseException], ...] | None = None,
) -> None:
    """Retry an IDEMPOTENT PMS mutation (promotion, or a delivery collection upsert) on a read/connect
    timeout OR a 5xx, backing off between tries.

    A busy PMS expresses the same overload two ways — it drops the connection, or it answers 500 —
    and only the first used to be retried. Both mean "try again", and neither says the request was
    wrong; a 4xx still raises on the first attempt so a genuine rejection is never repeated.

    The requests-level ``Retry`` only covers GETs, so every caller here has to carry its own reason
    to be safe to repeat, and they are not all the same reason:

    * promotion — hide + hub visibility is the same state re-applied, so a repeat is a no-op;
    * ``addItems`` — one PUT with SET semantics, so re-adding a member changes nothing;
    * a per-item removal — see ``set_items``; the repeat is bounded to the one item that failed,
      and ``already_done`` covers the item the failed attempt had in fact removed;
    * a label write — an ABSOLUTE tag set rebuilt from the same in-memory list, so re-sending it
      writes the identical set;
    * poster upload/reset — last write wins.

    At scale a busy PMS pushes these into read timeouts, and one un-retried timeout used to fail the
    whole user (SFLIX 48-user rollout, 2026-07-18). The backoff also gives the server air.

    ``already_done`` is the exception type meaning "the thing you asked for is already true" on a
    RETRY — never on the first attempt, where it is a genuine surprise worth raising.
    """
    for attempt in range(attempts):
        try:
            operation()
            return
        except Exception as error:
            if already_done is not None and attempt > 0 and isinstance(error, already_done):
                # The first attempt applied it and then failed to say so — a 500 after Plex had
                # already done the work. Asking again for a state that now holds is success.
                logger.debug("{}: already applied on retry ({})", label, type(error).__name__)
                return
            if not isinstance(error, _PMS_TIMEOUTS) and not _is_transient_pms_error(error):
                raise
            if attempt == attempts - 1:
                raise
            # Jittered: eight users deliver in parallel, so a PMS wobble times out all of them within
            # the same second and an unjittered ladder marches them back in lockstep.
            delay = http_retry.jittered(2.0 * (2**attempt))  # ~2s, ~4s, ~8s
            logger.warning(
                "{}: PMS {} — retry {}/{} in {:.0f}s",
                label,
                type(error).__name__,
                attempt + 1,
                attempts - 1,
                delay,
            )
            time.sleep(delay)


def _forbid_redirects(session) -> None:
    """Make every request on this session refuse to follow redirects.

    Wraps `request` rather than setting an attribute: `requests` decides per call, so a library that
    issues its own requests (plexapi does) would otherwise silently follow them.
    """
    original = session.request

    def request(method, url, **kwargs):
        kwargs["allow_redirects"] = False
        return original(method, url, **kwargs)

    session.request = request


def _is_newest_first(entries: list) -> bool:
    """Are this page's `lastViewedAt` stamps in non-increasing order?

    Rows without the attribute are ignored rather than treated as 1970 — a data gap is not evidence
    that the sort broke, and treating it as such would throw away the incremental read for everyone.
    """
    return all(a >= b for a, b in pairwise(_stamps(entries)))


def _merge_leaf(prior: ItemState, later: ItemState) -> ItemState:
    """Combine the two reads that can both return one item.

    A title that is watched AND part-way through appears in `unwatched=0` and in `viewOffset>0`, and
    each read carries only its own field reliably. Taking the max of both means the order the reads
    ran in cannot change the answer.
    """
    return ItemState(
        rating_key=prior.rating_key,
        media_type=prior.media_type,
        view_count=max(prior.view_count, later.view_count),
        view_offset_ms=max(prior.view_offset_ms, later.view_offset_ms),
        last_viewed_at=max(prior.last_viewed_at, later.last_viewed_at),
        show_rating_key=prior.show_rating_key or later.show_rating_key,
        title=prior.title or later.title,
    )


def _float_or_none(raw: str | None) -> float | None:
    """A PMS attribute as a float, or None when it is absent or unparseable.

    Unparseable reads as absent on purpose: one malformed attribute must not raise out of a whole
    library's watched read, the same tolerance `_watched_item` already gives a malformed guid.
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _stamps(entries: list) -> list[int]:
    """This page's parseable `lastViewedAt` values, in the order the server returned them."""
    stamps: list[int] = []
    for el in entries:
        raw = el.get("lastViewedAt")
        if raw is None:
            continue
        try:
            stamps.append(int(raw))
        except (TypeError, ValueError):
            continue
    return stamps


class PlexClient:
    """PMS operations, restricted to collections Shortlist owns (label-gated)."""

    # Class-level fallback ONLY for test doubles built via `PlexClient.__new__` (see
    # `tests/conftest.py`'s `mock_plex`, which skips `__init__`); every real instance overrides this
    # in `__init__` below with the operator's configured `plex.timeout_s`.
    _timeout: int = 20

    def __init__(self, base_url: str, token: str, *, timeout: int = 20, follow_redirects: bool = True):
        # This 20s default is for the fast-fail connection probes (setup/test-connection/section list).
        # The RUN's client is built by context_builder with the configurable `plex.timeout_s` (default
        # 45), because a large TV library's collection rebuild legitimately takes 15-20s and 20s timed
        # those out + retried (SFLIX 47-user run, 2026-07-20).
        # On why the default is 20, not 60: on a LAN PMS a single call taking >20s means the server is
        # stalled, not working, and waiting the full 60s just multiplied the damage (a stuck GET retried
        # 4x = ~240s, serialized behind the write-lock; SFLIX run 3, 2026-07-19). The retrying session's
        # backoff still covers real transients, and the reorder no longer holds the write-lock (deferred,
        # best-effort) so the old "keep the ceiling high for the busy reorder" reason is gone.
        session = _retrying_session()
        if not follow_redirects:
            # `net_guard.check_url` validates the ADDRESS, and its own docstring says the check is
            # worthless unless the caller also refuses redirects — a permitted host can answer 302 and
            # bounce the fetch to a blocked one. Enforced at the session, not per call, because
            # plexapi issues the requests and would otherwise have to be trusted to pass the flag.
            _forbid_redirects(session)
        self._server = PlexServer(base_url, token, session=session, timeout=timeout)
        # The ADMIN token, kept for the raw reads that are deliberately server-wide rather than
        # per-user: `play_history` is one call covering every account, where the watched-state read is
        # made as each user with their own share token.
        self._token = token
        # Every raw (non-plexapi) PMS read in this class must use THIS, not a hardcoded number —
        # plexapi's own calls already get `timeout` via the PlexServer above; `user_hubs` and
        # `_read_watched_page` used to hardcode 30/45 here, silently ignoring the operator's
        # configured `plex.timeout_s` on exactly the two heaviest raw reads.
        self._timeout = timeout
        # Per-run read caches. A PlexClient is built fresh for each run (the server adapter
        # constructs one per run), so these live exactly one run — no cross-run staleness. Library
        # sections don't change mid-run; a section's collection LIST changes only when WE create or
        # delete one, so it is busted on exactly those two operations. Item edits, label adds and
        # promotes mutate the cached Collection objects in place, so they need no busting.
        self._sections_cache: list[LibrarySection] | None = None
        self._collections_cache: dict[str, list[Collection]] = {}
        # Per-run memo for cold-start top-rated: the highest-rated titles of a section are identical for
        # every cold user, so a cold-heavy night (a Tautulli outage pushes many users below min_history)
        # otherwise repeats the same search O(cold_users x sections). Keyed (section.key, limit).
        self._top_rated_cache: dict[tuple[str, int], list[tuple[int, object]]] = {}

    @property
    def machine_id(self) -> str:
        return self._server.machineIdentifier

    @property
    def version(self) -> str:
        return self._server.version

    @property
    def server_name(self) -> str:
        """The server's friendly name — what setup/settings show the owner."""
        return self._server.friendlyName

    def sections(self, types: tuple[str, ...] = ("movie", "show")) -> list[LibrarySection]:
        if self._sections_cache is None:
            self._sections_cache = self._server.library.sections()
        return [s for s in self._sections_cache if s.type in types]

    def _section_collections(self, section: LibrarySection) -> list[Collection]:
        """This section's collections, fetched once per run and reused (busted on create/delete).

        The full collection list of a section is otherwise re-pulled for every owned-collections
        scan, every delivery, and every promote — the biggest single source of repeated PMS reads.
        """
        if section.key not in self._collections_cache:
            self._collections_cache[section.key] = section.collections()
        return self._collections_cache[section.key]

    def _invalidate_collections(self) -> None:
        """Drop the collection-list cache so the next read re-fetches from the PMS. Delete uses this
        (a removed collection must vanish from the list); create instead APPENDS to keep the cache warm
        (see ``create_collection``) — so the cache is a complete mirror of the server's shortlist rows
        WITHIN a single (single-flight) run, but the privacy sync still forces a fresh read of its own
        (``invalidate_collections_cache`` before its enumeration) rather than trusting that."""
        self._collections_cache.clear()

    def invalidate_collections_cache(self) -> None:
        """Public: force the next collection read to hit the PMS. The privacy sync calls this so its
        row enumeration is a genuine fresh server read, never the in-process (warm) cache."""
        self._invalidate_collections()

    def sections_by_type(self) -> dict[MediaType, LibrarySection]:
        """One representative library per media type — used for cold-start discovery, NOT for
        choosing where rows are delivered.

        Row delivery targets ``library_keys`` (all libraries by default; see the delivery module),
        so a server with several libraries of a type builds rows in every one. This helper is only a
        stable single pick per type for the callers that need just one: the lowest section key
        wins — deliberately NOT the order the PMS lists them in, so a reordering can't shift which
        library those callers read.
        """
        by_type: dict[MediaType, LibrarySection] = {}
        for section in sorted(self.sections(), key=lambda s: int(s.key)):
            kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
            by_type.setdefault(kind, section)
        return by_type

    def build_library_index(self, section: LibrarySection) -> dict[int, int]:
        """Scan a section once, returning ``tmdb_id -> ratingKey`` for every TMDB-identified item.

        The finished-show fraction no longer needs a total episode count here — the share-token watch
        read carries each user's own ``viewedLeafCount``/``leafCount`` (marks included), so the total is
        read per user rather than reconstructed from a server-wide index.
        """
        index: dict[int, int] = {}
        for item in section.all():
            tmdb_id = _tmdb_guid(item)
            if tmdb_id is not None:
                index[tmdb_id] = item.ratingKey
        logger.debug(
            "library index for '{}': {} of {} items have TMDB ids", section.title, len(index), section.totalSize
        )
        return index

    def section_signature(self, section: LibrarySection) -> str | None:
        """A cheap fingerprint of a section's contents for the cross-run index cache — its item count
        plus last-updated stamp, both already loaded on the section (no extra PMS call). Returns None
        when neither is available, which tells the caller to scan rather than trust a cache."""
        total = getattr(section, "totalSize", None)
        updated = getattr(section, "updatedAt", None)
        if total is None and updated is None:
            return None
        stamp = int(updated.timestamp()) if hasattr(updated, "timestamp") else updated
        return f"{total}:{stamp}"

    def top_rated(self, section: LibrarySection, limit: int) -> list[tuple[int, object]]:
        """Highest audience-rated titles that carry a TMDB id — the cold-start 'popular' source.

        Returns ``(tmdb_id, item)`` pairs, up to ``limit``. Over-fetches (2x) because titles with
        no TMDB guid are skipped, so a library with sparse ids still fills the request. Owns the
        ``tmdb://`` guid grammar so cold start never has to parse a guid itself.

        Memoized per (section, limit) for the run: a section's top-rated list is the same for every
        cold-start user, so many cold users share one PMS search instead of one each.
        """
        cache_key = (str(section.key), limit)
        if cache_key in self._top_rated_cache:
            return self._top_rated_cache[cache_key]
        out: list[tuple[int, object]] = []
        for item in section.search(sort="audienceRating:desc", limit=limit * 2):
            tmdb_id = _tmdb_guid(item)
            if tmdb_id is None:
                continue
            out.append((tmdb_id, item))
            if len(out) == limit:
                break
        self._top_rated_cache[cache_key] = out
        return out

    def owned_collections(self, label_prefix: str = "shortlist") -> dict[str, OwnedRow]:
        """Map slug -> OwnedRow for every shortlist-owned collection, across every library.

        The PMS is the source of truth for label casing (Plex title-cases new labels) and for
        the collection ids behind a user's rows. A user has one collection per library they get
        picks in, so ids accumulate — collapsing them to a single id once
        hid a real leak: only the last collection was seen while two other
        rows were visible to everyone.
        """
        prefix = f"{label_prefix}_".lower()
        owned: dict[str, OwnedRow] = {}
        for section in self.sections():
            for collection in self._section_collections(section):
                for label in collection.labels:
                    if label.tag.lower().startswith(prefix):
                        slug = label.tag[len(prefix) :].lower()
                        row = owned.setdefault(slug, OwnedRow(label=label.tag))
                        row.rating_keys.append(collection.ratingKey)
        return owned

    def marked_account_ids(self) -> set[int]:
        """Plex account ids that still have a marker-carrying collection on this server.

        A SECOND, INDEPENDENT answer to "does this person's row still exist", deliberately not derived
        from ``collection.labels``. ``owned_collections`` above reads labels, and plex-safety rule 4 is
        explicit that a label re-read which SUCCEEDS carrying no ``<Label>`` is indistinguishable from
        a genuinely unlabelled row — so a prune that removes an exclude on the strength of that one
        read can un-hide a live row (see ``privacy.dead_private``, which now requires both sources to
        agree before it removes anything).

        Independent because the marker travels in the TITLE, which arrives inline in the collections
        listing that a real PMS returns — no per-collection re-read, and therefore not the read that
        can come back silently empty. Costs no extra PMS calls either: ``_section_collections`` is
        cached for the run.

        Not a replacement for the label read: a marked row with no label is exactly the orphan
        ``sweep_broken_rows`` deletes, and only the label is what a ``label!=`` exclude can match on.
        This is here to make the pair disagree loudly rather than to be believed alone.
        """
        accounts: set[int] = set()
        for section in self.sections():
            for collection in self._section_collections(section):
                account = _marker_account(collection.title)
                if account is not None:
                    accounts.add(account)
        return accounts

    def list_owned_collections(self, label_prefix: str = "shortlist") -> list[dict]:
        """Every shortlist-owned collection currently on the server — one entry each (NOT collapsed by
        slug), for a cleanup audit. Read-only and label-based, so it lists rows even for users or rows
        no longer in the database (exactly the drift a cleanup needs to catch). Returns one
        ``{library, title, label, rating_key}`` per collection."""
        prefix = f"{label_prefix}_".lower()
        out: list[dict] = []
        for section in self.sections():
            for collection in self._section_collections(section):
                label = next((lbl.tag for lbl in collection.labels if lbl.tag.lower().startswith(prefix)), None)
                if label is None:
                    continue
                out.append(
                    {
                        "library": section.title,
                        "title": collection.title,  # raw (carries the invisible marker); caller strips it
                        "label": label,
                        "rating_key": collection.ratingKey,
                    }
                )
        return out

    def confirm_unlabelled(self, collection: Collection, label_prefix: str = "shortlist") -> bool:
        """Re-read ONE collection straight from the PMS and report whether it really has no label.

        The guard on the only destructive decision Shortlist makes from a label read. A real PMS does
        NOT return ``<Label>`` children in the section listing — verified on 1.43.3.10861: 103
        collections, zero labels — so ``collection.labels`` is only ever populated because plexapi
        silently re-reads each collection behind the attribute access. That is an implementation
        detail of a third-party library sitting on the path to ``delete()``. A re-read that FAILS raises
        and is caught; the dangerous case is a re-read that SUCCEEDS carrying no ``<Label>``, which is
        indistinguishable from a genuinely unlabelled row.

        Get it wrong in that direction and every Shortlist row on the server is an unlabelled orphan
        — ``delete_owned_collection`` accepts our title marker alone as proof of ownership, so the
        sweep would delete all of them and the run would report success.

        So before deleting, ask the server again, explicitly. Returns True only when a fresh read
        still shows no ``{label_prefix}_*`` label. A failed re-read returns False — refusing to
        delete on an unreadable answer is the whole point.
        """
        try:
            collection.reload()
        except Exception as e:
            logger.warning(
                "{}: could not re-read labels before deleting — leaving it alone ({})",
                log_title(collection.title),
                type(e).__name__,
            )
            return False
        prefix = f"{label_prefix}_".lower()
        return not any(label.tag.lower().startswith(prefix) for label in collection.labels)

    def owned_row_surfaces(self, label_prefix: str = "shortlist", *, flags: bool = True) -> list[dict]:
        """Every Shortlist collection with the surfaces it is CURRENTLY claiming. Read-only.

        The answer to a question nothing in the app could previously ask. ``promotedToOwnHome`` is the
        one surface no share filter can hide — the owner is never restricted (rule 5) — so when
        somebody reports seeing another person's row on their Home screen, these three flags ARE the
        diagnosis. No tool reported them, so the report could not be investigated at all (issue #75).

        A collection counts as ours if it carries our label OR our invisible title marker, and BOTH
        are reported, because the two disagreeing is itself the finding: a marked collection with no
        label is one that no ``label!=`` exclude can hide and that ``sweep_broken_rows`` deletes as an
        orphan (issue #76). Anything with neither is somebody else's and is skipped (rule 4).

        ``flags=False`` skips the surface read entirely, for a caller that only needs to know WHICH
        collections are ours — the flags cost one ``visibility()`` round-trip each (91 of them on a
        real 46-user server), which is worth paying to answer "where is this row showing" and not
        worth paying to count rows.
        """
        prefix = f"{label_prefix}_".lower()
        out: list[dict] = []
        for section in self.sections():
            for collection in self._section_collections(section):
                label = next((lbl.tag for lbl in collection.labels if lbl.tag.lower().startswith(prefix)), None)
                marked = has_shortlist_marker(collection.title)
                if label is None and not marked:
                    continue
                row = {
                    "library": section.title,
                    "library_key": str(section.key),
                    # Marker stripped and the account id it encodes shown instead — two users' rows
                    # are otherwise IMPOSSIBLE to tell apart by eye (see log_title).
                    "title": log_title(collection.title),
                    "label": label or "",
                    "marked": marked,
                    "rating_key": int(collection.ratingKey),
                }
                if flags:
                    try:
                        hub = collection.visibility()
                        row |= {
                            "recommended": bool(getattr(hub, "promotedToRecommended", False)),
                            "own_home": bool(getattr(hub, "promotedToOwnHome", False)),
                            "shared_home": bool(getattr(hub, "promotedToSharedHome", False)),
                        }
                    except Exception as e:
                        # One unreadable hub must not cost the whole walk — this is the tool someone
                        # reaches for when the server is already misbehaving.
                        row["error"] = f"{type(e).__name__}: {e}"
                out.append(row)
        return out

    def matches_section(self, collection: Collection, section: LibrarySection) -> bool:
        """Whether this collection's type matches the library it lives in.

        Plex fixes a collection's subtype from the items it is CREATED with and never revises it,
        so a collection built from shows keeps `subtype="show"` even after its contents are
        swapped for movies. A mismatched collection is matched by neither `filterMovies` nor
        `filterTelevision`, which makes it impossible to hide from anyone — so it must be
        deleted and recreated, never edited in place (SFLIX, 2026-07-12).

        The subtype is conclusive, so it answers on its own: falling through to the items would
        cost a PMS round-trip per user per library, every night, for rows that are already fine.
        The item check is only the fallback for a collection with no subtype at all — and an
        EMPTY one is deliberately treated as matching, because a collection with nothing in it
        shows nobody anything.
        """
        subtype = getattr(collection, "subtype", None)
        if subtype:
            return subtype == section.type
        return all(item.type == section.type for item in collection.items())

    def find_owned_collections(self, section: LibrarySection, wanted_label: str) -> list[Collection]:
        """Every collection in this section carrying `wanted_label` (case-insensitive).

        A user can have several rows, all sharing their label and told apart by title — so delivery
        picks the one with the matching title, and promotion promotes them all.
        """
        wl = wanted_label.lower()
        return [c for c in self._section_collections(section) if any(label.tag.lower() == wl for label in c.labels)]

    def create_collection(self, section: LibrarySection, title: str, items: list) -> Collection:
        collection = self._server.createCollection(title=title, section=section, items=items)
        # Keep the per-section cache WARM: append the new collection rather than wiping the whole
        # cache. Wiping meant every subsequent user re-read the ENTIRE (and growing) section.collections()
        # list to find their own row — O(N^2) PMS reads across a rollout, the dominant delivery cost on a
        # busy server (SFLIX 48-user run, 2026-07-18). Its label is applied next (stored_label reloads
        # this same object in place), so the cached entry becomes correctly labelled.
        cached = self._collections_cache.get(section.key)
        if cached is not None:
            cached.append(collection)
        return collection

    def stored_label(self, collection: Collection, label: str, *, extra: str | None = None) -> str:
        """Ensure `label` is on the collection and return it AS STORED (Plex title-cases it).

        ``extra`` puts a SECOND label on in the same write. Measured on SFLIX 2026-09-06: a label PUT
        costs ~9.3s whatever it carries, and every new row takes two of them (its ``shortlist_<user>``
        and the constant ``shortlist``) — 64 PUTs, 593s, 21% of that run. plexapi's ``editTags``
        concatenates ``existing + items`` and issues ONE ``PUT /library/sections/<key>/all``, so
        passing both spends one write instead of two. Verified against the live PMS, not just the
        source: two labels in one call came back as one PUT with both present.

        ONLY safe on a row we have just created, and the reason is the whole leak this file guards.
        A Plex label write REPLACES the label set; it reads as an append only because plexapi
        re-sends ``collection.labels`` from memory. On an EXISTING row an empty/stale read would
        therefore delete ``shortlist_<user>`` and leave the row visible to every shared account
        (see ``delivery._apply_shortlist_label``, which keeps its own guard for exactly that). A
        freshly created collection has no labels at all, so there is nothing a replace can drop —
        and folding the second write in removes one read-modify-write from the create path rather
        than adding one.

        ``label`` is the one that matters and still raises if it does not persist: it is what other
        accounts' share filters exclude, and the caller deletes the row when this raises. ``extra``
        is cosmetic (Kometa coexistence), so a miss warns and returns normally — the same outcome as
        the separate call it replaces.
        """
        present = {tag.tag.lower(): tag.tag for tag in collection.labels}
        existing = present.get(label.lower())
        wanted = [label] if existing is None else []
        if extra is not None and extra.lower() not in present:
            wanted.append(extra)
        if not wanted:
            return existing  # type: ignore[return-value]

        try:
            # Retried like any other idempotent PMS write: re-sending the same label set is a no-op,
            # and a transient 5xx here makes the CALLER DELETE THE ROW (see
            # `delivery._create_labelled_collection`). Plex answers 500-at-10.0s under load often
            # enough that the self-test for this change needed four attempts to create one
            # collection, so an un-retried label write would bin rows for no reason.
            _retry_idempotent(lambda: collection.addLabel(wanted), label=log_title(collection.title))
        except Exception:
            # Gate on WHICH label failed, never on how many were sent. `wanted` omits whatever is
            # already on the row, so a row that already carries its `shortlist_<user>` and needs
            # only the cosmetic one sends `wanted == [extra]` — and counting would then re-raise,
            # letting `_create_labelled_collection` DELETE a row over a label that is decorative.
            # That is the precise outcome this fallback exists to prevent.
            if label not in wanted:
                logger.warning(
                    "{}: could not add the '{}' label — the row is fine, but a co-managing tool may keep reordering it",
                    log_title(collection.title),
                    extra,
                )
            else:
                if extra is None or len(wanted) == 1:
                    raise
                # Fall back to the critical label alone, so a batched failure is no worse than the
                # two separate writes this replaced: there, `label` succeeding and `extra` failing
                # left the row alive and private.
                logger.warning(
                    "{}: could not write labels {} together — retrying with {!r} alone",
                    log_title(collection.title),
                    wanted,
                    label,
                )
                _retry_idempotent(lambda: collection.addLabel([label]), label=log_title(collection.title))
        collection.reload()
        stored = next((tag.tag for tag in collection.labels if tag.tag.lower() == label.lower()), None)
        if stored is None:
            raise RuntimeError(f"label {label!r} did not persist on collection {collection.title!r}")
        if stored != label:
            logger.debug("Plex stored label {!r} as {!r}", label, stored)
        if extra is not None and not any(t.tag.lower() == extra.lower() for t in collection.labels):
            # Never fatal, and never silent: the row is found, hidden and managed entirely through
            # `label`. All this costs is a co-managing tool reordering this one row.
            logger.warning(
                "{}: the '{}' label did not persist — the row is fine, but a co-managing tool may keep reordering it",
                log_title(collection.title),
                extra,
            )
        return stored

    def promote(
        self,
        collection: Collection,
        *,
        shared: bool = True,
        # Defaults to OFF: `home` is promotedToOwnHome, the SERVER OWNER's Home shelf, and the owner
        # has no share filter — anything landing there is visible to them with nothing able to hide
        # it. A privacy tool must never put a row on that surface by omission; callers say so.
        home: bool = False,
        recommended: bool = True,
        pin_top: bool = False,
    ) -> None:
        """Hide from library browsing but promote onto the chosen surfaces (Home / Library Recommended).

        ``modeUpdate(hide)`` is unconditional — it hides the collection from normal library BROWSE and
        is the leak-safe half of promotion, independent of where the row is shown. ``home``/``shared``/
        ``recommended`` pick the surfaces (a per-row placement). ``pin_top`` moves the managed hub to
        the top of the library's Recommended shelf (server-wide order, not per viewing-user).
        """
        start = time.monotonic()

        def _apply() -> None:
            collection.modeUpdate(mode="hide")
            hub = collection.visibility()
            hub.updateVisibility(recommended=recommended, home=home, shared=shared)
            if pin_top:
                # after=None -> first position in this library's Managed Recommendations.
                hub.reload().move(after=None)

        # Retry the whole promote on a PMS timeout — it's idempotent, and a busy server can time out a
        # single mutation that a retry (with the server given room to breathe) then completes.
        _retry_idempotent(_apply, label=log_title(collection.title))
        logger.info(
            "{}: promoted (home={} library={} pin={}) in {:.1f}s",
            log_title(collection.title),
            home,
            recommended,
            pin_top,
            time.monotonic() - start,
        )

    def reads_as_on_owner_home(self, collection: Collection) -> bool:
        """Is this collection currently on the owner's Home shelf? A read, never a write.

        Split out so a DRY RUN can report the real list. Previewing by title alone would name every
        collection considered rather than the ones actually stranded, and the preview is what an
        operator reads before authorising the live pass.
        """
        return bool(getattr(collection.visibility(), "promotedToOwnHome", False))

    def claims_any_surface(self, collection: Collection) -> bool:
        """Does this collection claim ANY surface right now? A read, never a write.

        The dry-run twin of ``demote_all``. Without it a preview counts every candidate rather than
        the ones that would actually change — so the Tools button offered to "fix" rows that were
        already down (caught on the live server: preview said 2, the live pass corrected 0).
        """
        hub = collection.visibility()
        return any(
            (
                bool(getattr(hub, "promotedToRecommended", False)),
                bool(getattr(hub, "promotedToOwnHome", False)),
                bool(getattr(hub, "promotedToSharedHome", False)),
            )
        )

    def demote_all(self, collection: Collection, *, reason: str = "") -> bool:
        """Take a collection off EVERY surface, leaving it (and its label) in place.

        This is what "pause" means: the person stops seeing their row, but the collection and its
        label survive, so every other account's `label!=` exclude still matches it and unpausing is
        a re-promote rather than a rebuild. Deleting would cost a full LLM re-curation to undo.

        Monotonically private — it only ever removes visibility — so it needs no privacy gate.
        Idempotent: reads first and writes nothing when the collection already claims nothing.
        """
        hub = collection.visibility()
        claims = (
            bool(getattr(hub, "promotedToRecommended", False)),
            bool(getattr(hub, "promotedToOwnHome", False)),
            bool(getattr(hub, "promotedToSharedHome", False)),
        )
        if not any(claims):
            return False
        hub.updateVisibility(recommended=False, home=False, shared=False)
        logger.info("{}: taken off every surface{}", log_title(collection.title), f" ({reason})" if reason else "")
        return True

    def demote_own_home(self, collection: Collection) -> bool:
        """Take a collection off the SERVER OWNER's Home shelf, leaving its other surfaces alone.

        The one convergence write that is always safe: ``promotedToOwnHome`` is the owner's Home, the
        owner has no share filter, and so nothing can hide a row that lands there. Clearing it only
        ever makes the server MORE private, which is why this needs no privacy gate and can run for
        collections the promote phase never reached.

        Idempotent by design — reads the hub first and writes nothing when the flag is already off,
        so a nightly converge over hundreds of collections costs reads, not writes. Returns True only
        when a write actually happened.
        """
        hub = collection.visibility()
        if not getattr(hub, "promotedToOwnHome", False):
            return False
        hub.updateVisibility(
            recommended=bool(getattr(hub, "promotedToRecommended", False)),
            home=False,
            shared=bool(getattr(hub, "promotedToSharedHome", False)),
        )
        logger.info("{}: demoted off the owner's Home (converge)", log_title(collection.title))
        return True

    def order_owned_hubs(
        self,
        section: LibrarySection,
        *,
        label_prefix: str,
        anchor_title: str = "",
        anchor_keys: set[int] | None = None,
        anchor_exclude_keys: set[int] | None = None,
        anchor_label: str = "",
        before: bool = False,
        dry_run: bool = False,
        only_keys: set[int] | None = None,
        exclude_keys: set[int] | None = None,
        pinned_keys: set[int] | None = None,
        to_top: bool = False,
        attempts: int = _HUB_ORDER_ATTEMPTS,
    ) -> dict:
        """Place this section's Shortlist rows in Plex's Managed Recommendations shelf: at the very TOP
        (``to_top``) or right after/before the ``anchor_title`` collection, so a co-managing tool
        (Kometa) can't bury them.

        Only OUR hubs are moved — those labelled ``label_prefix``_* OR carrying the Shortlist title
        marker; a FOREIGN anchor is read-only. ``only_keys`` restricts the move to the rows with those
        collection ratingKeys (used when different rows anchor to different collections) — ``None``
        moves them all.

        An anchor that is a COLLECTION must itself be promoted. One promoted nowhere is in
        ``managedHubs()`` but on no shelf, so it names no position a viewer can see, and following it
        buries the row (issue #106); the row is then left where it is rather than placed somewhere
        nobody asked for. Plex's own built-in hubs are never refused — see the branch for why.

        ``exclude_keys`` is the OTHER way to name a subset, and it exists because enumerating one is
        fragile. It means "every row of ours here EXCEPT these ratingKeys" — used for the rows that
        follow the library default, so they are defined by what they are not, and a row missing from
        the delivery ledger lands in that set instead of being stranded wherever Plex left it. Only
        the rows the owner explicitly placed elsewhere have to be enumerated. Ignored when
        ``only_keys`` is given; both ``None`` means every row of ours, as before.

        ``anchor_keys`` anchors to one of OUR OWN rows instead of a foreign collection: the ratingKeys
        of that row's collections in this section, from the delivery ledger. The anchor hub is then the
        last of them in current shelf order (or the first, for ``before``) rather than a title match —
        a per-person row has one collection per person, so no single title names it. Passing it lifts
        the "never one of ours" rule for exactly those keys, which is safe only because the caller
        places that row's block FIRST; anchoring to a row not yet in position would chase a moving
        target. ``only_keys`` and ``anchor_keys`` must not overlap — a row cannot follow itself.
        ``anchor_label`` is what the audit and logs CALL the anchor, since a row anchor has no title.

        Returns an audit dict ``{anchor, moved: [titles], skipped: bool, reason?}``,
        plus ``verified: bool`` once anything has actually been written (a dry run returns
        ``dry_run: True`` and no ``verified``: it asked for nothing, so there is nothing to confirm).

        Moves ONLY hubs actually out of place, and VERIFIES by re-reading the shelf (SFLIX, 2026-08-12).
        The old loop chained ``move(after=previous)`` over every one of our hubs whenever any single one
        was out of place — 47 unpaced PUTs in 344ms, ~27 of them re-asserting rows already in position —
        and then logged ``moved 47`` having never looked at the result. It counted requests ISSUED.

        That is what made this shelf unreadable: another tool on the same host (agregarr) reorders every
        managed hub every 30 minutes, so the shelf genuinely was not ours, and a function that cannot
        tell "we asked" from "it happened" reported success throughout. Re-reading is what stops us
        claiming a shelf we lost. ``order_collection`` orders items the same way.
        """
        prefix = f"{label_prefix}_".lower()
        # title -> ratingKey for every row of ours here. Titles carry the invisible per-account marker,
        # so within one section they are unique per user; the key is what `only_keys` partitions on.
        #
        # Ours by label OR by title marker. `collection.labels` makes plexapi silently re-read each
        # collection, and a read that comes back carrying no <Label> is indistinguishable from a
        # genuinely unlabelled row (plex-safety rule 4) — which here would empty this map and skip the
        # whole library in silence, the same shape of quiet nothing that hid the ordering bug. Ordering
        # only ever changes a POSITION, so the marker alone is safe proof of ownership: rule 4's two
        # guards exist because a wrong answer there DELETES, and nothing here can.
        key_by_title = {
            c.title: c.ratingKey
            for c in self._section_collections(section)
            if has_shortlist_marker(c.title) or any(label.tag.lower().startswith(prefix) for label in c.labels)
        }
        owned_all = set(key_by_title)
        #: Hub identifiers of EVERY row of ours here, whether or not this call moves them — used to
        #: spot a request for a slot another of our calls already owns.
        owned_idents: set[str] = set()
        #: hub identifier -> ratingKey, for the rows of ours on this shelf.
        key_by_ident: dict[str, int] = {}
        #: True when THIS attempt's read shows a `to_top` request yielding to rows already holding the
        #: top — the audit must not then claim the top for itself. Decided fresh each attempt, because
        #: the shelf is re-read each attempt and the rows it yielded to may since have gone.
        retargeted = False
        # What the audit and the logs CALL this anchor. A row anchor has no title of its own — one
        # collection per person — so without a label every ordering record for one would read
        # 'anchor: ""', which is not an answer to "what moved where" (rule 10).
        audit_anchor = anchor_label or anchor_title or ("another Shortlist row" if anchor_keys else "")
        # The subset to MOVE: enumerated (`only_keys`), everything-but (`exclude_keys`), or all of ours.
        if only_keys is not None:
            owned_titles = {t for t, key in key_by_title.items() if key in only_keys}
        elif exclude_keys:
            owned_titles = {t for t, key in key_by_title.items() if key not in exclude_keys}
        else:
            owned_titles = owned_all
        if not owned_titles:
            return {"anchor": audit_anchor, "moved": [], "skipped": True, "reason": "no rows in this library"}
        # The titles a ROW anchor resolves to here — its collections, one per person. Everything else
        # of ours stays barred from being an anchor: without `anchor_keys` this set is empty and the
        # rule is exactly what it was. A row that named ITSELF would be asked to move relative to its
        # own hubs, which is meaningless and would thrash the shelf; the caller rejects that, and the
        # subtraction here means a slip cannot reach Plex.
        anchor_titles: set[str] = set()
        if anchor_keys:
            anchor_titles = {t for t, key in key_by_title.items() if key in anchor_keys} - owned_titles
        elif anchor_exclude_keys is not None:
            # The anchor row is itself placed BY EXCLUSION (it follows the library default), so its
            # block is every row of ours here minus the hand-placed ones — the same set the catch-all
            # call moves. Naming it by exclusion is what makes this converge: aimed at the anchor
            # row's own hubs instead, the follower lands INSIDE that contiguous block, the next
            # catch-all restores contiguity and evicts it, and neither call ever settles while both
            # report `verified: True`. That is the churn the group-anchoring comment below describes,
            # and it costs one PUT per account per library every run, for ever.
            anchor_titles = {t for t, key in key_by_title.items() if key not in anchor_exclude_keys} - owned_titles
        if (anchor_keys or anchor_exclude_keys is not None) and not anchor_titles:
            # Named row has nothing on this shelf (never delivered here, or its collections are gone).
            # Leaving the shelf alone beats falling back to a different slot: a silent reinterpretation
            # of where someone asked their row to go is worse than not moving it, and the next run
            # places it once the row exists.
            return {"anchor": audit_anchor, "moved": [], "skipped": True, "reason": "anchor row not on this shelf"}

        where = "to the top" if to_top else f"{'before' if before else 'after'} {audit_anchor!r}"
        # Hub identifier -> title, so a row re-moved on a later attempt is audited once, not once per
        # attempt. Titles are unique per section (the marker), the identifier more so.
        moved: dict[str, str] = {}

        def top_anchor() -> str:
            """What a `to_top` call CALLS its landing point. Not always "top": a request that yielded
            to rows already holding the slot landed below them, and every return used to claim the top
            regardless — including the dry-run preview an owner is told to trust.

            One reads `False` by construction rather than by fact: every `outcome()` call site sits
            ABOVE the retarget block in the same attempt, so a give-up that carries writes from an
            earlier attempt reports the top even if that attempt yielded. It is already flagged
            `verified: False`, so the record does not assert the shelf ended that way."""
            return "after the rows already at the top" if retargeted else "top"

        def outcome(reason: str) -> dict:
            """Give up, without discarding the record of writes already made.

            An early exit on attempt 2+ has already moved hubs, and returning ``moved: []`` there put a
            real Plex write outside the audit entirely — `_apply_order` drops skipped results, so
            `report.hub_orderings` never saw it (plex-safety rule 10).
            """
            if not moved:
                return {"anchor": audit_anchor, "moved": [], "skipped": True, "reason": reason}
            return {
                "anchor": top_anchor() if to_top else audit_anchor,
                "moved": list(moved.values()),
                "skipped": False,
                "verified": False,
                "reason": reason,
            }

        # `attempts` write passes, then ONE more read that only verifies. Without that extra pass the
        # final attempt wrote and fell straight through to `verified: False` — so a shelf this fixed on
        # its last try was reported as a failure, which is the very defect this function exists to
        # remove, pointed the other way.
        for attempt in range(1, attempts + 2):
            order = list(section.managedHubs())  # the live shelf order, re-read each attempt
            retargeted = False  # decided fresh from that read, like everything else below
            # Rows promoted NOWHERE are skipped. `managedHubs()` lists every managed hub, promoted or
            # not, so a paused/disabled user's dormant row was being moved into place on every pass —
            # a position nobody can see, since all three promotion flags are off. On SFLIX that was 4
            # wasted moves per library per pass, and it kept a reconciled shelf looking contested: a
            # co-managing tool (agregarr) rightly ignores those rows, so we alone kept shuffling them.
            # PROMOTED only. An unpromoted hub of ours is moved by no call at all (`ours` filters on
            # exactly this), so it contends for nothing — counting it made the guard below fire on the
            # plain single-call path of every ordinary server the moment a paused user's dormant row
            # happened to sit first, and the whole library then stopped being ordered, for ever.
            owned_idents = {
                h.identifier for h in order if (getattr(h, "title", "") or "") in owned_all and is_promoted(h)
            }
            key_by_ident = {
                h.identifier: key_by_title[getattr(h, "title", "") or ""]
                for h in order
                if (getattr(h, "title", "") or "") in key_by_title
            }
            ours = [h for h in order if (getattr(h, "title", "") or "") in owned_titles and is_promoted(h)]
            if not ours:
                return outcome("rows not promoted yet")

            if to_top:
                target = None  # move(after=None) -> the very top of the shelf
            elif anchor_titles:
                # A ROW anchor is a BLOCK of hubs — one collection per person — not a single
                # collection, so it has two edges: sit after its LAST hub, or before its FIRST.
                # Re-resolved every attempt because that block was itself placed moments ago.
                #
                # PROMOTED hubs only, exactly like `ours` above. A paused or disabled person's copy of
                # the anchor row is still on the shelf and still in the ledger, but we never move it —
                # so it sits wherever Plex appended it, at the bottom. Anchoring to it dragged the
                # follower down there with it, underneath the co-managing tool's hubs: precisely the
                # burial this whole function exists to undo, reported as a verified success.
                block = [h for h in order if (getattr(h, "title", "") or "") in anchor_titles and is_promoted(h)]
                if not block:
                    # Every copy of the anchor row here is dormant. Same answer as "not on this shelf":
                    # there is no position to be relative to, and inventing one puts the follower
                    # somewhere nobody asked for.
                    logger.warning(
                        "hub order: anchor row has no promoted hub in {} — {}",
                        section.title,
                        f"stopping after {len(moved)} move(s)" if moved else "leaving the shelf order unchanged",
                    )
                    return outcome("anchor row not on this shelf")
                if before:
                    # Skip only the hubs we are ABOUT TO MOVE: their current position is about to
                    # change, so landing on one aims at a slot that is disappearing. Other rows of ours
                    # are already in place and are legitimate landmarks — which only matters here,
                    # because a row anchor is the one case where our own hubs are the neighbourhood.
                    anchor_idx = order.index(block[0])
                    target = next(
                        (
                            h
                            for h in reversed(order[:anchor_idx])
                            if (getattr(h, "title", "") or "") not in owned_titles
                        ),
                        None,
                    )
                else:
                    target = block[-1]
            else:
                named = [
                    h
                    for h in order
                    if (getattr(h, "title", "") or "") == anchor_title
                    and (getattr(h, "title", "") or "") not in owned_all
                ]

                # Issue #106: `managedHubs()` lists every manageable hub, and a COLLECTION promoted
                # nowhere has no position on the shelf anyone can see. Following one landed our rows
                # wherever Plex keeps it — in practice below every standard Plex hub — and the verify
                # pass then agreed the shelf was exactly what we asked for, so the run reported
                # `verified: True` over a buried row. The picker offered these, because they are real
                # collections in the library.
                #
                # Judged ONLY for collections, and that limit is the point. Plex sends the promotion
                # booleans for a collection — the app reads them on every promote — but a built-in hub
                # ("Recently Added") is an ordinary anchor that nothing in this repo has ever read
                # those flags from, and for which there is no recorded fixture (plex-safety rule 11).
                # Refusing to place a row needs positive evidence that its anchor is off the shelf, and
                # a collection is where we have it; an attribute we cannot vouch for must never be the
                # reason a working placement stops.
                anchor = next((h for h in named if can_anchor(h)), None)
                if anchor is None:
                    logger.warning(
                        "hub order: anchor {!r} {} in {} — {}",
                        anchor_title,
                        "is a collection that is not on any Plex shelf" if named else "not found",
                        section.title,
                        f"stopping after {len(moved)} move(s)" if moved else "leaving the shelf order unchanged",
                    )
                    return outcome("anchor not on the shelf" if named else "anchor not found")
                # 'after anchor' -> the anchor; 'before anchor' -> the hub just before it that this
                # call is not about to move (None -> the very top of the shelf).
                #
                # `owned_titles`, not `owned_all` — the same rule the row-anchor branch states above,
                # and for the same reason: only the hubs we are ABOUT to move are disappearing from
                # their slots. OTHER rows of ours are already in place and are legitimate landmarks.
                # Skipping them too turned "immediately before Letzte Chance" into "the very top" the
                # moment any row of ours sat above the anchor — which then collides with a top-anchored
                # default and either churns for ever or, once the guard below existed, is refused and
                # never placed at all. Landing behind our own rows and in front of the anchor is both
                # what was asked for and stable.
                if before:
                    anchor_idx = order.index(anchor)
                    target = next(
                        (
                            h
                            for h in reversed(order[:anchor_idx])
                            if (getattr(h, "title", "") or "") not in owned_titles
                        ),
                        None,
                    )
                else:
                    target = anchor

            idents = [h.identifier for h in order]
            by_ident = {h.identifier: h for h in order}
            our_idents = [h.identifier for h in ours]

            if to_top and target is None:
                # We have been asked for the VERY TOP. If rows of ours that this call does NOT move
                # are already pinned there, both cannot hold position 0: whoever writes second wins,
                # the other call puts it back next pass, and neither settles — 4 moves a pass, for
                # ever, both reporting `verified: True`.
                #
                # So land immediately after that leading run instead of fighting it. Refusing was the
                # first answer here and it was worse: the refused call is the only one that would ever
                # move those rows, so the setting silently did nothing and they stayed wherever Plex
                # left them — the bottom, for anything newly created. Sharing the top is what both
                # requests actually meant.
                # `to_top` ONLY. `before` also resolves to None when its anchor sits at position 0 —
                # and the anchor's own hubs are ours and not in `our_idents`, so this loop walked
                # straight over them and landed the row AFTER the row it was asked to precede. Stable,
                # and reported `verified: True`, so nothing ever corrected it. Requesting "the very
                # top unconditionally" is the only request that can share the top; "before X" has an
                # answer of its own. (For a foreign `before`, `leading` is provably empty anyway.)
                #
                # And only past hubs ANOTHER CALL THIS RUN is placing there. A row Shortlist positions
                # nowhere is not contending for the slot, so yielding to it is not sharing, it is
                # losing — `pinned_keys` is how the caller says which rows tonight actually claims.
                leading = []
                for ident in idents:
                    if ident in our_idents or ident not in owned_idents:
                        break
                    if pinned_keys is not None and key_by_ident.get(ident) not in pinned_keys:
                        break
                    leading.append(ident)
                if leading:
                    target = by_ident[leading[-1]]
                    retargeted = True
            start = idents.index(target.identifier) + 1 if target is not None else 0
            if idents[start : start + len(our_idents)] == our_idents:
                if not moved:
                    return {"anchor": audit_anchor, "moved": [], "skipped": True, "reason": "already in place"}
                logger.info(
                    "hub order: placed {} row(s) {} in {} — {} move(s) over {} attempt(s), verified",
                    len(ours),
                    where,
                    section.title,
                    len(moved),
                    attempt - 1,
                )
                return {
                    "anchor": top_anchor() if to_top else audit_anchor,
                    "moved": list(moved.values()),
                    "skipped": False,
                    "verified": True,
                }

            if attempt > attempts:
                break  # the extra pass is verify-only: the shelf has just been read and is not right

            # PLAN first, then write — `idents` is our model of the live order, advanced as if each
            # move had landed, so a hub already in its wanted slot is never touched. Planning it
            # separately is what lets the dry run report the REAL cost: it used to say "would move 46
            # rows" for a shelf needing nineteen, which is the same overstatement the live pass made.
            planned: list[tuple[str, object]] = []
            previous = target
            for ident in our_idents:
                want = 0 if previous is None else idents.index(previous.identifier) + 1
                if idents.index(ident) != want:
                    planned.append((ident, previous))
                    idents.remove(ident)
                    idents.insert(0 if previous is None else idents.index(previous.identifier) + 1, ident)
                previous = by_ident[ident]

            if dry_run:
                logger.info(
                    "[dry-run] hub order: would move {} of {} row(s) {} in {}",
                    len(planned),
                    len(ours),
                    where,
                    section.title,
                )
                return {
                    "anchor": top_anchor() if to_top else audit_anchor,
                    "moved": [by_ident[ident].title for ident, _ in planned],
                    "skipped": False,
                    "dry_run": True,
                }

            for ident, after in planned:
                by_ident[ident].reload().move(after=after)  # after=None -> top of the shelf
                moved[ident] = by_ident[ident].title

        # Every attempt moved rows and the shelf still is not what we asked for — and the read that
        # ended the loop confirms that, rather than assuming it. The likeliest reason by far is another
        # tool reordering the same shelf between our passes, so the message says so: an operator who
        # sees this needs to go looking OUTSIDE Shortlist. Cosmetic, so the run carries on, but it is
        # reported as UNVERIFIED rather than as success, which is how this hid for weeks.
        logger.warning(
            "hub order: the Shortlist rows in {} are still not {} after {} attempts and {} move(s) — "
            "something else is very likely reordering this shelf (Kometa, agregarr); leaving it as it is",
            section.title,
            where,
            attempts,
            len(moved),
        )
        return {
            "anchor": top_anchor() if to_top else audit_anchor,
            "moved": list(moved.values()),
            "skipped": False,
            "verified": False,
        }

    def set_items(self, collection: Collection, existing_items: list, add_items: list, wanted_keys: list[int]) -> None:
        """Add/remove to make the collection exactly ``wanted_keys``, and pin it to custom sort — but do
        NOT order it (that's the deferred ``order_collection`` pass).

        The caller passes the ALREADY-FETCHED current membership (``existing_items``) and ONLY the media
        items to add (``add_items``), so this makes ZERO extra PMS reads. It used to re-fetch
        ``collection.items()`` here — a second read of what the caller had just read — and the caller
        fetched ALL wanted items even when only a few changed. On a slow, single-writer PMS those reads
        were the dominant per-user delivery cost, serialized across users (SFLIX, 2026-07-18).

        Ordering (Plex's ``moveItem``, one PMS round-trip per item, no bulk API) is deliberately NOT done
        here: it runs once at the very end via ``order_collection`` — best-effort, so a slow PMS degrades
        the ordering, never the delivery or the leak-safe promotion.
        """
        wanted_set = set(wanted_keys)
        to_remove = [i for i in existing_items if i.ratingKey not in wanted_set]
        if add_items:
            try:
                # Safe to repeat: a Plex collection is a SET, so re-adding an item the first attempt
                # already landed is a no-op, and a partially-applied add converges on the retry.
                _retry_idempotent(lambda: collection.addItems(add_items), label=log_title(collection.title))
            except Exception as exc:
                # Anchored on the leading token, never `"400" in`: plexapi formats the message as
                # `f'({status}) {codename}; {url} {errtext}'` and that url carries the collection's
                # own ratingKey, so a substring test matches keys like 1400 or 40053 — the mistake
                # that once swallowed 500s and 401s in `delivery._rename_or_keep`.
                if not str(exc).startswith("(400)"):
                    raise
                # Raised from HERE, not inferred by the caller, so "the collection refuses items" can
                # never be confused with a 400 from the removeItems or sortUpdate below. Those are
                # different endpoints and a rebuild does not fix them.
                raise CollectionRejectedItems(str(exc)) from exc
        if to_remove:
            # ONE ITEM AT A TIME, each with its own retry, because plexapi's `removeItems` loops and
            # issues a separate DELETE per item. Retrying the whole BATCH would re-send the deletes
            # that already succeeded; this re-sends only the one item that failed. Same PMS
            # round-trip count as before.
            #
            # It is not fully idempotent even so, and saying otherwise would be a lie a future
            # reader relies on: the failure being retried is a 500-after-10.0s, which is Plex
            # timing out on work it may well have applied — so the retry can re-DELETE an item that
            # is already gone, and Plex's answer to that is recorded nowhere (rule 11). A NotFound
            # on a later attempt is therefore treated as the success it describes: the item is not
            # in the collection, which is exactly what this call was asking for.
            for item in to_remove:
                _retry_idempotent(
                    lambda it=item: collection.removeItems([it]),
                    label=log_title(collection.title),
                    already_done=NotFound,
                )
        _retry_idempotent(lambda: collection.sortUpdate(sort="custom"), label=log_title(collection.title))
        # INFO only when the membership actually MOVED. A steady row is the common case on a nightly
        # converge, and "items +0 -0" once per collection buried the lines that mattered — a
        # 96-collection server logged ~96 of them a night saying nothing happened.
        if add_items or to_remove:
            logger.info("{}: items +{} -{}", log_title(collection.title), len(add_items), len(to_remove))
        else:
            logger.debug("{}: items unchanged", log_title(collection.title))

    def order_collection(self, collection: Collection, wanted_keys: list[int]) -> int:
        """Order a collection to ``wanted_keys`` (ranked) via ``moveItem`` — the expensive one-call-per-item
        step, run in a best-effort pass AFTER promotion (never under the delivery write-lock). Moves ONLY
        items actually out of place, so a steady row that barely changed costs a few calls or none.
        Returns the number of moves made.

        The WHOLE row, deliberately uncapped. This ordered only the top 15 until an owner opened a
        30-pick row and found it ranked to halfway and alphabetical after that — and concluded the
        release-date weighting was broken, because the half they could see was the unranked half. The
        cap's premise (it is "the part a viewer sees before see all") holds for the Home shelf and fails
        in the collection itself, which is where someone checking their row actually looks.

        No runaway risk to guard: ``row.size`` and a row's own ``size`` are both validated 5..40, so this
        list is already bounded by a number the owner sets and can see. A second hardcoded cap only meant
        two numbers had to be kept in step while one of them was invisible — set 30, silently get 15.
        Cost is a few minutes on a cold rollout where every collection is new, and seconds once rows are
        in order; this is the last phase, everything is already delivered, hidden and promoted before it
        runs, and the next run re-applies whatever it misses.
        """
        start = time.monotonic()
        collection.reload()
        now = collection.items()
        by_key = {i.ratingKey: i for i in now}
        order = [i.ratingKey for i in now]  # our model of the live order, kept in sync as we move
        target = [k for k in wanted_keys if k in by_key]
        moves = 0
        previous: int | None = None  # the ratingKey the current target item must sit right after
        for key in target:
            want_idx = 0 if previous is None else order.index(previous) + 1
            if order.index(key) != want_idx:
                collection.moveItem(by_key[key], after=(by_key[previous] if previous is not None else None))
                order.remove(key)
                order.insert(0 if previous is None else order.index(previous) + 1, key)
                moves += 1
            previous = key
        if moves:
            logger.info(
                "{}: reordered {}/{} in {:.1f}s",
                log_title(collection.title),
                moves,
                len(target),
                time.monotonic() - start,
            )
        return moves

    def upload_poster(self, collection: Collection, image: bytes) -> None:
        """Set a custom poster on a collection from raw image bytes (cosmetic; our own collection only).

        Callers only ever pass a collection Shortlist owns (just-created + labelled, or found by our
        label), so this doesn't re-check ownership — a freshly created collection's ``labels`` aren't
        yet populated, and reloading purely to re-assert an invariant the caller already holds would
        cost a PMS round-trip per poster.

        plexapi's ``uploadPoster`` reads from a URL or a filepath, so the in-memory bytes are written
        to a temp file, uploaded, then deleted in a ``finally`` (rule 7: no scaffolding left behind on
        the server or the local disk, even if the write or upload fails). Retried like any idempotent
        PMS write.
        """
        # Reserve the path first, then write INSIDE the try, so a failed write still hits the finally.
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(image)
            label = f"uploadPoster {collection.title!r}"
            _retry_idempotent(lambda: collection.uploadPoster(filepath=path), label=label)
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)

    def reset_poster(self, collection: Collection) -> None:
        """Revert a collection to Plex's own artwork after we set a custom poster (cosmetic, best-effort).

        Selects a Plex-provided poster from the collection's options (falling back to the first that
        isn't our upload) and unlocks the field so Plex manages it again. Wrapped best-effort: a revert
        that can't find a default still unlocks, and any failure is swallowed by the caller.
        """

        def _op() -> None:
            with contextlib.suppress(Exception):
                options = collection.posters()
                # Our uploads carry provider "upload"/"local"; prefer anything else (a Plex/agent poster).
                default = next(
                    (p for p in options if (getattr(p, "provider", None) or "") not in ("upload", "local", "")),
                    None,
                )
                if default is not None:
                    collection.setPoster(default)
            collection.unlockPoster()  # hand thumb management back to Plex

        _retry_idempotent(_op, label=f"resetPoster {collection.title!r}")

    def delete_owned_collection(self, collection: Collection, label_prefix: str) -> None:
        """Delete a collection only if it is provably ours (Kometa coexistence, plex-safety rule 4).

        Ownership is proven by EITHER a ``{label_prefix}_*`` label OR Shortlist's invisible 64-char
        title marker. The marker case matters for an ORPHAN — a per-user row whose label write never
        landed after an interrupted run: with no label, no ``label!=`` share filter can hide it, so it
        leaks to every user, and the label-only check used to refuse to clean it up. The marker (a
        64-char zero-width suffix no other tool produces) still identifies it as ours."""
        labelled = any(label.tag.lower().startswith(f"{label_prefix}_") for label in collection.labels)
        if not labelled and not has_shortlist_marker(collection.title):
            raise PermissionError(f"refusing to delete {collection.title!r}: no {label_prefix}_* label — not ours")
        collection.visibility().updateVisibility(recommended=False, home=False, shared=False)
        collection.delete()
        self._invalidate_collections()  # a removed collection changes the section's list

    def fetch_items(self, rating_keys: list[int]) -> tuple[list, list[int]]:
        """``(items, missing)`` for these ratingKeys — what Plex still has, and what has gone.

        Returns BOTH because a caller cannot otherwise tell: a partial batch comes back as a 200 with
        the dead keys simply absent (recorded: ``tests/fixtures/pms_metadata_batch_partial.json``,
        from a real PMS 1.43.3.10793), so the omission is silent. Reporting a row as having delivered
        a title Plex does not hold makes "why isn't X in my row" unanswerable from the audit trail,
        which is the one thing plex-safety rule 10 exists to guarantee.

        Plex 404s only when NOT ONE requested key exists — so a ``NotFound`` here means every one has
        been deleted, and the honest answer is nothing found, everything missing. That case is
        ordinary and must not fail the run: ``to_add_keys`` is the DELTA, so on a steady night whose
        only change was a deletion, the delta IS the dead keys. Live on the maintainer's server
        (2026-08-18, run #17) that raised, taking down one person's whole delivery and the shared row
        while the other 45 were fine.

        An expired token (401 -> Unauthorized), an unreachable server and a 5xx all still raise: none
        is a NotFound, so a real outage can never be mistaken for a tidied library.
        """
        if not rating_keys:
            return [], []
        try:
            items = self._server.fetchItems(rating_keys)
        except NotFound:
            logger.warning(
                "PMS · none of the {} requested ratingKey(s) still exist: {}",
                len(rating_keys),
                ", ".join(str(k) for k in rating_keys[:10]),
            )
            return [], list(rating_keys)
        got = {int(k) for i in items if (k := getattr(i, "ratingKey", None)) is not None}
        missing = [k for k in rating_keys if k not in got]
        if missing:
            logger.warning(
                "PMS · {} of {} ratingKey(s) no longer exist: {}",
                len(missing),
                len(rating_keys),
                ", ".join(str(k) for k in missing[:10]),
            )
        return items, missing

    def user_hubs(self, canary_token: str, path: str = "/hubs") -> list[dict]:
        """Fetch hubs AS another user (for visibility checks). Uses that user's server token, not the owner's."""
        r = http_retry.get(
            self._server.url(path, includeToken=False),
            headers={"X-Plex-Token": canary_token, "Accept": "application/json"},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json().get("MediaContainer", {}).get("Hub", []) or []

    def scrobble_as(self, rating_key: int, token: str, *, dry_run: bool = False) -> bool:
        """Mark one item played AS another account, using that account's server token.

        **Plex stamps this `now` and offers no way to say otherwise.** There is no documented
        endpoint for setting another account's `lastViewedAt`, so a transferred history arrives on
        the PMS dated today no matter what. That is why `WatchedTitle.source_viewed_at` exists —
        Plex gets the checkmark, Shortlist keeps the real date. Never infer a watch DATE from a
        scrobbled item; read the column.

        Returns True when the PMS accepted the write (or would have, under dry run). Returns False —
        rather than raising — when the item is simply not visible to that account, which is the
        normal outcome for a title in a library they were not shared, and must not abort a transfer
        of thousands of others.
        """
        if dry_run:
            logger.info("DRY RUN: would mark ratingKey={} played for the target account", rating_key)
            return True
        r = http_retry.get(
            self._server.url("/:/scrobble", includeToken=False),
            params={"key": str(rating_key), "identifier": "com.plexapp.plugins.library"},
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=self._timeout,
        )
        if r.status_code in (401, 403, 404):
            logger.debug("scrobble skipped for ratingKey={} (HTTP {})", rating_key, r.status_code)
            return False
        r.raise_for_status()
        return True

    def unscrobble_as(self, rating_key: int, token: str, *, dry_run: bool = False) -> bool:
        """Mark one item UNWATCHED as another account — the only call here that removes state.

        Zeroes `viewCount`. Returns False rather than raising when the item is not visible to that
        account (401/403/404), exactly like `scrobble_as`: one unreachable title must not abandon a
        run of thousands.

        It ALSO clears any view offset the item carries, and it is the ONLY call that does —
        `/:/progress?time=0` leaves one exactly where it was (live-probed: 1,139,347 stayed
        1,139,347). An earlier version of this docstring claimed the opposite, and acting on that
        left 293 items part-watched after an undo that reported success.
        """
        if dry_run:
            logger.info("DRY RUN: would mark ratingKey={} UNWATCHED for the target account", rating_key)
            return True
        return self._user_write("/:/unscrobble", {"key": str(rating_key)}, rating_key, token)

    def set_progress_as(self, rating_key: int, offset_ms: int, token: str, *, dry_run: bool = False) -> bool:
        """Set one item's playback position as another account.

        This is the only way a PARTIAL watch can be replicated: `unwatched=0` never returns one, and
        `/:/scrobble` can only say "finished". Live-probed against a real server — the offset comes
        back exactly as sent, with `viewCount` untouched, so a film that is both watched and 8 minutes
        in survives scrobble-then-progress with both facts intact.

        `offset_ms=0` does NOT clear the position — the PMS ignores it. Use `unscrobble_as`, which is
        the only call that clears an offset (and zeroes the view count with it). Measured, twice.
        """
        if dry_run:
            logger.info("DRY RUN: would set ratingKey={} to offset {}ms for the target account", rating_key, offset_ms)
            return True
        return self._user_write(
            "/:/progress",
            {"key": str(rating_key), "time": str(int(offset_ms)), "state": "stopped"},
            rating_key,
            token,
        )

    def _user_write(self, path: str, params: dict[str, str], rating_key: int, token: str) -> bool:
        """One state write as another account. False (not an exception) when they cannot see the item.

        `includeToken=False` keeps the OWNER's token out of the URL — the per-user token goes in the
        header instead (rule 9).
        """
        r = http_retry.get(
            self._server.url(path, includeToken=False),
            params={**params, "identifier": "com.plexapp.plugins.library"},
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=self._timeout,
        )
        if r.status_code in (401, 403, 404):
            logger.debug("{} skipped for ratingKey={} (HTTP {})", path, rating_key, r.status_code)
            return False
        r.raise_for_status()
        return True

    def apply_watch_op(self, op: WriteOp, token: str, *, dry_run: bool = False) -> bool:
        """Apply one planned write. Returns whether the PMS took it.

        A scrobble only ever ADDS one, so `op.scrobbles` — the shortfall the planner worked out
        against a fresh read of the target — is the call count, not `op.view_count`, which is the
        total it should end up at. Using the total would take a film already watched once to four
        rather than three. Probed live: three scrobbles 28 ms apart left `viewCount=3`, so the repeat
        needs no pacing.
        """
        if op.kind is OpKind.MARK:
            ok = True
            for _ in range(max(1, op.scrobbles)):
                ok = self.scrobble_as(op.rating_key, token, dry_run=dry_run) and ok
            return ok
        if op.kind is OpKind.UNMARK:
            return self.unscrobble_as(op.rating_key, token, dry_run=dry_run)
        if op.kind is OpKind.SET_OFFSET:
            return self.set_progress_as(op.rating_key, op.offset_ms, token, dry_run=dry_run)
        # CLEAR_OFFSET is an un-scrobble, NOT `/:/progress?time=0`. Live-probed: `time=0` leaves the
        # offset exactly where it was, so an undo using it left 293 items still part-watched while
        # reporting success. `unscrobble` is the only call that clears one, and the planner accounts
        # for its zeroing the view count too.
        return self.unscrobble_as(op.rating_key, token, dry_run=dry_run)

    def read_watch_state(self, sections: list[tuple[str | int, MediaType]], token: str) -> WatchState:
        """Everything one account has watched or started, across these libraries, read AS that account.

        Four reads per library pair rather than one, because Plex has no single query for "watched or
        in progress": `unwatched=0` filters on `viewCount>0` and so cannot see a partial, while
        `viewOffset>0` cannot see a finished title. Both filters ARE honoured server-side (unlike
        `lastViewedAt>=`, which this PMS silently ignores).

        **Leaves only** — movies and EPISODES, never shows. A show row is state Plex derives, and it
        can disagree with its own episodes: a show-key scrobble leaves it reading 47/47 while the
        show-level query cannot see it at all. Show totals are aggregated from episodes by whoever
        needs them.

        Rating keys are server-scoped, and a transfer moves between two accounts on the SAME server,
        so keys compare directly and no TMDB mapping is involved.

        Args:
            sections: `(section_key, media_type)` pairs to read.
            token: The server-scoped token to read as — never the owner's when reading someone else.

        Returns:
            A `WatchState` keyed by rating key. A library this token cannot see is skipped, not an
            error (`SectionNotShared`).
        """
        items: dict[int, ItemState] = {}
        unreadable: list[str] = []
        for section_key, media_type in sections:
            plex_type = 1 if media_type is MediaType.MOVIE else 4
            kind = "movie" if media_type is MediaType.MOVIE else "episode"
            try:
                for params in ({"unwatched": 0}, {"viewOffset>": 0}):
                    for el in self._leaf_rows(section_key, plex_type, token, params):
                        parsed = self._leaf_state(el, kind)
                        if parsed is None:
                            continue
                        # The two reads overlap on a title that is both watched and in progress. Merge
                        # rather than overwrite: whichever read came second would otherwise erase the
                        # other's field and the item would replicate as half of what it is.
                        prior = items.get(parsed.rating_key)
                        items[parsed.rating_key] = _merge_leaf(prior, parsed) if prior else parsed
            except SectionNotShared:
                # Recorded, never silently swallowed. A caller that mirrors from this state must know
                # it is partial: treating a truncated read as authoritative deletes everything the
                # missing library holds on the other account. See `WatchState.unreadable`.
                logger.warning("watch state: section {} is not shared with this token — read is PARTIAL", section_key)
                unreadable.append(str(section_key))
        return WatchState(items=items, unreadable=tuple(unreadable))

    def _leaf_rows(self, section_key: str | int, plex_type: int, token: str, extra: dict) -> list[ET.Element]:
        """Every row of one filtered leaf read, paged.

        BOTH container headers or nothing — `X-Plex-Container-Size` alone is ignored by this PMS and
        it returns the whole library, the same trap `_history_page` documents.
        """
        out: list[ET.Element] = []
        start = 0
        while True:
            r = http_retry.get(
                self._server.url(f"/library/sections/{section_key}/all", includeToken=False),
                params={"type": plex_type, **extra},
                headers={
                    "X-Plex-Token": token,
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(self._WATCHED_PAGE),
                },
                timeout=self._timeout,
            )
            if r.status_code == 403:
                raise SectionNotShared(f"section {section_key} is not shared with this user")
            r.raise_for_status()
            rows = list(ET.fromstring(r.text))
            out.extend(rows)
            if len(rows) < self._WATCHED_PAGE:
                return out
            start += self._WATCHED_PAGE

    @staticmethod
    def _leaf_state(el: ET.Element, kind: str) -> ItemState | None:
        """One `<Video>` row as an `ItemState`, or None if it carries no usable rating key."""
        try:
            rating_key = int(el.get("ratingKey") or 0)
        except ValueError:
            return None
        if not rating_key:
            return None
        show_key: int | None = None
        # Present on every episode of both show libraries on a real server (9,850 of 9,850), so unlike
        # the history log — which carries only a `grandparentKey` path — this needs no parsing.
        raw_show = el.get("grandparentRatingKey")
        if raw_show and raw_show.isdigit():
            show_key = int(raw_show)
        return ItemState(
            rating_key=rating_key,
            media_type=kind,
            view_count=int(el.get("viewCount") or 0),
            view_offset_ms=int(el.get("viewOffset") or 0),
            last_viewed_at=int(el.get("lastViewedAt") or 0),
            show_rating_key=show_key,
            title=el.get("title") or "",
        )

    # A watched-titles read for one section, paged. Plex defaults to 50 unless X-Plex-Container-Size
    # says otherwise; a heavy watcher has thousands of watched titles, so we page rather than trust a
    # single response to hold them all (a silent cap here would hide older watches from the
    # already-watched filter — the very 200-row bug the share-token read exists to end).
    _WATCHED_PAGE = 500

    # Safety stop for the episode roll-up, which pages until the server returns an EMPTY page rather
    # than trusting a short one (see `_newest_episode_stamps`). At 500 a page this is 100,000 watched
    # episodes — an order of magnitude past the largest library measured (9,563) — so reaching it
    # means the server is not terminating, not that someone watches a lot.
    _EPISODE_PAGE_LIMIT = 200
    #: History pages. Both container headers are required — see `play_history`.
    _HISTORY_PAGE = 1000

    @property
    def token(self) -> str:
        """The admin token, for the callers that must send it themselves (the notification socket)."""
        return self._token

    def notification_socket_url(self) -> str:
        """The PMS notification websocket, WITHOUT the token.

        Token-free on purpose: this URL is passed to a websocket library that puts it in log lines and
        exception messages, and rule 9 says a token never reaches either. The caller sends it as a
        header instead.
        """
        base = self._server._baseurl.rstrip("/")
        scheme = "wss" if base.startswith("https://") else "ws"
        return f"{scheme}://{base.split('://', 1)[1]}/:/websockets/notifications"

    def active_sessions(self) -> dict[str, dict]:
        """What is playing right now, keyed by Plex's `sessionKey`.

        The notification socket carries no user and no runtime — only a session key, a rating key and
        an offset — so this read is what turns an anonymous position update into "this person is 40%
        through this title". `<User id>` here IS the plex.tv account id (verified against a live
        server: 14136324 is the account we hold for that user), which is what makes it joinable where
        a display name would not be.
        """
        r = http_retry.get(
            self._server.url("/status/sessions", includeToken=False),
            headers={"X-Plex-Token": self._token},
            timeout=self._timeout,
        )
        r.raise_for_status()
        out: dict[str, dict] = {}
        for el in ET.fromstring(r.text):
            key = el.get("sessionKey")
            if not key:
                continue
            user = el.find("User")
            grandparent = (el.get("grandparentRatingKey") or "").strip()
            out[key] = {
                "account_id": int(user.get("id")) if user is not None and (user.get("id") or "").isdigit() else None,
                "rating_key": int(el.get("ratingKey") or 0) or None,
                "show_rating_key": int(grandparent) if grandparent.isdigit() else None,
                "media_type": el.get("type") or "",
                # Milliseconds, and the denominator for every percentage we report. Absent on some
                # live items, so it stays optional rather than defaulting to something wrong.
                "duration_ms": int(el.get("duration") or 0) or None,
                "state": (el.find("Player").get("state") if el.find("Player") is not None else "") or "",
            }
        return out

    def play_history(self, *, since: datetime | None = None, limit: int = 20000) -> list[PlayEvent]:
        """Every play the SERVER recorded, newest first, as the admin — one call for all users.

        This is `/status/sessions/history/all`, the durable log Shortlist never read. It is the only
        source of an exact per-play timestamp: the library read exposes `lastViewedAt`, which is the
        LATEST view, so a rewatch erases the date of the first one. It is also self-healing — the log
        lives on the server (101,604 rows reaching back to 2020-10-26 on the maintainer's box), so
        anything missed while Shortlist was down is still there afterwards.

        It records COMPLETIONS only. Probed live: an episode at 73% with no `viewCount` had no entry.
        Starts come from the websocket, never from here.

        Args:
            since: Only plays at or after this moment. Unlike the library read — where `lastViewedAt>=`
                is silently ignored by this PMS build — `viewedAt>` IS honoured here, verified against
                a real server (101,604 rows unfiltered, 2,049 for 30 days, 102 for 24 hours). So the
                incremental read is genuinely incremental rather than a sort-and-stop.
            limit: Stop after this many events. A backstop for a first read with no cursor, which
                would otherwise pull six years of history in one go.

        Returns:
            Newest first, so a caller that hits `limit` keeps the RECENT end rather than 2020's.
        """
        out: list[PlayEvent] = []
        start = 0
        while start < limit:
            params: dict[str, object] = {"sort": "viewedAt:desc"}
            if since is not None:
                params["viewedAt>"] = int(since.timestamp())
            root = self._history_page(params, start)
            page = [event for el in root if (event := self._play_event(el)) is not None]
            out.extend(page)
            # `size` is what this page returned; a short page is the end. `totalSize` is the whole
            # filtered set and is only present when the container headers are sent.
            if len(list(root)) < self._HISTORY_PAGE:
                break
            start += self._HISTORY_PAGE
        return out[:limit]

    def _history_page(self, params: dict[str, object], start: int) -> ET.Element:
        """One page of the play history.

        BOTH container headers or nothing: `X-Plex-Container-Size` alone is IGNORED by this PMS and
        the server returns the entire log — 101,604 rows, ~40 MB, on a request that asked for 1,000.
        Live-probed 2026-08-23. Sending Start as well makes it honour both and fill in `totalSize`.
        """
        r = http_retry.get(
            self._server.url("/status/sessions/history/all", includeToken=False),
            params=params,
            headers={
                "X-Plex-Token": self._token,
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(self._HISTORY_PAGE),
            },
            timeout=self._timeout,
        )
        r.raise_for_status()
        return ET.fromstring(r.text)

    @staticmethod
    def _play_event(el: ET.Element) -> PlayEvent | None:
        """One `<Video>` history row, or None if it carries nothing we can attribute.

        A history row is thin — `accountID`, `ratingKey`, `viewedAt`, `type`, and for an episode the
        show's `grandparentKey`. There is no duration, no viewOffset and no TMDB guid, so this cannot
        say how much was watched and does not pretend to.
        """
        try:
            account_id = int(el.get("accountID") or 0)
            rating_key = int(el.get("ratingKey") or 0)
            viewed_at = int(el.get("viewedAt") or 0)
        except ValueError:
            return None
        if not account_id or not rating_key or not viewed_at:
            return None
        # The SHOW's key, dug out of `/library/metadata/592373` — history entries carry no
        # `grandparentRatingKey` attribute, only the path. This is the field that actually matches a
        # pick: a series pick stores the show's key while history reports the episode played, and over
        # 30 days of real history 46 of 78 matches were reachable only this way.
        show_key: int | None = None
        tail = (el.get("grandparentKey") or "").rsplit("/", 1)[-1]
        if tail.isdigit():
            show_key = int(tail)
        return PlayEvent(
            plex_account_id=account_id,
            rating_key=rating_key,
            show_rating_key=show_key,
            media_type=el.get("type") or "",
            viewed_at=datetime.fromtimestamp(viewed_at, tz=UTC),
            history_key=el.get("historyKey") or None,
        )

    def watched_titles(
        self,
        section_key: str | int,
        media_type: MediaType,
        token: str,
        *,
        since: datetime | None = None,
    ) -> list[WatchedItem]:
        """Every title in one library this user has watched, read from the PMS AS that user.

        The filter DIFFERS by media type, and the difference is issue #108:

        * **Movies** — ``unwatched=0``, i.e. ``viewCount>0``. Plex's own binary watched flag, which
          INCLUDES a mark-as-watched (the playback-history API never returns marks; issue #12).
        * **Shows** — ``viewedLeafCount!=0``, NEVER ``unwatched=0``. The latter filters on the show's
          own watch-state row, which marking a series or a season does not establish, so a finished
          series is absent from it while its episode counts are correct. Measured on two servers:
          533 shows against 491, the 491 matching the episode-level truth exactly. The filter is
          applied again client-side, because an ignored query param on this endpoint is answered with
          the whole library rather than an error.

        ``includeGuids=1`` inlines each item's ``tmdb://`` GUID, so a title resolves to its tmdb_id here
        with no dependency on the run's library index — the same on a sync as on a run (live-verified
        2026-07-24: 100% of a friend's watched movies carried an inline TMDB GUID).

        A show is returned once, at the show level, carrying the user's own ``viewedLeafCount`` /
        ``leafCount`` — so the finished-show fraction is Plex's, not a reconstruction from play counts,
        and a bulk-marked season is counted correctly. Movies carry ``viewCount`` as ``watch_count``.

        Args:
            section_key: The library section key to read.
            media_type: Which type the section holds — selects Plex's ``type`` (1=movie, 2=show).
            token: The server-scoped ``X-Plex-Token`` to read as (this user's, not the owner's) — a
                live per-user credential, never logged (rule 9).
            since: Return only titles last viewed at or after this moment — an INCREMENTAL read.

                Done by ORDERING, not filtering: the read is sorted ``lastViewedAt:desc`` and stops at
                the first title older than the cutoff. A `lastViewedAt>=` query filter was tried first
                and is **silently ignored** by PMS 1.43.3 (live-probed 2026-07-30 against SFLIX:
                unfiltered, `>=` and `>>=` all returned the same totalSize of 1077 — as did a `year>>=`
                control, so param filtering on this endpoint does not work at all). Ignoring a filter
                is the worst failure mode available: it returns everything while looking like it
                worked. Sorting IS honoured, so the cutoff is applied client-side against an order the
                server guarantees.

                Still a partial answer by construction — it cannot see a title that was un-watched or
                deleted — so callers must keep doing a periodic full read. ``None`` (the default) reads
                everything, unsorted, exactly as before.

        Returns:
            A `WatchedRead`: one WatchedItem per distinct watched title (newest watch first is NOT
            guaranteed — callers sort; titles with no ``tmdb://`` GUID are dropped, they can never
            match a candidate), plus `covers_window`, which says whether this read can be trusted to
            have returned EVERYTHING at or after ``since``. See `WatchedRead` — a caller that deletes
            on absence must check it.
        """
        plex_type = 1 if media_type is MediaType.MOVIE else 2
        # Captured BEFORE the walk: the sort fallback below sets `since = None` mid-read, so by the
        # end `since is None` no longer distinguishes "asked for everything" from "asked for a window
        # and gave up on the sort". Only the first earns the complete-read coverage rule.
        full_read = since is None
        items: list[WatchedItem] = []
        # Rows Plex DID return that we then threw away for want of a `tmdb://` guid. Counted because
        # the drop is otherwise invisible: a title the person really has watched simply never reaches
        # the cache, the log says "5 titles" rather than "5 of 8", and nothing anywhere says why. A
        # library matched with the legacy TheTVDB agent yields `tvdb://` only, so this is a whole
        # library's worth of silence, not a stray row.
        dropped = 0
        # Rows the SERVER should have filtered out and did not. A handful is normal; a library's
        # worth means `viewedLeafCount!=0` was ignored and we are paging everything, which the
        # client-side filter hides completely — the answer stays right, the cost silently multiplies.
        filtered_out = 0
        start = 0
        reached_cutoff = False
        read_whole_library = False
        # Only ever set False. The cutoff stop is sound only while the server sorts newest-first, and
        # the fallback below abandons the sort MID-WALK — the pages already read stay in whatever
        # order they arrived, so one failure taints the whole read, not just the page that failed.
        sort_honoured = True
        # `_is_newest_first` is vacuously true on a page carrying fewer than two comparable stamps,
        # so "the sort didn't visibly break" is not the same as "the sort was observed working". The
        # cutoff stop trusts the ORDER, so it may only prove coverage once the order has actually
        # been seen holding. Reaching a reported total needs no such evidence — it counts rows.
        order_observed = False
        while True:
            root = self._read_watched_page(section_key, plex_type, token, start, since=since)
            entries = list(root)
            # Validate the ORDER before trusting the early stop, not while walking it. The stop is
            # only sound while the server honours `sort=lastViewedAt:desc` — and `lastViewedAt>=` was
            # also documented as supported and is silently ignored by this PMS, so the sort earns the
            # same suspicion. Checked up front because an ascending page puts its OLDEST row first:
            # detecting mid-walk is too late, the stop has already fired and truncated the read to
            # something that looks exactly like a quiet night.
            if since is not None and not _is_newest_first(entries):
                logger.warning(
                    "PMS returned watched items out of order for section {} — the incremental sort "
                    "was not honoured, so this read falls back to a complete one",
                    section_key,
                )
                since = None
                sort_honoured = False
            elif since is not None and len(_stamps(entries)) >= 2:
                order_observed = True
            for el in entries:
                # Apply the filter OURSELVES rather than trusting the server to have applied it.
                # Query-param filtering on this endpoint is silently ignored by some responses — a
                # 200 carrying the FULL library, which is the worst failure available here: every
                # show in the library would read as watched and nothing would ever be recommended
                # again. Filtering client-side makes the answer correct either way; an honoured
                # filter just means fewer rows crossed the wire.
                if media_type is MediaType.SHOW and int(el.get("viewedLeafCount") or 0) <= 0:
                    filtered_out += 1
                    continue
                item = self._watched_item(el, media_type)
                if item is None:
                    # Only on a complete read. An incremental walk stops at a cutoff, so a row it
                    # skipped may simply be outside the window — counting it would report a match
                    # problem that isn't one. Every sync reads complete now, so nothing is lost.
                    dropped += full_read
                    continue
                if since is not None and item.watched_at < since:
                    # An item with NO `lastViewedAt` is stamped 1970 by `_watched_item`, so it looks
                    # older than any cutoff. Treating that as "everything after this is older" would
                    # end the walk on a data gap and silently drop every title behind it — the read
                    # would look like a quiet night rather than a truncation. Skip it and keep going;
                    # only a real timestamp may end the walk.
                    if el.get("lastViewedAt") is None:
                        # Returned, not just stepped over. The `continue` only has to stop the walk
                        # ENDING here; dropping the row as well made an incremental read report a
                        # show the full read had dated from its episodes as absent, which is
                        # indistinguishable from an un-watch and let `_drop_vanished_since` delete it.
                        # `viewedLeafCount!=0` already proved it watched, so returning it is right.
                        items.append(item)
                        continue
                    # Sorted newest-first, so everything from here on is older. Stop reading — this
                    # is where the saving actually comes from, since the server ignores the filter.
                    reached_cutoff = True
                    break
                items.append(item)
            # NEVER fall back to `size`: on a paged response that is the size of THIS PAGE, so a
            # server omitting `totalSize` made `total` equal the page length and the walk stopped
            # after one page — a partial watched set reported as complete, which is exactly the
            # already-watched-titles-recommended bug this paging exists to prevent. `totalSize` is
            # now an upper bound only; a SHORT page is what proves the end.
            reported_total = root.get("totalSize")
            total = int(reported_total) if reported_total is not None else None
            start += len(entries)
            # Two ways to know we are done, and which one applies depends on whether the server told
            # us a total:
            #
            # * `totalSize` present -> trust it. A server may legitimately return fewer rows per page
            #   than we asked for, so a short page does NOT mean the end when a bigger total is known.
            # * `totalSize` absent  -> a short page is the only end-signal available. Falling back to
            #   `size` (the PAGE size) made `total` equal the page length and stopped the walk after
            #   one page — a partial watched set reported as complete.
            #
            # An empty page always stops us, so a server ignoring the paging headers cannot loop.
            done = start >= total if total is not None else len(entries) < self._WATCHED_PAGE
            if total is not None and done:
                # The server told us a total and we reached it, so the library was read end to end.
                # A short page WITHOUT a total proves nothing — see `covers_window` below.
                read_whole_library = True
            if reached_cutoff or not entries or done:
                break
            if total is None:
                logger.warning(
                    "PMS gave no totalSize for section {} — paging on short-page detection alone",
                    section_key,
                )
        # Did this read definitely return everything at or after `since`?
        #
        # This is the difference between an under-read and DELETED WATCH HISTORY. The cache drops
        # cached titles the read did not return, so a caller that assumes coverage turns every
        # truncated walk into "they un-watched all of it". Two ways to have earned the claim:
        #
        # * `reached_cutoff` + `order_observed` — we saw a real timestamp older than the cutoff, so
        #   everything newer had already been emitted. Paired with `order_observed` because that stop
        #   is only as good as the sort, and a page with fewer than two comparable stamps passes the
        #   order check without demonstrating anything;
        # * `read_whole_library` — the server gave a `totalSize` and we walked to it, so there was
        #   nothing left to miss.
        #
        # Everything else is unproven, and the unproven cases are real: a server that omits
        # `totalSize` AND caps the container below our page size ends the walk on a short page having
        # read only part of the window. `sort_honoured` gates both, because the fallback drops the
        # sort mid-walk and leaves the earlier pages in an order nothing verified.
        #
        # A COMPLETE read (`since is None`) has no cutoff to stop at, so `read_whole_library` is the
        # only proof available — and it is the one the cache's reconcile pass checks before replacing
        # a section wholesale.
        covers_window = (
            read_whole_library
            if full_read
            else (sort_honoured and ((reached_cutoff and order_observed) or read_whole_library))
        )
        # `warned` is per instance, created lazily: `tests/conftest.py`'s `mock_plex` builds this
        # class via `__new__` and never runs `__init__`, and a mutable CLASS attribute would leak one
        # test's suppressions into the next. A client lives for one sync or one run, which is the
        # right lifetime for "already said once".
        warned = self.__dict__.setdefault("_warned_unmatched", set())
        if filtered_out > len(items) and filtered_out > 10 and str(section_key) not in warned:
            # More thrown away than kept: the server is not applying `viewedLeafCount!=0`, so every
            # sync pages the whole library instead of the watched part of it. Correctness is
            # unaffected — the filter is applied here too — but on a real server this is 4,880 rows
            # against 491, per person, per sync, with nothing else to show for it.
            warned.add(str(section_key))
            logger.warning(
                "watched read: section {} — the server returned {} shows with nothing watched "
                "alongside {} watched ones, so it is ignoring `viewedLeafCount!=0` and this read is "
                "paging the whole library every time. Correct, but far more expensive than it needs "
                "to be.",
                section_key,
                filtered_out,
                len(items),
            )
        if dropped and str(section_key) not in warned:
            # ONCE per library per client, not once per person. What it reports is a property of the
            # LIBRARY — its metadata agent — and it is invariant across users and across nights,
            # while this method runs per (person, library). Unguarded it produced one identical line
            # per user per sync: ~100 a day on a 47-user server, which is how a warning becomes
            # wallpaper. The per-read count stays on the DEBUG line above for anyone counting.
            #
            # WARNING, not debug: every one of these is a title the person has watched that Shortlist
            # will keep recommending back to them, and the only cure is fixing the match in Plex.
            warned.add(str(section_key))
            agent = "TheTVDB" if media_type is MediaType.SHOW else "the legacy Movie"
            logger.warning(
                "watched read: section {} — Plex returned {} watched title(s) with no tmdb:// guid, "
                "so they cannot be matched and are ignored. A library matched with {} does this to "
                "EVERY title; re-matching it against Plex's own agent fixes it.",
                section_key,
                dropped,
                agent,
            )
        logger.debug(
            "watched read: section {} ({}) -> {} titles{}{}{}",
            section_key,
            media_type.value,
            len(items),
            f" ({dropped} dropped — no tmdb:// guid)" if dropped else "",
            f" since {since.isoformat()}" if since else "",
            "" if covers_window else " (INCOMPLETE — coverage unproven)",
        )
        if media_type is MediaType.SHOW and full_read:
            items = self._dates_from_episodes(section_key, token, items)
        return WatchedRead(items=items, covers_window=covers_window, dropped_no_guid=dropped)

    def _dates_from_episodes(self, section_key: str | int, token: str, shows: list[WatchedItem]) -> list[WatchedItem]:
        """Give a show with no watch date of its own the date of its newest watched EPISODE.

        Marking a series or a season watched sets the episodes and leaves the show row with no
        `lastViewedAt` — the same omission behind issue #108. `_watched_item` has to date such a row
        1970, which is not a small inaccuracy: `watched_at` drives seed recency (a 1970 date weighs
        zero, so the show never seeds) and the effectiveness report, which showed a series finished
        minutes ago as "finished 20697d ago".

        Only runs when at least one show came back undated, and reads once for the whole library
        rather than per show. On a real server 19 of 491 shows were undated, of which the episodes
        could date 2 — the rest have no watched episodes either, so nothing anywhere knows when, and
        they keep the epoch. That is the honest answer rather than a guess.
        """
        undated = [item for item in shows if item.rating_key is not None and item.watched_at <= _EPOCH]
        if not undated:
            return shows
        try:
            newest = self._newest_episode_stamps(section_key, token)
        except Exception as e:
            # Dates only — never worth losing a good read over. Degrades to the epoch, which is what
            # these rows carried before this existed.
            logger.warning(
                "watched read: section {} — could not read episodes to date {} undated show(s) ({})",
                section_key,
                len(undated),
                type(e).__name__,
            )
            return shows
        if newest is None:
            # Coverage unproven — see `_newest_episode_stamps`. Dating from an arbitrary prefix of an
            # unordered list would put a wrong date on a real row, which is worse than none: the
            # epoch at least says "unknown" and weighs zero, while a plausible-looking wrong date
            # silently mis-weights the seed and the effectiveness report.
            return shows

        dated = 0
        out = []
        for item in shows:
            stamp = newest.get(item.rating_key or -1, 0) if item.watched_at <= _EPOCH else 0
            if stamp:
                dated += 1
                out.append(replace(item, watched_at=datetime.fromtimestamp(stamp, tz=UTC)))
            else:
                out.append(item)
        logger.debug(
            "watched read: section {} — dated {} of {} undated show(s) from their episodes",
            section_key,
            dated,
            len(undated),
        )
        return out

    #: Above this many shows needing a date, ONE library-wide episode read is cheaper than a call
    #: each. The library walk is ~2.8s over 9,563 episodes; a single show is one page of at most a few
    #: dozen rows, so ~0.1-0.2s — the crossover is a dozen or so, not the ~3 the 1.1s SECTION read
    #: would suggest (that read returns every show in the library, which is a different question).
    #: Marking a handful of shows is the normal case, so the per-show path is the one that runs.
    _PER_SHOW_DATE_LIMIT = 12

    def newest_episode_dates(self, section_key: str | int, token: str, show_keys: set[int]) -> dict[int, datetime]:
        """When each of these shows was last WATCHED, taken from its episodes.

        For a show whose own `lastViewedAt` is stale — marking a partly-watched series bumps
        `viewedLeafCount` and leaves the show's date alone (issue #108) — the episodes are the only
        place the real date exists. The caller decides which shows need it; this just answers.

        Args:
            section_key: The library, used only to pick the bulk read when there are many.
            token: The user's own server token — these are per-user watch states.
            show_keys: The shows to date. Empty returns empty without a request.

        Returns:
            `{show ratingKey: newest watched-episode datetime}`, omitting any show nothing could date.
        """
        if not show_keys:
            return {}
        if len(show_keys) > self._PER_SHOW_DATE_LIMIT:
            stamps = self._newest_episode_stamps(section_key, token)
            if stamps is None:
                return {}
            return {k: datetime.fromtimestamp(v, tz=UTC) for k, v in stamps.items() if k in show_keys and v > 0}
        out: dict[int, datetime] = {}
        for key in sorted(show_keys):
            try:
                stamp = self._newest_leaf_stamp(key, token)
            except Exception as e:
                # Dates only — one show that will not read must not cost the other libraries their
                # sync. It keeps the date it had, which is the pre-repair behaviour.
                logger.warning("watched read: could not date show {} from its episodes ({})", key, type(e).__name__)
                continue
            if stamp:
                out[key] = datetime.fromtimestamp(stamp, tz=UTC)
        return out

    def _newest_leaf_stamp(self, show_rating_key: int, token: str) -> int:
        """Newest `lastViewedAt` across one show's WATCHED episodes, or 0 if none are.

        Two things this endpoint does that the section read does not, both live-probed 2026-09-05 and
        recorded in `pms_all_leaves.xml.txt`:

        * ``?unwatched=0`` is **silently ignored** here — the filtered and unfiltered answers were
          byte-for-byte the same 8 rows. So the filter is applied client-side, on ``viewCount``.
        * ``totalSize`` is omitted entirely unless an explicit container size is asked for (One Piece
          answered `size=1175` with no total, and `size=50 totalSize=1175` when paged). So the page
          headers are always sent — without them a 1,175-episode show is one 3.6MB response.

        A partially-watched episode carries a `lastViewedAt` and NO `viewCount` (recorded: episode 2
        of the fixture, `viewOffset` only). It is excluded, because it is equally excluded from the
        `viewedLeafCount` this repair exists to explain — counting it would date a show from an
        episode nobody finished.
        """
        newest = 0
        start = 0
        for _ in range(self._EPISODE_PAGE_LIMIT):
            url = self._server.url(f"/library/metadata/{show_rating_key}/allLeaves", includeToken=False)
            r = http_retry.get(
                url,
                headers={
                    "X-Plex-Token": token,
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(self._WATCHED_PAGE),
                },
                timeout=self._timeout,
            )
            if r.status_code == 403:
                raise SectionNotShared(f"show {show_rating_key} is not visible to this user")
            r.raise_for_status()
            root = ET.fromstring(r.text)
            page = list(root)
            for el in page:
                try:
                    views = int(el.get("viewCount") or 0)
                    stamp = int(el.get("lastViewedAt") or 0)
                except ValueError:
                    continue
                if views > 0:
                    newest = max(newest, stamp)  # unwatched and part-watched rows are both excluded
            if not page:
                return newest
            start += len(page)
            reported = root.get("totalSize")
            if reported is not None and start >= int(reported):
                return newest
        logger.warning("watched read: show {} did not finish paging its episodes", show_rating_key)
        return newest

    def _newest_episode_stamps(self, section_key: str | int, token: str) -> dict[int, int] | None:
        """`{show ratingKey: newest watched episode lastViewedAt}` for one show library, read as `token`.

        Folded per page rather than accumulating elements. The library this exists for holds 9,563
        watched episodes (recorded: `pms_watched_episodes_rollup.xml.txt`) — retaining them was 14MB
        held per user per library per sync to extract a handful of integers, while the dict is a few
        hundred ints.

        Returns:
            The stamps, or **None** when the walk could not be proven complete — see below. Never a
            partial answer: the episode list is not ordered by `lastViewedAt`, so a truncated read is
            an arbitrary subset and every date taken from it is arbitrarily too old.
        """
        newest: dict[int, int] = {}
        start = 0
        # A server that reports no `totalSize` AND caps the container below what we asked for
        # answers every page short, so "short page" cannot mean "the end" — that read stops after one
        # page and dates every show from the first 2% of an unordered list. Page until the server
        # returns an EMPTY page instead, which costs one extra request and cannot be misread. The
        # bound is a safety stop against a server that never empties, not an expected exit.
        for _ in range(self._EPISODE_PAGE_LIMIT):
            url = self._server.url(f"/library/sections/{section_key}/all", includeToken=False)
            # `?` or `&`: plexapi appends `?X-Plex-Token=...` to `url()` even with `includeToken=False`
            # whenever `log.show_secrets` is on, and a hardcoded `?` then made `type=4` part of the
            # token value rather than a parameter — answered with the whole library, not its episodes.
            r = http_retry.get(
                f"{url}{'&' if '?' in url else '?'}type=4&unwatched=0",
                headers={
                    "X-Plex-Token": token,
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(self._WATCHED_PAGE),
                },
                timeout=self._timeout,
            )
            if r.status_code == 403:
                raise SectionNotShared(f"section {section_key} is not shared with this user")
            r.raise_for_status()
            root = ET.fromstring(r.text)
            page = list(root)
            for el in page:
                raw = el.get("grandparentRatingKey")
                if raw is None:
                    continue  # a season or show row — only leaves carry the key we fold on
                try:
                    key, stamp = int(raw), int(el.get("lastViewedAt") or 0)
                    views = int(el.get("viewCount") or 0)
                except ValueError:
                    continue
                # `viewCount`, client-side, exactly as `_newest_leaf_stamp` does — so the two paths
                # that date a show cannot disagree about the same show. A part-watched episode
                # carries a `lastViewedAt` and no `viewCount`, and on the first real show this was
                # tried against its stamp was NEWER than the only episode actually finished. This
                # server's `unwatched=0` does exclude those (probed 2026-09-05: episode 460770,
                # viewOffset-only, absent from all 9,581 rows) — but the same endpoint family
                # silently ignores `viewedLeafCount!=0` and `lastViewedAt>=`, so depending on the
                # server to filter is the assumption that keeps being wrong here.
                if views <= 0:
                    continue
                if stamp > newest.get(key, 0):
                    newest[key] = stamp
            if not page:
                return newest
            start += len(page)
            reported = root.get("totalSize")
            if reported is not None and start >= int(reported):
                return newest
        logger.warning(
            "watched read: section {} — episode read did not terminate in {} pages, dates left unknown",
            section_key,
            self._EPISODE_PAGE_LIMIT,
        )
        return None

    def _read_watched_page(
        self,
        section_key: str | int,
        plex_type: int,
        token: str,
        start: int,
        *,
        since: datetime | None = None,
    ) -> ET.Element:
        """One page of a section's watched titles as XML, read as ``token``. Retries transient reads."""
        # Query params rather than plexapi: we need the raw per-user response as a specific token, and
        # includeGuids inlines the TMDB id so no library index is consulted. includeToken=False keeps
        # the OWNER's token out of the URL — we set the per-user token in the header instead (rule 9).
        url = self._server.url(f"/library/sections/{section_key}/all", includeToken=False)
        params: dict[str, object] = {"type": plex_type, "includeGuids": 1}
        show_filter = ""
        if plex_type == 2:  # a show library
            # `viewedLeafCount!=0`, NOT `unwatched=0`. The latter filters on the show's own
            # watch-state row, which marking a series or a season never establishes — so a series
            # someone has finished is absent from it while its episode counts are perfectly correct.
            # Measured on two independent servers: `unwatched=0` returned 533 shows where
            # `viewedLeafCount!=0` returned 491, matching the episode-level truth
            # (`?type=4&unwatched=0` rolled up by show) EXACTLY, in both directions, and 20 shows with
            # watched episodes were missing from `unwatched=0` altogether. Issue #108; the reporter
            # found this filter independently.
            #
            # No new trust: `viewedLeafCount`/`leafCount` already drive the finished-show fraction
            # everywhere else, so believing them about PRESENCE adds no assumption.
            #
            # APPENDED to the URL, not passed through `params`, because Plex's filter OPERATOR lives
            # in the key and httpx percent-encodes a key: `params={"viewedLeafCount!": 0}` goes on the
            # wire as `viewedLeafCount%21=0`. This server decodes that and answers 491 either way
            # (measured), but plexapi's own `joinArgs` encodes only the VALUE for exactly this reason,
            # and a server that did not decode it would fall back to the whole library — 4,880 rows
            # instead of 491, per person, per library, per sync. Verified: the literal form
            # round-trips intact and returns the same 491.
            show_filter = "viewedLeafCount!=0"
        else:
            params["unwatched"] = 0
        if since is not None:
            # SORT, not filter. `lastViewedAt>=` (and `>>=`) are silently ignored by PMS 1.43.3 —
            # live-probed 2026-07-30, see `watched_titles`. Sorting newest-first IS honoured, and the
            # caller stops at the first title older than the cutoff.
            params["sort"] = "lastViewedAt:desc"
        # The query is assembled here rather than handed to httpx as `params`, because httpx REPLACES
        # a URL's existing query with `params` — so the show filter has to travel with the rest, and
        # it has to keep its literal `!` (see above).
        query = str(httpx.QueryParams(params))
        if show_filter:
            query = f"{query}&{show_filter}"
        r = http_retry.get(
            f"{url}{'&' if '?' in url else '?'}{query}",
            headers={
                "X-Plex-Token": token,
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(self._WATCHED_PAGE),
            },
            timeout=self._timeout,
        )
        # 403 here is the PMS saying this token cannot see this library — an unshared library, not a
        # broken read. It has to be a distinct signal: treated as a generic failure it invalidated the
        # WHOLE person's watch cache on every sync, forcing an uncached complete re-read of every
        # library for ever (SFLIX: two users, hourly, silently). See `SectionNotShared`.
        if r.status_code == 403:
            raise SectionNotShared(f"section {section_key} is not shared with this user")
        r.raise_for_status()
        return ET.fromstring(r.text)

    @staticmethod
    def _watched_item(el: ET.Element, media_type: MediaType) -> WatchedItem | None:
        """Build a WatchedItem from one ``<Video>``/``<Directory>`` element, or None if it has no TMDB id."""
        tmdb_id: int | None = None
        for guid in el.iter("Guid"):
            gid = guid.get("id") or ""
            if gid.startswith("tmdb://"):
                try:
                    tmdb_id = int(gid.removeprefix("tmdb://"))
                except ValueError:
                    # A malformed guid must not raise out of the whole watched-titles read — treat
                    # it the same as no guid at all (see `_tmdb_guid`'s tolerance for the same case).
                    continue
                break
        if tmdb_id is None:
            return None
        last_viewed = el.get("lastViewedAt")
        watched_at = datetime.fromtimestamp(int(last_viewed), tz=UTC) if last_viewed else _EPOCH
        year = el.get("year")
        # `userRating` belongs to the TOKEN this page was read with, not to the server — live-probed
        # 2026-08-06 across 50 accounts on a real server: a title reading 6.2 as the owner came back
        # with no `userRating` at all for all 49 viewers. So it needs no extra request and cannot leak
        # one person's opinion into another's row. Absent on all but ~0.3% of watched rows.
        user_rating = _float_or_none(el.get("userRating"))
        if media_type is MediaType.MOVIE:
            return WatchedItem(
                title=el.get("title") or "",
                media_type=MediaType.MOVIE,
                watched_at=watched_at,
                tmdb_id=tmdb_id,
                year=int(year) if year else None,
                rating_key=int(el.get("ratingKey")) if el.get("ratingKey") else None,
                watch_count=int(el.get("viewCount") or 1),
                user_rating=user_rating,
            )
        viewed_leaf = el.get("viewedLeafCount")
        leaf = el.get("leafCount")
        viewed_leaf_count = int(viewed_leaf) if viewed_leaf else 0
        return WatchedItem(
            title=el.get("title") or "",
            media_type=MediaType.SHOW,
            watched_at=watched_at,
            tmdb_id=tmdb_id,
            year=int(year) if year else None,
            rating_key=int(el.get("ratingKey")) if el.get("ratingKey") else None,
            # Episodes watched is the frequency signal for a show — a 50-episode binge weighs like 50
            # movie plays (see WatchedItem.watch_count). Floor at 1: unwatched=0 guarantees >0 viewed.
            watch_count=max(1, viewed_leaf_count),
            viewed_leaf_count=viewed_leaf_count,
            leaf_count=int(leaf) if leaf else None,
            user_rating=user_rating,
        )
