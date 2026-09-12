"""In-memory fakes of a Plex Media Server and plex.tv, served over real HTTP.

``make_fake_plex(state)`` builds a FastAPI app speaking just enough of the PMS wire protocol
(XML MediaContainer responses) for plexapi 4.x and the engine's raw ``/hubs`` calls to work
unmodified. ``make_fake_plextv(state)`` builds the plex.tv surface (``/api/users`` XML,
``/api/v2/*`` JSON) the engine's ``PlexTvClient`` talks to. Both share one ``FakePlexState``,
so tests can assert on server-side effects directly.

Fidelity notes (mirrors of real-Plex behavior the engine depends on):
- New labels are stored title-cased (``shortlist_x`` -> ``Shortlist_x``), like a real PMS.
- ``/hubs`` respects the requesting token: ``server-<accountID>`` tokens see only collections
  promoted to shared Home whose labels are NOT in that user's ``label!=`` share-filter excludes.
- plex.tv Home-user switch mints ``switch-<id>``; ``/api/v2/resources`` exchanges it for the
  server-scoped ``server-<id>`` token (the T2 canary mechanism).
"""

from __future__ import annotations

import base64
import io
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

FILTER_FIELDS = ("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos")
_LABEL_PARAM = re.compile(r"^label\[\d+\]\.tag\.tag$")
_SORT_KEYS = {
    "addedAt": lambda m: m.added_at,
    "audienceRating": lambda m: m.audience_rating,
    "titleSort": lambda m: m.title,
}


@dataclass
class FakeMovie:
    """A library item. `media_type` decides how the PMS serves it (<Video> vs <Directory>).

    Which library it lives in is decided by the FakeSection that holds it — NOT by this field. The
    same title can exist in two libraries ("Movies" and "4K Movies") under different ratingKeys.
    """

    rating_key: int
    title: str
    year: int
    added_at: int  # epoch seconds
    tmdb_id: int
    audience_rating: float
    media_type: str = "movie"  # "movie" | "show" | "episode"
    leaf_count: int = 0  # total episodes (shows only); the share-token watched read serves it as leafCount
    #: Episodes only — the SHOW this belongs to. A real PMS puts it on every episode row of a section
    #: read (9,850 of 9,850 on a live server), unlike the history log, which carries only a path.
    grandparent_rating_key: int | None = None
    parent_index: int = 0  # season number
    index: int = 0  # episode number within the season


@dataclass
class FakeSection:
    """One Plex library: its own key, type, title and items.

    A server can have SEVERAL libraries of one type — "Movies" + "4K Movies" is a very common
    layout. A fake that can only model one library per type cannot reproduce it, and that blind
    spot hid two production bugs: a row delivered to a non-lowest-keyed library was never promoted
    (so it stayed visible in library browse to everyone), and a row pinned to one library was
    curated against the union of all of them.
    """

    key: int
    type: str  # "movie" | "show"
    title: str
    items: dict[int, FakeMovie] = field(default_factory=dict)


def _default_sections() -> dict[int, FakeSection]:
    """The one-library-per-type layout every existing test assumes: Movies(1), TV Shows(2)."""
    return {
        1: FakeSection(key=1, type="movie", title="Movies"),
        2: FakeSection(key=2, type="show", title="TV Shows"),
    }


@dataclass
class FakeCollection:
    rating_key: int
    title: str
    section_id: int
    labels: list[str] = field(default_factory=list)  # stored casing, like the PMS keeps them
    item_keys: list[int] = field(default_factory=list)  # ordered item rating keys
    # Plex fixes a collection's subtype at CREATION from the items it is created with, and never
    # revises it — swapping the contents later does not re-type it. That stickiness is why a
    # mistyped collection cannot be repaired in place: it has to be deleted and recreated.
    subtype: str = "movie"
    mode: int = -1  # -1 default / 0 hide (plexapi collectionMode enum)
    sort: int = 0  # 0 release / 1 alpha / 2 custom
    promoted_recommended: bool = False
    promoted_own_home: bool = False
    promoted_shared_home: bool = False


@dataclass
class FakeUser:
    id: int
    username: str
    home: bool = False
    restricted: bool | None = None
    # The Plex parental preset ("little_kid"|"older_kid"|"teen", "" for none). Only /api/home/users
    # reports it, and it is what decides whether Plex accepts a label restriction at all (#20).
    restriction_profile: str = ""
    protected: bool = False
    uuid: str = ""
    filters: dict[str, str] = field(default_factory=lambda: dict.fromkeys(FILTER_FIELDS, ""))

    def __post_init__(self) -> None:
        # DERIVED, not a free field: plex.tv reports `restricted="1"` for EVERY Plex Home account,
        # preset or not (that is the whole reason #20 needed `restrictionProfile` to tell them apart).
        # Left independent, this fake could serve a home account as `restricted="0"` — a shape plex.tv
        # cannot produce, and one that silently disarms the skip in `privacy.py`, which is gated on
        # BOTH flags. A test built that way writes filters for an account Plex would 422.
        if self.restricted is None:
            self.restricted = self.home


@dataclass
class FakeHistoryEntry:
    account_id: int
    rating_key: int
    viewed_at: int  # epoch seconds


@dataclass
class FakePlexState:
    """Shared in-memory truth for both fake servers; tests assert on it directly."""

    machine_id: str = "fake-machine-1"
    #: What the wizard screenshots show as the server's name, so it has to read like a name somebody
    #: would actually give their server. It was "FakePlex", which told every visitor to the docs site
    #: that the picture was staged — on the one screen whose job is "this is what setup looks like".
    friendly_name: str = "Home Server"
    version: str = "1.43.3.10793"
    owner_token: str = "owner-token"
    owner_account_id: int = 555000001  # the owner's plex.tv id
    owner_username: str = "steve"
    # PMS keeps its own account table and files the OWNER's watch history under a local id, not
    # their plex.tv one — so `accountID=<owner_account_id>` matches nothing. Shared users are listed
    # under their plex.tv id, which is why only the owner needs resolving.
    owner_pms_account_id: int = 1
    pms_url: str = "http://127.0.0.1:32400"  # set by the harness once the fake PMS has a port
    sections: dict[int, FakeSection] = field(default_factory=_default_sections)
    collections: dict[int, FakeCollection] = field(default_factory=dict)
    #: Managed-hub order per section — identifiers, Plex's own hubs and our collections in ONE list,
    #: because that is what `GET /hubs/sections/{id}/manage` returns and what `.../move` reorders.
    #: Shelf order used to be modelled as the insertion order of `collections`, which meant a
    #: built-in could not be moved or moved past, so the shipped default placement (a row above
    #: `movie.recentlyadded`) was unrealisable and the engine burned its retries against it.
    #: Populated lazily by `_shelf`, which appends collections it has not seen — new hubs go to the
    #: BOTTOM, as on a real server.
    hub_order: dict[int, list[str]] = field(default_factory=dict)
    users: dict[int, FakeUser] = field(default_factory=dict)  # owner is NOT in this dict
    history: list[FakeHistoryEntry] = field(default_factory=list)
    #: Per-account LEAF watch state — `{account_id: {rating_key: [view_count, view_offset_ms]}}`.
    #:
    #: Deliberately separate from `history`, because the real server keeps them separate too: a
    #: scrobble changes this and writes NOTHING to `/status/sessions/history/all` (probed live, 31
    #: scrobbles, log still empty). A fake that filed scrobbles as history would prove the opposite of
    #: what the server does, and the whole watching-account transfer rests on the distinction.
    leaf_state: dict[int, dict[int, list[int]]] = field(default_factory=dict)
    # What each account rated each title, {(account_id, rating_key): 0..10}. Keyed by ACCOUNT because
    # that is how Plex really scopes it: live-probed across 50 accounts on a real server, a title
    # reading 6.2 for the owner carried no `userRating` at all for any of the 49 viewers. A fake that
    # served one rating to everybody would make the leak this keying prevents untestable.
    user_ratings: dict[tuple[int, int], float] = field(default_factory=dict)
    # How many episodes of a SHOW one account has watched, {(account_id, rating_key): viewedLeafCount}.
    # Absent = the seeded default of "watched means finished".
    #
    # Without this the fake could only ever serve a show as fully watched, and a partly-watched
    # series is not a corner case: on a real 47-account server (measured 2026-08-16) only 21 of 158
    # credited show picks had been finished, and 31 were a single episode. Every test of the
    # watched-vs-finished split would otherwise run against the one shape the split does not have to
    # distinguish. "The fake must be no easier than the real server" (.claude/rules/testing.md).
    partial_shows: dict[tuple[int, int], int] = field(default_factory=dict)
    next_rating_key: int = 5000

    def rate(self, account_id: int, rating_key: int, rating: float) -> None:
        """Record that one account rated one title — what tapping the stars in Plex does."""
        self.user_ratings[(account_id, rating_key)] = rating

    def watch_episodes(self, account_id: int, rating_key: int, viewed: int) -> None:
        """This account has watched `viewed` of the show's episodes — a series in progress.

        Plex reports a show through `viewedLeafCount`/`leafCount` and has no show-level watched flag,
        so this is the only way the state exists at all.
        """
        self.partial_shows[(account_id, rating_key)] = viewed

    @property
    def section_id(self) -> int:
        """The default (lowest-keyed) movie library — what a one-library-per-type test means."""
        return self.default_section("movie").key

    @property
    def show_section_id(self) -> int:
        return self.default_section("show").key

    @property
    def movies(self) -> dict[int, FakeMovie]:
        """Items of the DEFAULT movie library. A test with several movie libraries must address
        them through `sections`, since "the movie library" is no longer a single thing."""
        return self.default_section("movie").items

    @property
    def shows(self) -> dict[int, FakeMovie]:
        return self.default_section("show").items

    def default_section(self, kind: str) -> FakeSection:
        """The lowest-keyed library of a type — the one `sections_by_type()` picks."""
        return min((s for s in self.sections.values() if s.type == kind), key=lambda s: s.key)

    def add_section(self, key: int, kind: str, title: str) -> FakeSection:
        """Add a library. A second library of an existing type is the point: see FakeSection."""
        section = FakeSection(key=key, type=kind, title=title)
        self.sections[key] = section
        return section

    def sections_of(self, kind: str) -> list[FakeSection]:
        return sorted((s for s in self.sections.values() if s.type == kind), key=lambda s: s.key)

    def new_rating_key(self) -> int:
        self.next_rating_key += 1
        return self.next_rating_key

    def item(self, rating_key: int) -> FakeMovie | None:
        for section in self.sections.values():
            if rating_key in section.items:
                return section.items[rating_key]
        return None

    def section_of(self, rating_key: int) -> FakeSection | None:
        """Which library holds this item. RatingKeys are server-unique, so at most one does."""
        return next((s for s in self.sections.values() if rating_key in s.items), None)

    def section_type(self, section_id: int) -> str:
        return self.sections[section_id].type

    def items_in(self, section_id: int) -> dict[int, FakeMovie]:
        return self.sections[section_id].items

    def titles_of_type(self, kind: str) -> list[FakeMovie]:
        """Every distinct TMDB title of a type, across every library of that type.

        Deduped by tmdb_id: the same film in "Movies" and "4K Movies" is ONE title as far as
        anything upstream of Plex (TMDB, the curator) is concerned.
        """
        by_tmdb: dict[int, FakeMovie] = {}
        for section in self.sections_of(kind):
            for item in section.items.values():
                by_tmdb.setdefault(item.tmdb_id, item)
        return list(by_tmdb.values())

    def members(self, collection: FakeCollection) -> list[int]:
        """The items a collection actually contains, per Plex's real model.

        A Plex collection is a TAG on items, keyed by TITLE within a library — not an independent
        bag with its own membership. So two collections with the same title in the same library
        are ONE membership: each returns the union of both. Verified on a live server (SFLIX,
        2026-07-13): a film picked for one user alone appeared in another user's row, carrying a
        single collection tag.

        This is why every user's row must have a title no other row in that library uses. Modelling
        collections as independent objects is exactly what let the bug ship.
        """
        keys: list[int] = []
        for other in self.collections.values():
            if other.section_id != collection.section_id or other.title != collection.title:
                continue
            for key in other.item_keys:
                if key not in keys:
                    keys.append(key)
        return keys

    def filterable(self, collection: FakeCollection) -> bool:
        """Whether a real PMS could hide this collection with a `label!=` share filter.

        Share filters are applied per library: `filterMovies` to the movie libraries,
        `filterTelevision` to the TV ones. A collection whose SUBTYPE doesn't match the library
        it sits in (e.g. a show-subtype collection inside a movie library) is matched by NEITHER
        filter, so its label exclude does nothing and it stays visible to every user. That is not
        a hypothetical: it is exactly how two users' rows ended up on everyone's Home screen on a
        live server (SFLIX, 2026-07-12).

        Subtype is sticky — see FakeCollection.subtype — so swapping in items of the right type
        does NOT make a mistyped collection filterable again.
        """
        return collection.subtype == self.section_type(collection.section_id)

    @staticmethod
    def store_label(label: str) -> str:
        """Title-case a new label exactly like a real PMS does (``shortlist_x`` -> ``Shortlist_x``)."""
        return label[:1].upper() + label[1:] if label else label

    def user_for_token(self, token: str) -> FakeUser | None:
        """Resolve a server-scoped ``server-<accountID>`` token; None for unknown tokens."""
        if token.startswith("server-") and token.removeprefix("server-").isdigit():
            return self.users.get(int(token.removeprefix("server-")))
        return None

    def watched_account_id(self, token: str) -> int | None:
        """Which PMS account a share-token watched read (``unwatched=0``) speaks for.

        The share-token read is served AS the token's owner, so the watched set is theirs. The owner
        reads with the admin token and their history is filed under the local ``owner_pms_account_id``
        (never their plex.tv id — see the class notes); a shared/Home user reads with a
        ``server-<accountID>`` token filed under that same plex.tv id.
        """
        if token == self.owner_token:
            return self.owner_pms_account_id
        user = self.user_for_token(token)
        return user.id if user else None

    def watched_keys(self, account_id: int) -> set[int]:
        """The rating keys this account has watched (movies and shows), from ``history``."""
        return {h.rating_key for h in self.history if h.account_id == account_id}

    def last_viewed_at(self, account_id: int, rating_key: int) -> int:
        """The most recent watch time for one title, or 0 if never watched by this account."""
        times = [h.viewed_at for h in self.history if h.account_id == account_id and h.rating_key == rating_key]
        return max(times, default=0)

    def leaf(self, account_id: int, rating_key: int) -> list[int]:
        """`[view_count, view_offset_ms]` for one account and one leaf, created empty on first touch."""
        return self.leaf_state.setdefault(account_id, {}).setdefault(rating_key, [0, 0])

    def episodes_of(self, show_rating_key: int) -> list[FakeMovie]:
        for section in self.sections.values():
            found = [i for i in section.items.values() if i.grandparent_rating_key == show_rating_key]
            if found:
                return sorted(found, key=lambda i: (i.parent_index, i.index))
        return []

    def scrobble(self, account_id: int, rating_key: int) -> bool:
        """Mark one key played, exactly as the PMS does — INCREMENTING, never setting.

        A SHOW key marks every episode. That is the real behaviour and it is the bug the transfer
        exists to stop reintroducing, so the fake reproduces it rather than refusing: write a show key
        here and the e2e will show all ten episodes watched, which is what a live server does.
        """
        item = self.item(rating_key)
        if item is None:
            return False
        targets = self.episodes_of(rating_key) if item.media_type == "show" else [item]
        for leaf in targets or [item]:
            entry = self.leaf(account_id, leaf.rating_key)
            entry[0] += 1
            # A scrobble CLEARS an offset the item already carries — probed live, 480,000 read back
            # as 0 after one scrobble. Leaving it here would make the fake easier than the server and
            # hide a title losing its position every time its count is topped up.
            entry[1] = 0
        return True

    def unscrobble(self, account_id: int, rating_key: int) -> bool:
        """Clear a key. Zeroes the view count AND the offset — measured, and load-bearing.

        It is the only call that clears an offset, which is why the planner treats clearing one as a
        full reset of the item rather than a surgical edit.
        """
        item = self.item(rating_key)
        if item is None:
            return False
        targets = self.episodes_of(rating_key) if item.media_type == "show" else [item]
        for leaf in targets or [item]:
            self.leaf(account_id, leaf.rating_key)[:] = [0, 0]
        return True

    def set_progress(self, account_id: int, rating_key: int, offset_ms: int) -> bool:
        """Set a playback position — and IGNORE `time=0`, exactly as a real server does.

        Live-probed 2026-08-25: `/:/progress?time=0` left an offset of 1,139,347 untouched. Only
        `/:/unscrobble` clears one. The fake used to honour `time=0`, which made an undo look like it
        worked here while leaving 293 items part-watched on a real account — a fake easier than the
        server, which is the one thing it may never be.
        """
        if self.item(rating_key) is None:
            return False
        if offset_ms <= 0:
            return True
        self.leaf(account_id, rating_key)[1] = offset_ms
        return True

    def leaf_view(self, account_id: int, rating_key: int) -> tuple[int, int]:
        """`(view_count, view_offset_ms)` without creating an entry — for the read path.

        Falls back to `history` when this account has no explicit leaf entry, so the fake has ONE
        watch-state model rather than two that can disagree. A real PMS serves the same `viewCount`
        to every read of a title; a fake where the share-token read said "watched" and the leaf read
        said "not watched" would let a real inconsistency through as a passing suite.

        An explicit entry always wins, including an explicit zero — that is what an un-scrobble
        leaves behind, and it has to be able to override seeded history.
        """
        entry = self.leaf_state.get(account_id, {}).get(rating_key)
        if entry is not None:
            return entry[0], entry[1]
        plays = sum(1 for h in self.history if h.account_id == account_id and h.rating_key == rating_key)
        return plays, 0

    #: Show keys a real PMS would OMIT from `?type=2&unwatched=0` despite their episodes being
    #: watched — the issue #108 shape. Marking a series or a season watched sets the episodes without
    #: establishing the show-level watch-state row the query filters on, and 20 of 491 shows on a real
    #: server were in exactly this state. Without it the fake answers the show-level read from the
    #: same set as the episode read, the two can never disagree, and the whole recovery path is dead
    #: code in every full-stack test — the "fake must be no easier than the real server" rule.
    invisible_to_show_read: set[int] = field(default_factory=set)

    #: Show ratingKeys the show read RETURNS but with **no** `lastViewedAt` — the mark-as-watched
    #: shape. A real PMS sets the show's own watch-state row only when the show itself was played;
    #: marking a series or a season leaves `viewedLeafCount` correct and the date absent, and 19 of
    #: 492 shows on a real server were in exactly this state. Without it `_movie_xml` dates every
    #: watched show, no show is ever undated, and `_dates_from_episodes` — the whole reason the
    #: episode roll-up exists — is dead code in every full-stack test.
    undated_in_show_read: set[int] = field(default_factory=set)

    def watched_now(self, account_id: int) -> set[int]:
        """Every key this account currently counts as watched, from BOTH sources.

        `watched_keys` reads history alone and cannot see a scrobble (which writes no history row on
        a real server, and none here either), so a read filtered on it would report a freshly
        replicated account as empty.
        """
        keys = {h.rating_key for h in self.history if h.account_id == account_id}
        keys |= {k for k, v in self.leaf_state.get(account_id, {}).items() if v[0] > 0}
        return {k for k in keys if self.leaf_view(account_id, k)[0] > 0}

    @staticmethod
    def excluded_labels(user: FakeUser) -> set[str]:
        """Lowercased ``label!=`` values across the user's movie/TV share filters."""
        excludes: set[str] = set()
        for fieldname in ("filterMovies", "filterTelevision"):
            for condition in (user.filters.get(fieldname) or "").split("|"):
                if condition.startswith("label!="):
                    excludes.update(v.lower() for v in condition.removeprefix("label!=").split(",") if v)
        return excludes


#: The demo library the docs screenshots are taken against. Real titles, because every one of
#: them sits beside its own poster in a published image, and a placeholder name under real cover
#: art reads as a mock-up — the owner's verdict on the drawn version was "it looks a bit odd".
#:
#: The ARTWORK is not in this repo. `scripts/fetch_demo_posters.py` downloads it from TMDB into
#: `tests/e2e/assets/posters/` (gitignored) and `_fake_poster` serves it when present, falling
#: back to a drawn placeholder — so an ordinary test run still needs no network and no key. Only
#: the finished screenshots are committed, which is the posture the hero image already has.
#:
#: Index order is load-bearing: item N keeps rating_key 100+N (movies) or 300+N (shows), so every
#: fixture, seeded watch and recorded expectation that addresses an item by KEY is untouched by
#: what it is called. Tests that need a name should ask `movie_title()` / `show_title()` rather
#: than hardcoding one.
DEMO_MOVIES: tuple[tuple[str, int], ...] = (
    ("The Shawshank Redemption", 1994),
    ("The Godfather", 1972),
    ("The Dark Knight", 2008),
    ("Pulp Fiction", 1994),
    ("Inception", 2010),
    ("Interstellar", 2014),
    ("The Matrix", 1999),
    ("GoodFellas", 1990),
    ("Se7en", 1995),
    ("Fight Club", 1999),
    ("Forrest Gump", 1994),
    ("Gladiator", 2000),
    ("The Departed", 2006),
    ("Whiplash", 2014),
    ("Parasite", 2019),
    ("Mad Max: Fury Road", 2015),
    ("Blade Runner 2049", 2017),
    ("Arrival", 2016),
    ("Dune", 2021),
    ("Heat", 1995),
    ("No Country for Old Men", 2007),
    ("There Will Be Blood", 2007),
    ("The Prestige", 2006),
    ("Casino Royale", 2006),
    ("Sicario", 2015),
    ("Prisoners", 2013),
    ("Nightcrawler", 2014),
    ("Ex Machina", 2015),
    ("Her", 2013),
    ("Drive", 2011),
)

DEMO_SHOWS: tuple[tuple[str, int], ...] = (
    ("Breaking Bad", 2008),
    ("The Sopranos", 1999),
    ("The Wire", 2002),
    ("Chernobyl", 2019),
    ("Band of Brothers", 2001),
    ("True Detective", 2014),
    ("Better Call Saul", 2015),
    ("Succession", 2018),
    ("Severance", 2022),
    ("The Bear", 2022),
    ("Fargo", 2014),
    ("MINDHUNTER", 2017),
    ("Dark Matter", 2024),
    ("Stranger Things", 2016),
    ("The Last of Us", 2023),
    ("Andor", 2022),
    ("The Expanse", 2015),
    ("Peaky Blinders", 2013),
    ("Sherlock", 2010),
    ("Black Mirror", 2011),
    ("Ted Lasso", 2020),
    ("The Crown", 2016),
    ("Ozark", 2017),
    ("Narcos", 2015),
    ("Westworld", 2016),
    ("House of the Dragon", 2022),
    ("Yellowstone", 2018),
    ("Slow Horses", 2022),
    ("Shōgun", 2024),
    ("The Boys", 2019),
)


def movie_title(index: int) -> str:
    """The demo library's Nth film, 1-based — the title on rating_key ``100 + index``."""
    return DEMO_MOVIES[index - 1][0]


def show_title(index: int) -> str:
    """The demo library's Nth show, 1-based — the title on rating_key ``300 + index``."""
    return DEMO_SHOWS[index - 1][0]


def seed_state() -> FakePlexState:
    """Two libraries (30 movies, 30 shows), 3 users (one Home user without a PIN), history.

    The TV library is not decoration: a server with only movies cannot exhibit the class of bug
    where a show is delivered into a movie collection, so every test would pass while the real
    thing leaked.
    """
    state = FakePlexState()
    base_added = 1_700_000_000
    for i in range(1, 31):
        title, year = DEMO_MOVIES[i - 1]
        state.movies[100 + i] = FakeMovie(
            rating_key=100 + i,
            title=title,
            year=year,
            added_at=base_added + i * 86_400,
            tmdb_id=9000 + i,
            audience_rating=5.0 + (i * 7) % 40 / 10,
        )
    # 30 shows, like the movie library: a TV catalog barely bigger than what a user has already
    # watched starves the candidate pool and makes row sizes a property of the fixture, not the
    # engine.
    for i in range(1, 31):
        title, year = DEMO_SHOWS[i - 1]
        state.shows[300 + i] = FakeMovie(
            rating_key=300 + i,
            title=title,
            year=year,
            added_at=base_added + i * 86_400,
            tmdb_id=7000 + i,
            audience_rating=5.0 + (i * 3) % 40 / 10,
            media_type="show",
            leaf_count=10,  # 10 episodes; a seeded "watched" show is served fully watched (finished)
        )
    state.users[201] = FakeUser(id=201, username="sarah")
    state.users[202] = FakeUser(id=202, username="mike")
    state.users[203] = FakeUser(id=203, username="jess", home=True, uuid="uuid-203")
    # A managed account with a parental preset. Plex refuses a label filter for one, so Shortlist
    # writes it no excludes — and this account can still SEE collections, which is the whole point:
    # `little_kid` sees none, `older_kid` sees them (measured on a real server, 2026-08-11, #76).
    # The listing above serves every collection to every token, which for an account carrying no
    # excludes is exactly what a real PMS does, so this models the case faithfully.
    state.users[204] = FakeUser(id=204, username="kid", home=True, uuid="uuid-204", restriction_profile="older_kid")
    base_viewed = 1_752_000_000
    # One run then covers the whole delivery matrix: sarah watches both types (two rows), mike
    # watches only TV (one row, in the TV library), the jess has no history (cold start).
    watched = {
        201: list(range(101, 109)) + list(range(301, 305)),
        202: list(range(305, 313)),
        # The owner, under the id PMS files THEM under — never their plex.tv id. A test that seeded
        # this under `owner_account_id` would pass while the real server returned nothing.
        state.owner_pms_account_id: list(range(109, 117)) + list(range(313, 317)),
    }
    for account, keys in watched.items():
        for offset, key in enumerate(keys):
            state.history.append(FakeHistoryEntry(account_id=account, rating_key=key, viewed_at=base_viewed + offset))
    return state


#: A valid 1x1 PNG, for the artwork endpoint. Real bytes rather than a placeholder string so the
#: proxy's content-type passthrough and the browser's `<img>` both behave as they would live.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _xml(root: Element) -> Response:
    return Response(content=tostring(root, encoding="unicode"), media_type="text/xml")


def _el(parent: Element, name: str, /, **attrs) -> Element:
    child = SubElement(parent, name)
    for key, value in attrs.items():
        child.set(key, str(value))
    return child


def _container(**attrs) -> Element:
    root = Element("MediaContainer")
    for key, value in attrs.items():
        root.set(key, str(value))
    return root


def _leaf_xml(parent: Element, item: FakeMovie, view_count: int, view_offset: int) -> Element:
    """One `<Video>` row of a LEAF read, shaped like the recorded fixtures.

    `viewCount` and `viewOffset` are OMITTED when zero, exactly as a real server omits them — the
    absent-vs-zero distinction is load-bearing: 72 films on a live account carried an offset and no
    `viewCount` at all, and reading that as "watched once" would mark every one of them finished.
    """
    element = SubElement(parent, "Video")
    element.set("ratingKey", str(item.rating_key))
    element.set("key", f"/library/metadata/{item.rating_key}")
    element.set("type", item.media_type)
    element.set("title", item.title)
    element.set("duration", "3600000")
    if item.grandparent_rating_key is not None:
        element.set("grandparentRatingKey", str(item.grandparent_rating_key))
        element.set("grandparentKey", f"/library/metadata/{item.grandparent_rating_key}")
        element.set("parentIndex", str(item.parent_index))
        element.set("index", str(item.index))
    if view_count:
        element.set("viewCount", str(view_count))
        element.set("lastViewedAt", str(item.added_at))
    if view_offset:
        element.set("viewOffset", str(view_offset))
    return element


def _movie_xml(parent: Element, state: FakePlexState, movie: FakeMovie, *, watched_by: int | None = None) -> Element:
    """One library item. Plex serves movies as <Video> and shows as <Directory>.

    When ``watched_by`` is set, the element also carries that account's per-user watched counts the way
    a real ``unwatched=0`` read does: ``viewCount``/``lastViewedAt`` for a movie, and
    ``viewedLeafCount``/``leafCount`` for a show (the share-token watched read reads exactly these).
    """
    is_show = movie.media_type == "show"
    section = state.section_of(movie.rating_key)
    element = _el(
        parent,
        "Directory" if is_show else "Video",
        ratingKey=movie.rating_key,
        key=f"/library/metadata/{movie.rating_key}" + ("/children" if is_show else ""),
        type=movie.media_type,
        title=movie.title,
        year=movie.year,
        addedAt=movie.added_at,
        audienceRating=movie.audience_rating,
        # Artwork, in the shape a real PMS serves it — a server-relative path whose trailing segment
        # is the artwork's own stamp (recorded: `pms_play_history.xml.txt`,
        # `pms_collections_listing.json`). The poster proxy reads exactly this and builds its ETag
        # from that stamp, so leaving it off would exercise only the "no artwork" branch.
        thumb=f"/library/metadata/{movie.rating_key}/thumb/{movie.added_at}",
        # The library that actually holds it — never inferred from the type, or a second movie
        # library's items would all claim to live in the first one.
        librarySectionID=section.key if section else state.section_id,
    )
    if watched_by is not None:
        # Omitted for a show in `undated_in_show_read`: see the field. The episode read is then the
        # only place its date exists, which is what the production date-repair path is built on.
        if not (is_show and movie.rating_key in state.undated_in_show_read):
            element.set("lastViewedAt", str(state.last_viewed_at(watched_by, movie.rating_key)))
        # Only for the account being read AS — the real PMS omits the attribute entirely for anyone
        # who hasn't rated it, which is what makes "never rated" distinguishable from a 0.
        rating = state.user_ratings.get((watched_by, movie.rating_key))
        if rating is not None:
            element.set("userRating", str(rating))
        if is_show:
            # A watched show defaults to fully watched (viewed == total) — the "finished the series"
            # case. `state.watch_episodes()` overrides it per account so the fake can also serve a
            # series IN PROGRESS, which is what `unwatched=0` mostly returns on a real server and the
            # only shape that tells `watched` and `finished` apart.
            viewed = state.partial_shows.get((watched_by, movie.rating_key), movie.leaf_count)
            # `FakeMovie.leaf_count` defaults to 0, and the show read now filters on
            # `viewedLeafCount != 0` — so a show seeded without an explicit leaf_count is invisible to
            # it and the failure reads as a mystery. Loud here rather than puzzling three files away.
            assert movie.leaf_count, (
                f"show {movie.rating_key} ({movie.title!r}) was seeded with leaf_count=0, so it can "
                "never appear in a watched read — give it a real episode count"
            )
            element.set("viewedLeafCount", str(min(viewed, movie.leaf_count)))
            element.set("leafCount", str(movie.leaf_count))
        else:
            # viewCount = how many times this account has this title in history (>= 1, since it's watched).
            plays, offset = state.leaf_view(watched_by, movie.rating_key)
            element.set("viewCount", str(max(1, plays)))
            # A title can be watched AND part-way through a rewatch. Omitted when zero, as a real
            # server omits it — the absent-vs-zero distinction is load-bearing for partial plays.
            if offset:
                element.set("viewOffset", str(offset))
    _el(element, "Guid", id=f"tmdb://{movie.tmdb_id}")
    return element


def _collection_xml(
    parent: Element, state: FakePlexState, collection: FakeCollection, *, labels: bool = True
) -> Element:
    directory = _el(
        parent,
        "Directory",
        ratingKey=collection.rating_key,
        # `/library/collections/<rk>/children`, exactly as PMS 1.43.3 returns it — recorded from a
        # real server 2026-08-10. plexapi strips the `/children` to build `key`, and every reload of
        # this object then goes to `/library/collections/<rk>`, which is why that route must exist.
        key=f"/library/collections/{collection.rating_key}/children",
        type="collection",
        subtype=collection.subtype,
        title=collection.title,
        smart="0",
        collectionMode=collection.mode,
        collectionSort=collection.sort,
        librarySectionID=collection.section_id,
    )
    # A real PMS returns NO <Label> children in the section LISTING — verified against 1.43.3.10861:
    # 103 collections, zero with labels. They appear only on the per-collection detail read. Serving
    # them inline made every test prove label identity against a shape Plex does not produce, and hid
    # the fact that the whole feature depends on plexapi silently re-reading each collection.
    if labels:
        for i, tag in enumerate(collection.labels, start=1):
            _el(directory, "Label", id=i, tag=tag)
    # plexapi's editAdvanced (modeUpdate/sortUpdate) reads these to validate enum values.
    preferences = SubElement(directory, "Preferences")
    for setting_id, default, value, enums in (
        ("collectionMode", "-1", collection.mode, "-1:Library default|0:Hide collection|1:Hide items|2:Show items"),
        ("collectionSort", "0", collection.sort, "0:Release date|1:Alphabetical|2:Custom"),
    ):
        _el(preferences, "Setting", id=setting_id, type="int", default=default, value=value, enumValues=enums)
    return directory


def _managed_hub_xml(parent: Element, section_id: int, collection: FakeCollection) -> Element:
    # Identifier matches what plexapi synthesizes (custom.collection.<sectionID>.<ratingKey>)
    # so ManagedHub.reload() can find this hub again after updateVisibility.
    return _el(
        parent,
        "Hub",
        identifier=f"custom.collection.{section_id}.{collection.rating_key}",
        title=collection.title,
        deletable="1",
        promotedToRecommended=int(collection.promoted_recommended),
        promotedToOwnHome=int(collection.promoted_own_home),
        promotedToSharedHome=int(collection.promoted_shared_home),
        homeVisibility="all" if collection.promoted_shared_home else "none",
        recommendationsVisibility="all" if collection.promoted_recommended else "none",
    )


#: Plex's OWN hubs, which `GET /hubs/sections/{id}/manage` returns alongside the collections —
#: recorded in `tests/fixtures/pms_managed_hubs.xml.txt`. The fake served collections only, and their
#: promotion flags are now load-bearing on both sides of the app: `can_anchor` refuses an anchor that
#: is promoted nowhere, and the anchor picker greys the same ones out. With no built-in in the fake,
#: no full-stack path ever saw one (testing rule: the fake must be no easier than the real server).
#:
#: One ON and one OFF, because the OFF case is the one that used to be silently unplaceable: a
#: built-in the owner switched off in Manage Recommendations reads with all three flags at 0.
_BUILTIN_HUBS = (
    ("movie.recentlyadded", "Recently Added", True),
    ("movie.genre", "By Genre", False),
)


def _builtin_hub_xml(parent: Element, identifier: str, title: str, promoted: bool) -> Element:
    """A built-in hub as a real PMS serves it: no `deletable`, and all three flags always present."""
    return _el(
        parent,
        "Hub",
        identifier=identifier,
        title=title,
        promotedToRecommended=int(promoted),
        promotedToOwnHome=0,
        promotedToSharedHome=0,
        homeVisibility="none",
        recommendationsVisibility="all" if promoted else "none",
    )


def _page(request: Request, total: int) -> tuple[int, int]:
    """Container paging: plexapi sends X-Plex-Container-Start/Size as headers OR query params."""
    query, headers = request.query_params, request.headers
    start = int(query.get("X-Plex-Container-Start") or headers.get("X-Plex-Container-Start") or 0)
    raw_size = query.get("X-Plex-Container-Size") or headers.get("X-Plex-Container-Size")
    size = int(raw_size) if raw_size is not None else total
    return start, size


def _meta_xml(state: FakePlexState, section_id: int, total: int) -> Element:
    """Filter metadata plexapi loads before validating any sort= argument."""
    root = _container(size=0, totalSize=total)
    meta = SubElement(root, "Meta")
    section = state.sections[section_id]
    kind = section.type
    item_type = _el(
        meta,
        "Type",
        key=f"/library/sections/{section_id}/all?type={1 if kind == 'movie' else 2}",
        type=kind,
        title=section.title,
    )
    for key, direction, title in (
        ("addedAt", "asc", "Date Added"),
        ("audienceRating", "desc", "Audience Rating"),
        ("titleSort", "asc", "Title"),
    ):
        _el(item_type, "Sort", key=key, defaultDirection=direction, title=title)
    collection_type = _el(
        meta, "Type", key=f"/library/sections/{section_id}/all?type=18", type="collection", title="Collections"
    )
    _el(collection_type, "Sort", key="titleSort", defaultDirection="asc", title="Title")
    return root


def _sorted_items(items: list[FakeMovie], sort: str | None) -> list[FakeMovie]:
    if not sort:
        return sorted(items, key=lambda m: m.rating_key)
    fieldname, _, direction = sort.split(",")[0].rsplit(".", 1)[-1].partition(":")  # 'movie.addedAt:asc' -> addedAt
    return sorted(items, key=_SORT_KEYS.get(fieldname, lambda m: m.rating_key), reverse=direction == "desc")


def _poster_title_lines(draw, title: str, font, max_width: int) -> list[str]:
    """Wrap a poster title to at most two lines, ellipsising the second — a title block has no third."""
    lines = [""]
    for word in title.split():
        trial = f"{lines[-1]} {word}".strip()
        if not lines[-1] or draw.textlength(trial, font=font) <= max_width:
            lines[-1] = trial
        elif len(lines) < 2:
            lines.append(word)
        else:
            lines[-1] = f"{lines[-1]}\u2026"
            break
    return lines


#: Where `scripts/fetch_demo_posters.py` puts real cover art. Gitignored and usually absent.
_DEMO_POSTERS = Path(__file__).resolve().parents[1] / "e2e" / "assets" / "posters"
#: Only a capture run draws on that art; every other run gets the drawn placeholder, so the
#: bytes a test sees never depend on what somebody happened to download.
_CAPTURING = bool(os.environ.get("SHOTS_DIR"))


@lru_cache(maxsize=256)
def _fake_poster(rating_key: int, title: str = "") -> bytes:
    """A poster-SHAPED, per-title-COLOURED image carrying its own title, not a stretched single pixel.

    The 1x1 placeholder below is right for asserting "artwork was served"; it is wrong for the
    screenshots the docs site ships, where every pick rendered as the same flat green rectangle and
    the pick list looked broken rather than illustrated. Since `.claude/rules/testing.md` says the
    fake must be no EASIER than the real server, and a real PMS returns a distinct 2:3 image per
    title with that title printed on it, this returns one too — at 400x600, because the docs site's
    hero renders a poster around 360 device pixels wide and a 200px source upscales to mush.

    Deterministic from the rating key and title, so a screenshot re-taken tomorrow is byte-identical
    and does not churn the repo. Falls back to the flat pixel if Pillow is missing, so the fake never
    becomes the reason a test cannot run.
    """
    # Real cover art, but ONLY while capturing the docs images — see `scripts/fetch_demo_posters.py`.
    #
    # Gated on SHOTS_DIR rather than on the file simply being there, which is how it was written
    # first and was wrong: whether a developer had ever run the fetch script then decided what these
    # bytes were, so `test_a_delivered_pick_serves_the_artwork_the_server_actually_holds` passed on
    # CI and failed on the machine that had. A fixture must not depend on untracked local state.
    if _CAPTURING and (real := _DEMO_POSTERS / f"{rating_key}.jpg").is_file():
        return real.read_bytes()

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover - Pillow ships in requirements.lock via the posters extra
        return _PNG_1X1

    width, height, band = 400, 600, 118
    hue = (rating_key * 47) % 360  # spread neighbouring keys far apart so a list looks varied
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        # Top-to-bottom darkening, which is what makes it read as artwork rather than a colour swatch.
        lightness = 62 - int(38 * y / height)
        draw.line([(0, y), (width, y)], fill=f"hsl({hue}, 45%, {lightness}%)")
    # A darker band where a real poster carries its title block.
    draw.rectangle([0, height - band, width, height], fill=f"hsl({hue}, 40%, 14%)")
    if title:
        # The same built-in face `poster_service` renders row posters with, so the fake needs no font
        # file on disk and CI, the Mac and the image all produce identical bytes.
        font = ImageFont.load_default(size=36)
        lines = _poster_title_lines(draw, title, font, width - 40)
        y = height - band + (band - len(lines) * 44) // 2
        for line in lines:
            draw.text(((width - draw.textlength(line, font=font)) / 2, y), line, font=font, fill=(238, 238, 242))
            y += 44
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_fake_plex(state: FakePlexState) -> FastAPI:
    """PMS surface (path prefix '') — enough for plexapi + the engine's raw /hubs calls."""
    app = FastAPI()

    def _collection(rating_key: int) -> FakeCollection:
        collection = state.collections.get(rating_key)
        if collection is None:
            raise HTTPException(status_code=404, detail=f"collection {rating_key} not found")
        return collection

    @app.get("/")
    @app.get("/identity")
    def root() -> Response:
        attrs = {"friendlyName": state.friendly_name, "machineIdentifier": state.machine_id}
        return _xml(_container(size=0, version=state.version, **attrs))

    @app.get("/library")
    def library_root() -> Response:
        return _xml(_container(size=1, title1="Plex Library", identifier="com.plexapp.plugins.library"))

    @app.get("/library/sections")
    def sections() -> Response:
        root = _container(size=len(state.sections), allowSync="0", title1="Plex Library")
        for section in state.sections.values():
            _el(
                root,
                "Directory",
                key=section.key,
                type=section.type,
                title=section.title,
                uuid=f"section-uuid-{section.key}",
                filters="1",
            )
        return _xml(root)

    @app.get("/library/sections/{section_id}/all")
    @app.get("/library/sections/{section_id}/collections")
    def section_all(section_id: int, request: Request) -> Response:
        query = request.query_params
        items = state.items_in(section_id)
        if query.get("includeMeta") == "1":
            return _xml(_meta_xml(state, section_id, len(items)))
        if query.get("type") == "18" or request.url.path.endswith("/collections"):
            owned = [c for c in state.collections.values() if c.section_id == section_id]
            root = _container(size=len(owned), totalSize=len(owned), librarySectionID=section_id)
            for collection in owned:
                _collection_xml(root, state, collection, labels=False)
            return _xml(root)
        # LEAF reads — the watching-account transfer's four reads. `type=4` is episodes; `viewOffset>`
        # is the only way a PARTIAL play is visible at all, and both filters ARE honoured server-side
        # (unlike `lastViewedAt>=`, which this PMS silently drops — see the note further down).
        if query.get("type") == "4" or query.get("viewOffset>") is not None:
            account_id = state.watched_account_id(request.headers.get("X-Plex-Token", ""))
            want_episodes = query.get("type") == "4"
            listing = []
            for item in _sorted_items(list(items.values()), None):
                if want_episodes and item.media_type != "episode":
                    continue
                if not want_episodes and item.media_type != "movie":
                    continue
                count, offset = state.leaf_view(account_id or 0, item.rating_key)
                if query.get("viewOffset>") is not None and offset <= int(query["viewOffset>"]):
                    continue
                if query.get("unwatched") == "0" and count <= 0:
                    continue
                listing.append((item, count, offset))
            start, size = _page(request, len(listing))
            root = _container(
                size=len(listing[start : start + size]), totalSize=len(listing), librarySectionID=section_id
            )
            for item, count, offset in listing[start : start + size]:
                _leaf_xml(root, item, count, offset)
            return _xml(root)
        # The share-token watched read (ShareTokenWatchSource): `unwatched=0` filters to what the
        # REQUESTING account has watched, served AS them with their own per-user viewCount/leaf counts.
        # The token is in the X-Plex-Token header (includeToken=False keeps the owner's out of the URL).
        if query.get("unwatched") == "0" or query.get("viewedLeafCount!") is not None:
            account_id = state.watched_account_id(request.headers.get("X-Plex-Token", ""))
            watched = state.watched_now(account_id) if account_id is not None else set()
            # The two queries DISAGREE, exactly as they do on a real server (issue #108).
            # `unwatched=0` filters on the show's own watch-state row, which marking a series or a
            # season never establishes — so a finished series is missing from it while its episode
            # counts are correct. `viewedLeafCount!=0` filters on the counts and returns it.
            #
            # Modelled because otherwise both answer from the same set, they can never disagree, and
            # the whole reason the read changed is unrepresentable here — the rule that a fake must
            # be no easier than the real server.
            if query.get("viewedLeafCount!") is None:
                watched -= state.invisible_to_show_read
            listing = [item for item in _sorted_items(list(items.values()), None) if item.rating_key in watched]
            # The INCREMENTAL read asks for `sort=lastViewedAt:desc` and stops client-side at the
            # first title older than its cutoff. It deliberately does NOT send a `lastViewedAt>=`
            # filter: real PMS 1.43.3 silently ignores that (live-probed 2026-07-30), and a fake that
            # honoured a filter the real server drops would make the e2e suite prove the opposite of
            # the truth. So honour the SORT — which the real server does — and nothing else.
            if query.get("sort") == "lastViewedAt:desc" and account_id is not None:
                listing = sorted(listing, key=lambda i: state.last_viewed_at(account_id, i.rating_key), reverse=True)
            start, size = _page(request, len(listing))
            page = listing[start : start + size]
            root = _container(size=len(page), totalSize=len(listing), librarySectionID=section_id)
            for item in page:
                _movie_xml(root, state, item, watched_by=account_id)
            return _xml(root)
        listing = _sorted_items(list(items.values()), query.get("sort"))
        if query.get("limit") is not None:
            listing = listing[: int(query["limit"])]
        start, size = _page(request, len(listing))
        page = listing[start : start + size]
        root = _container(size=len(page), totalSize=len(listing), librarySectionID=section_id)
        for item in page:
            _movie_xml(root, state, item)
        return _xml(root)

    @app.get("/:/scrobble")
    def scrobble(request: Request) -> Response:
        """Mark one key played AS the requesting account.

        No history row is written, deliberately: probed against a real server, 31 scrobbles left
        `/status/sessions/history/all` empty. The whole transfer design turns on that, so a fake that
        recorded them here would let a regression through unnoticed.

        404 for a key this account cannot see — the shape `scrobble_as` treats as skip, not failure.
        """
        account_id = state.watched_account_id(request.headers.get("X-Plex-Token", ""))
        key = int(request.query_params.get("key") or 0)
        if account_id is None or not state.scrobble(account_id, key):
            raise HTTPException(status_code=404, detail="not found")
        return _xml(_container(size=0))

    @app.get("/:/unscrobble")
    def unscrobble(request: Request) -> Response:
        account_id = state.watched_account_id(request.headers.get("X-Plex-Token", ""))
        key = int(request.query_params.get("key") or 0)
        if account_id is None or not state.unscrobble(account_id, key):
            raise HTTPException(status_code=404, detail="not found")
        return _xml(_container(size=0))

    @app.get("/:/progress")
    def progress(request: Request) -> Response:
        """Set a playback position — the only way a PARTIAL watch is written.

        It does NOT clear one: `time=0` is ignored here exactly as the server ignores it (see
        `set_progress`). Only `/:/unscrobble` clears an offset. The claim that this endpoint could
        clear one is the assumption that left 293 items part-watched after a live undo, so it does not
        get to live in the file whose job is to encode what the server really does.
        """
        account_id = state.watched_account_id(request.headers.get("X-Plex-Token", ""))
        key = int(request.query_params.get("key") or 0)
        offset = int(request.query_params.get("time") or 0)
        if account_id is None or not state.set_progress(account_id, key, offset):
            raise HTTPException(status_code=404, detail="not found")
        return _xml(_container(size=0))

    @app.put("/library/sections/{section_id}/all")
    def section_edit(section_id: int, request: Request) -> Response:
        """plexapi's tag/field edit endpoint (addLabel, editTitle): type=18&id=...&label[0].tag.tag=..."""
        query = request.query_params
        labels = [value for key, value in query.multi_items() if _LABEL_PARAM.match(key)]
        for raw_id in (query.get("id") or "").split(","):
            collection = state.collections.get(int(raw_id)) if raw_id.isdigit() else None
            if collection is None:
                continue
            if labels:
                existing = {label.lower(): label for label in collection.labels}
                collection.labels = [existing.get(v.lower(), state.store_label(v)) for v in labels]
            if query.get("title.value"):
                collection.title = query["title.value"]
        return Response(status_code=200)

    @app.post("/library/collections")
    def create_collection(request: Request) -> Response:
        query = request.query_params
        item_keys = [int(k) for k in query["uri"].rsplit("/library/metadata/", 1)[-1].split(",")]
        kept = [k for k in item_keys if state.item(k)]
        types = {state.item(k).media_type for k in kept}
        collection = FakeCollection(
            rating_key=state.new_rating_key(),
            title=query.get("title", ""),
            section_id=int(query.get("sectionId") or state.section_id),
            # The PMS happily puts a collection of shows in a movie library — it only objects to
            # MIXING types in one collection (plexapi rejects that client-side). Refusing the
            # wrong-library case here would hide the very bug this fake exists to catch.
            item_keys=kept,
            # Subtype comes from the items, NOT from the library — that is how a movie library
            # ends up holding a show-subtype collection that no share filter can touch.
            subtype=types.pop() if len(types) == 1 else "movie",
        )
        state.collections[collection.rating_key] = collection
        root = _container(size=1)
        _collection_xml(root, state, collection)
        return _xml(root)

    @app.get("/library/collections/{rating_key}")
    def collection_detail(rating_key: int) -> Response:
        """The per-collection read plexapi falls back to whenever it wants a field the section
        listing does not carry — labels being the one Shortlist's whole identity model rests on.

        Real PMS 1.43.3 serves labels ONLY here, never in the listing, so this route is the only
        reason `collection.labels` is ever non-empty in production. Without it the tests exercised
        an easier server than the real one.
        """
        collection = _collection(rating_key)
        root = _container(size=1, librarySectionID=collection.section_id)
        _collection_xml(root, state, collection, labels=True)
        return _xml(root)

    @app.get("/library/collections/{rating_key}/children")
    @app.get("/library/metadata/{rating_key}/children")
    def collection_children(rating_key: int) -> Response:
        collection = _collection(rating_key)
        members = state.members(collection)  # shared with any same-titled collection in this library
        root = _container(size=len(members), totalSize=len(members))
        for key in members:
            if (item := state.item(key)) is not None:
                _movie_xml(root, state, item)
        return _xml(root)

    @app.put("/library/collections/{rating_key}/items")
    @app.put("/library/metadata/{rating_key}/items")
    def collection_add_items(rating_key: int, request: Request) -> Response:
        collection = _collection(rating_key)
        for raw in request.query_params["uri"].rsplit("/library/metadata/", 1)[-1].split(","):
            key = int(raw)
            if state.item(key) and key not in collection.item_keys:
                collection.item_keys.append(key)
        return Response(status_code=200)

    @app.delete("/library/collections/{rating_key}/items/{item_key}")
    @app.delete("/library/metadata/{rating_key}/items/{item_key}")
    def collection_remove_item(rating_key: int, item_key: int) -> Response:
        collection = _collection(rating_key)
        collection.item_keys = [k for k in collection.item_keys if k != item_key]
        return Response(status_code=200)

    @app.put("/library/collections/{rating_key}/items/{item_key}/move")
    @app.put("/library/metadata/{rating_key}/items/{item_key}/move")
    def collection_move_item(rating_key: int, item_key: int, request: Request) -> Response:
        collection = _collection(rating_key)
        after = request.query_params.get("after")
        collection.item_keys.remove(item_key)
        position = collection.item_keys.index(int(after)) + 1 if after else 0
        collection.item_keys.insert(position, item_key)
        return Response(status_code=200)

    @app.put("/library/collections/{rating_key}/prefs")
    @app.put("/library/metadata/{rating_key}/prefs")
    def collection_prefs(rating_key: int, request: Request) -> Response:
        collection = _collection(rating_key)
        if request.query_params.get("collectionMode") is not None:
            collection.mode = int(request.query_params["collectionMode"])
        if request.query_params.get("collectionSort") is not None:
            collection.sort = int(request.query_params["collectionSort"])
        return Response(status_code=200)

    @app.delete("/library/collections/{rating_key}")
    @app.delete("/library/metadata/{rating_key}")
    def delete_collection(rating_key: int) -> Response:
        collection = _collection(rating_key)
        # A collection is a TAG keyed by title. We have verified on a real PMS that same-titled
        # collections in one library SHARE their membership; we have NOT verified what deleting one
        # does to the others. So the fake assumes the worse of the two possibilities — the tag goes,
        # and every same-titled sibling empties — because code that is correct under that is correct
        # either way, and code that is only correct under the kinder assumption would fail live.
        for other in state.collections.values():
            if (
                other is not collection
                and other.section_id == collection.section_id
                and other.title == collection.title
            ):
                other.item_keys = [k for k in other.item_keys if k not in collection.item_keys]
        del state.collections[rating_key]
        return Response(status_code=200)

    @app.get("/library/metadata/{rating_keys}")
    def metadata(rating_keys: str) -> Response:
        root = _container(librarySectionID=state.section_id)
        found = 0
        for raw in rating_keys.split(","):
            key = int(raw)
            if (item := state.item(key)) is not None:
                _movie_xml(root, state, item)
                found += 1
            elif key in state.collections:
                _collection_xml(root, state, state.collections[key])
                found += 1
        if not found:
            raise HTTPException(status_code=404, detail=f"no items for {rating_keys}")
        root.set("size", str(found))
        return _xml(root)

    @app.get("/library/metadata/{rating_key}/thumb/{stamp}")
    def item_thumb(rating_key: int, stamp: str) -> Response:
        """One item's artwork bytes.

        Served ONLY behind the metadata read, exactly as a real PMS does: the caller has to learn the
        path (stamp included) from `/library/metadata/{key}` first. Guessing it — or asking for an
        item that is gone — is a 404, so the proxy's "missing item" branch is a real branch here and
        not something only the mocks can reach.
        """
        item = state.item(rating_key)
        if item is None or stamp != str(item.added_at):
            raise HTTPException(status_code=404, detail=f"no artwork at {rating_key}/thumb/{stamp}")
        # Sniffed, not assumed: a real PMS serves whatever the artwork happens to be, and while
        # capturing this is a JPEG off TMDB rather than the drawn PNG.
        art = _fake_poster(rating_key, item.title)
        return Response(art, media_type="image/jpeg" if art[:2] == b"\xff\xd8" else "image/png")

    def _shelf(section_id: int) -> list[str]:
        """This section's managed-hub order, as one list of identifiers.

        Self-maintaining: it starts as Plex's own hubs, and every collection the section has that is
        not in it yet is APPENDED — which is exactly what a real PMS does with a newly created
        collection, and the reason an unplaced row sinks out of sight. Collections that have gone are
        dropped. Returned by reference so `move_hub` reorders the state.
        """
        order = state.hub_order.setdefault(section_id, [identifier for identifier, _t, _p in _BUILTIN_HUBS])
        live = [
            f"custom.collection.{section_id}.{c.rating_key}"
            for c in state.collections.values()
            if c.section_id == section_id
        ]
        order[:] = [i for i in order if not i.startswith("custom.collection.") or i in live]
        order.extend(i for i in live if i not in order)
        return order

    @app.get("/hubs/sections/{section_id}/manage")
    def manage_hubs(section_id: int, request: Request) -> Response:
        wanted = request.query_params.get("metadataItemId")
        root = _container()
        builtins = dict((identifier, (title, promoted)) for identifier, title, promoted in _BUILTIN_HUBS)
        for identifier in _shelf(section_id):
            if identifier in builtins:
                # A single-hub lookup asks about one COLLECTION, so Plex's own hubs are not in it.
                if wanted is None:
                    title, promoted = builtins[identifier]
                    _builtin_hub_xml(root, identifier, title, promoted)
                continue
            collection = state.collections.get(int(identifier.rsplit(".", 1)[-1]))
            if collection is None:
                continue
            if wanted is not None and collection.rating_key != int(wanted):
                continue
            _managed_hub_xml(root, section_id, collection)
        root.set("size", str(len(root)))
        return _xml(root)

    def _apply_hub_flags(collection: FakeCollection, query) -> None:
        collection.promoted_recommended = query.get("promotedToRecommended") == "1"
        collection.promoted_own_home = query.get("promotedToOwnHome") == "1"
        collection.promoted_shared_home = query.get("promotedToSharedHome") == "1"

    @app.post("/hubs/sections/{section_id}/manage")
    def promote_hub(section_id: int, request: Request) -> Response:
        collection = _collection(int(request.query_params["metadataItemId"]))
        _apply_hub_flags(collection, request.query_params)
        return Response(status_code=200)

    @app.put("/hubs/sections/{section_id}/manage/{identifier}/move")
    def move_hub(section_id: int, identifier: str, request: Request) -> Response:
        # after=None (no query) -> pinned to the top of the Managed Recommendations shelf.
        after = request.query_params.get("after")
        # And REALLY reorder. `manage_hubs` serves `state.collections` in insertion order, so this
        # used to answer 200 while the shelf never moved — which is precisely the misbehaviour a real
        # PMS was caught in (2026-08-12), and which `place_rows` now retries and reports as
        # unverified. A fake that behaves like the bug makes every shelf-order assertion vacuous and
        # would have had e2e re-issuing moves three times and finishing on a warning. Testing rule:
        # the fake must be no easier than the real server.
        order = _shelf(section_id)
        if identifier not in order:
            return Response(status_code=404)
        order.remove(identifier)
        if after is None:
            order.insert(0, identifier)
        elif after in order:
            order.insert(order.index(after) + 1, identifier)
        else:
            order.append(identifier)
        return Response(status_code=200)

    @app.put("/hubs/sections/{section_id}/manage/{identifier}")
    def update_hub(section_id: int, identifier: str, request: Request) -> Response:
        collection = _collection(int(identifier.rsplit(".", 1)[-1]))
        _apply_hub_flags(collection, request.query_params)
        return Response(status_code=200)

    @app.get("/hubs")
    def hubs(request: Request) -> JSONResponse:
        token = request.headers.get("X-Plex-Token", "")
        user = state.user_for_token(token)
        if user is None and token != state.owner_token:
            return JSONResponse({"errors": [{"code": 1001, "message": "Unauthorized"}]}, status_code=401)
        excludes = state.excluded_labels(user) if user else set()
        hub_list: list[dict] = [
            {"key": "/hubs/home/continueWatching", "title": "Continue Watching", "type": "mixed", "promoted": True}
        ]
        for collection in state.collections.values():
            # Plex splits Home by OWNER vs everyone-else, NOT by Plex-Home membership:
            # `promotedToOwnHome` "applies to the server owner", `promotedToSharedHome` "applies to
            # all shared users, including managed users" —
            # https://support.plex.tv/articles/manage-recommendations/. `user is None` is the owner.
            promoted = collection.promoted_own_home if user is None else collection.promoted_shared_home
            if not promoted:
                continue
            excluded = bool({label.lower() for label in collection.labels} & excludes)
            # An exclude only takes effect if the PMS can actually match this collection with a
            # library filter. Off-type collections are unfilterable and stay visible — the leak.
            if excluded and state.filterable(collection):
                continue
            children_key = f"/library/collections/{collection.rating_key}/children"
            hub_list.append(
                {
                    "key": children_key,
                    "hubKey": children_key,
                    "title": collection.title,
                    "type": collection.subtype,
                    "hubIdentifier": f"custom.collection.{collection.rating_key}",
                    "promoted": True,
                }
            )
        return JSONResponse({"MediaContainer": {"size": len(hub_list), "Hub": hub_list}})

    @app.get("/accounts")
    def accounts() -> Response:
        """PMS's own account table. The owner is a LOCAL account here (not their plex.tv id), which
        is why the owner's history has to be looked up by the id PMS files it under."""
        root = _container(size=len(state.users) + 2)
        _el(root, "Account", id=0, key="/accounts/0", name="", defaultAudioLanguage="", autoSelectAudio="1")
        _el(
            root,
            "Account",
            id=state.owner_pms_account_id,
            key=f"/accounts/{state.owner_pms_account_id}",
            name=state.owner_username,
            defaultAudioLanguage="en",
            autoSelectAudio="1",
        )
        for user in state.users.values():
            _el(root, "Account", id=user.id, key=f"/accounts/{user.id}", name=user.username, autoSelectAudio="1")
        return _xml(root)

    @app.get("/status/sessions/history/all")
    def history(request: Request) -> Response:
        account_id = request.query_params.get("accountID")
        rows = [h for h in state.history if account_id is None or h.account_id == int(account_id)]
        rows.sort(key=lambda h: h.viewed_at, reverse=True)
        start, size = _page(request, len(rows))
        page = rows[start : start + size]
        root = _container(size=len(page), totalSize=len(rows))
        for i, row in enumerate(page):
            item = state.item(row.rating_key)
            if item is None:
                continue
            attrs = {
                "historyKey": f"/status/sessions/history/{start + i + 1}",
                "key": f"/library/metadata/{item.rating_key}",
                "ratingKey": item.rating_key,
                "title": item.title,
                "type": item.media_type,
                "viewedAt": row.viewed_at,
                "accountID": row.account_id,
            }
            if item.media_type == "show":
                # Plex logs TV watches as EPISODE rows: the show is the grandparent, and the
                # episode's own title/ratingKey are useless as a recommendation seed.
                attrs |= {
                    "type": "episode",
                    "title": f"Episode {i + 1}",
                    "ratingKey": 90_000 + item.rating_key,
                    "grandparentTitle": item.title,
                    "grandparentRatingKey": item.rating_key,
                }
            _el(root, "Video", **attrs)
        return _xml(root)

    return app


def make_fake_plextv(state: FakePlexState) -> FastAPI:
    """plex.tv surface — mounted as its own app because the engine hits absolute plex.tv URLs."""
    app = FastAPI()

    @app.get("/api/users")
    def list_users() -> Response:
        root = _container(friendlyName="myPlex", identifier="com.plexapp.plugins.myplex", size=len(state.users))
        for user in state.users.values():
            user_el = _el(
                root,
                "User",
                id=user.id,
                title=user.username,
                username=user.username,
                email=f"{user.username}@example.com",
                thumb=f"https://plex.tv/users/{user.id}/avatar",
                home=int(user.home),
                restricted=int(user.restricted),
                protected=int(user.protected),
                **user.filters,
            )
            _el(
                user_el,
                "Server",
                id=user.id,
                serverId="1",
                machineIdentifier=state.machine_id,
                name=state.friendly_name,
            )
        return _xml(root)

    @app.get("/api/servers/{machine_id}/shared_servers")
    def shared_servers(machine_id: str) -> Response:
        """The per-user server tokens plex.tv mints for every shared invite (ShareTokenWatchSource).

        Every shared/Home user gets one; the token is ``server-<accountID>`` so the PMS fake serves
        that user's own watched set for it (``user_for_token``). The owner is NOT here — they own the
        server rather than being shared to it, so the source reads their state with the admin token.
        """
        root = _container(size=len(state.users))
        for user in state.users.values():
            _el(
                root,
                "SharedServer",
                id=user.id,
                userID=user.id,
                username=user.username,
                accessToken=f"server-{user.id}",
                machineIdentifier=machine_id,
            )
        return _xml(root)

    @app.put("/api/users/{account_id}")
    def update_user(account_id: int, request: Request) -> Response:
        user = state.users.get(account_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"user {account_id} not found")
        for fieldname in FILTER_FIELDS:
            if fieldname in request.query_params:
                user.filters[fieldname] = request.query_params[fieldname]
        return Response(status_code=200, content="<Response code='200'/>", media_type="text/xml")

    @app.get("/api/home/users")
    def home_users_v1() -> Response:
        """The v1 XML surface, which is the ONLY one carrying `restrictionProfile`.

        Served because `PlexTvClient.list_users()` reads it to tell a parental-controlled managed
        account from a plain one — the distinction issue #20 turns on. Without it here, every
        full-stack test took the fail-open branch and the enrichment was never exercised at all.
        """
        rows = "".join(
            f'<User id="{u.id}" title="{u.username}" restricted="{int(u.restricted)}"'
            + (f' restrictionProfile="{u.restriction_profile}"' if u.restriction_profile else "")
            + ' protected="0"/>'
            for u in state.users.values()
            if u.home
        )
        return Response(
            status_code=200,
            content=f'<?xml version="1.0" encoding="UTF-8"?><MediaContainer>{rows}</MediaContainer>',
            media_type="text/xml",
        )

    @app.get("/api/v2/home/users")
    def home_users() -> JSONResponse:
        home = [u for u in state.users.values() if u.home]
        rows = [{"id": u.id, "uuid": u.uuid, "title": u.username, "protected": u.protected} for u in home]
        return JSONResponse({"users": rows})

    @app.post("/api/v2/home/users/{uuid}/switch")
    def switch_home_user(uuid: str) -> JSONResponse:
        user = next((u for u in state.users.values() if u.uuid == uuid), None)
        if user is None:
            raise HTTPException(status_code=404, detail=f"unknown home user {uuid}")
        return JSONResponse({"authToken": f"switch-{user.id}"})

    @app.post("/api/v2/pins")
    def create_pin() -> JSONResponse:
        """Mint a PIN. The e2e sign-in must run the REAL endpoint: a browser stub that forged the
        session cookie would keep passing even if `poll_pin` stopped setting one."""
        return JSONResponse({"id": 1234, "code": "ABCD"})

    @app.get("/api/v2/pins/{pin_id}")
    def poll_pin(pin_id: int) -> JSONResponse:
        """Already linked — a human typed the code at plex.tv while we weren't looking."""
        return JSONResponse({"id": pin_id, "code": "ABCD", "authToken": state.owner_token})

    @app.get("/api/v2/user")
    def whoami(request: Request) -> JSONResponse:
        """Who this token belongs to, and whether they have Plex Pass (the setup probe asks)."""
        if request.headers.get("X-Plex-Token") != state.owner_token:
            raise HTTPException(status_code=401, detail="bad token")
        return JSONResponse(
            {
                "id": state.owner_account_id,
                "username": state.owner_username,
                "title": state.owner_username.title(),
                "thumb": f"https://plex.tv/users/{state.owner_account_id}/avatar",
                "subscription": {"active": True},
            }
        )

    @app.get("/api/v2/resources")
    def resources(request: Request) -> JSONResponse:
        token = request.headers.get("X-Plex-Token", "")
        server = {
            "name": state.friendly_name,
            "clientIdentifier": state.machine_id,
            "provides": "server",
            "productVersion": state.version,
            "owned": True,
            # What the server picker consumes: several advertised addresses, only one of which
            # actually answers — exactly the situation the picker exists to resolve.
            "connections": [
                {"uri": state.pms_url, "local": True, "relay": False},
                {"uri": "http://10.255.255.1:32400", "local": False, "relay": False},
            ],
        }
        if token.startswith("switch-"):
            # The T2 canary flow: exchange a Home-user switch token for a server access token.
            account_id = token.removeprefix("switch-")
            return JSONResponse([{**server, "accessToken": f"server-{account_id}"}])
        return JSONResponse([server])

    return app
