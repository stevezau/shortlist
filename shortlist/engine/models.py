"""Engine dataclasses: inputs, intermediate stages, and run reports."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MediaType(StrEnum):
    MOVIE = "movie"
    SHOW = "show"


class UserType(StrEnum):
    OWNER = "owner"
    SHARED = "shared"
    MANAGED = "managed"


# Every Plex label/collection Shortlist owns starts with this. Was also an `EngineConfig` field,
# but nothing ever assigned it a non-default value, so `UserProfile.label` had already hardcoded
# the literal separately — one knob nobody turned, and one hardcode that could drift from it. This
# constant is now the single place either could change.
LABEL_PREFIX = "shortlist"


def is_human_rating(value: float | None) -> bool:
    """Whether a Plex ``userRating`` was plausibly set by a PERSON rather than by a tool.

    Plex's own rating controls write whole numbers only — five stars in half-star steps on a 0..10
    scale, and thumbs, which land on the same grid. Nothing a user can press produces 6.2.

    Tools do. Kometa's rating sync writes IMDb/TMDB scores straight into ``userRating``, and those
    carry a decimal: measured on the maintainer's server, 1,455 of the owner's 1,630 watched titles
    were rated this way and 90.7% of the values were fractional, against 0 of 36 among the 49 real
    viewers. Treating those as opinions would have silently stopped dozens of the owner's own seeds
    on IMDb's say-so. Coexisting with Kometa is a standing rule here (plex-safety 4), and this is
    that rule applied to a field rather than to a collection.

    It cannot catch a tool's value that lands on a whole number by chance (~9% of them did), which is
    why `history.ratings_are_trustworthy` judges the account as well.

    Args:
        value: A 0..10 ``userRating``, or None for a title nobody rated.

    Returns:
        False for None — "not rated" is not a rating, let alone a human one.
    """
    return value is not None and float(value).is_integer()


def slugify(name: str) -> str:
    """Normalize a username into the slug used in labels: ``shortlist_<slug>``."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "user"


def dedupe_slug(base: str, is_taken: Callable[[str], bool]) -> str:
    """Return ``base``, or ``base_2``, ``base_3``, … — the first that ``is_taken`` reports free.

    Slugs are what row labels are built from and must be unique per owner: two Plex display names
    can slugify alike (Plex names are free text), so the second claimant gets a numeric suffix
    rather than colliding onto the first's label — and their private row.
    """
    slug = base
    n = 2
    while is_taken(slug):
        slug = f"{base}_{n}"
        n += 1
    return slug


@dataclass(frozen=True)
class WatchedItem:
    """One watched title from the user's library, as Plex records it FOR THEM.

    The share-token source reads this straight from the PMS as the user (``unwatched=0``), so one item
    is one distinct TITLE they've watched — carrying Plex's own per-user counts — not one play event.
    ``watch_count`` is therefore the frequency signal (see below) rather than something a caller derives
    by counting duplicate rows.
    """

    title: str
    media_type: MediaType
    watched_at: datetime
    tmdb_id: int | None = None
    year: int | None = None
    rating_key: int | None = None
    completion: float = 1.0  # 0..1 fraction watched
    # How much this title was watched, the frequency half of a seed's weight. For a MOVIE it's the
    # play count (``viewCount``); for a SHOW it's episodes watched (``viewedLeafCount``) — so a show
    # binged 50 episodes deep weighs like 50 movie plays, matching the old per-play behaviour without
    # the source emitting 50 rows. Defaults to 1 so a single-play source reads as one watch.
    watch_count: int = 1
    # A show's per-user watched fraction, straight from Plex (``viewedLeafCount``/``leafCount``) —
    # marks included. The finished-show check reads these directly instead of reconstructing the
    # fraction from play counts. None for movies and for sources that don't report episode totals.
    viewed_leaf_count: int | None = None
    leaf_count: int | None = None
    # For a show watch, the specific episode behind it (the show name is `title`). Display only, never
    # used for seeding. Always None from ShareTokenWatchSource, which reads watched STATE at the show
    # level (viewedLeafCount), not per-episode play events — the recent-watches panel renders these
    # only when present, so it degrades to the show name. A seam for any future per-episode source.
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    # What THIS person rated the title in Plex, 0..10 (5 stars x 2), or None if they haven't rated it
    # — which is 99.7% of watches on a real server, so None is the case to optimise for. Per-account:
    # the share-token read returns the rating belonging to the token it was read with, never the
    # owner's. Only ever a whole number; `is_human_rating` explains why a fractional one is discarded.
    user_rating: float | None = None

    @property
    def is_human_rating(self) -> bool:
        """Whether `user_rating` was plausibly set by a PERSON rather than a tool — see the module
        function of the same name."""
        return is_human_rating(self.user_rating)

    @property
    def is_finished(self) -> bool:
        """Did they finish this, as opposed to merely starting it?

        A MOVIE is finished whenever it is here at all: this type only ever holds titles Plex has
        already flagged watched, and for a movie that flag means played.

        A SERIES needs every episode. That threshold is OURS and has to be — Plex publishes no
        show-level watched flag, only ``viewedLeafCount``/``leafCount`` (recorded:
        ``tests/fixtures/pms_watched_shows.xml.txt``, where a show 2 episodes into 176 comes back as
        "watched"). Of the thresholds available this is the strictest and the least arguable, and it
        is already the wording the user page shows per title ("3 of 12 episodes" / "finished").

        Deliberately NOT the engine's already-seen bar (`rows._watched_titles`, effectively 3
        episodes or 15%): that one answers "engaged enough not to recommend this again?", which is a
        different question with a legitimately looser answer.

        A series whose episode total is unknown reads as UNFINISHED — the opposite of the
        already-seen rule, which counts it as watched rather than risk re-recommending. Here the
        cautious direction is the other way: "we cannot show they finished it" must not become a
        claim that they did.
        """
        if self.media_type is MediaType.MOVIE:
            return True
        if not self.leaf_count:
            return False
        return (self.viewed_leaf_count or 0) >= self.leaf_count


@dataclass(frozen=True)
class Seed:
    """A history title used to seed candidate discovery."""

    tmdb_id: int
    title: str
    media_type: MediaType
    weight: float = 1.0  # recency/frequency weight
    # The two ingredients behind `weight` (weight = watch_count x recency decay), kept so the trace
    # can explain a seed's influence in plain terms ("watched 4x, last 3 days ago") instead of a bare
    # number. Display only — ranking reads `weight`, never these.
    watch_count: int = 1
    recency_days: int = 0  # days between this title's most-recent watch and the newest watch overall


@dataclass
class Candidate:
    """A TMDB-suggested title, later intersected with the library."""

    tmdb_id: int
    title: str
    media_type: MediaType
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    rating: float = 0.0  # TMDB vote_average, 0..10
    vote_count: int = 0  # TMDB vote_count — a 9.0 from 12 votes is noise; the request gate needs both
    # TMDB's own poster path ("/abc.jpg"), free in every list response. Only carried through to the
    # request inbox, which shows the artwork — a delivered pick uses Plex's copy of the title.
    poster_path: str = ""
    # TMDB's synopsis, free in the same list response as the poster and carried the same way: only
    # the request inbox reads it, so the owner can judge an unfamiliar title without leaving the page.
    overview: str = ""
    seeds: list[Seed] = field(default_factory=list)  # every seed that suggested it
    rating_key: int | None = None  # set once matched to the library
    # Which candidate source(s) produced it. Ranking needs this: seedless sources (tmdb_discover,
    # llm_web) would otherwise be crowded out wholesale by the seeded ones — see ranking.pre_rank,
    # which gives each source a fair share of the pool it draws the row from.
    sources: set[str] = field(default_factory=set)
    # How strongly the source that produced it vouched for it, 0..1. TMDB sets this from which
    # endpoint suggested the title and how near the top of that list it sat — the similarity signal
    # that used to be discarded. Sources with no ranking of their own (discover, Trakt, the web
    # source) keep the neutral 1.0: they are deliberate picks, not the tail of a list, and
    # penalising them for lacking a signal they never had is what `pre_rank`'s round-robin exists to
    # prevent. A title several seeds suggested keeps the strongest claim any of them made.
    affinity: float = 1.0

    @property
    def seed_frequency(self) -> int:
        return len(self.seeds)

    @property
    def top_seed(self) -> Seed | None:
        return max(self.seeds, key=lambda s: s.weight) if self.seeds else None


@dataclass(frozen=True)
class Pick:
    """A final ranked recommendation delivered to the user's row.

    `media_type` decides which library the pick's collection lives in. Plex collections belong
    to exactly one library section, and a collection holding items of the wrong type is matched
    by neither `filterMovies` nor `filterTelevision` — so it can never be hidden from other
    users. Delivering a show into a movie collection is therefore a privacy bug, not a cosmetic
    one (SFLIX, 2026-07-12).
    """

    tmdb_id: int
    rating_key: int
    title: str
    rank: int
    reason: str
    media_type: MediaType  # required on purpose: a forgotten default is exactly the bug above
    seed_tmdb_id: int | None = None
    seed_title: str | None = None
    collection_slug: str = ""  # which row produced it, so a user's picks can be grouped per row
    # The library this pick was delivered into. A row targeting >1 library becomes one Plex collection
    # PER library, so effectiveness is tracked per (row, library): `section_key` is the stable Plex
    # section key, `library` its display name ("Movies") for the report label.
    section_key: str = ""
    library: str = ""
    # Which candidate source(s) surfaced this title, and how strongly they vouched for it. Carried
    # all the way to the UI so "why is this here?" is answerable without reading a log: a pick that
    # came from the tail of TMDB's list should LOOK different to one an LLM chose deliberately.
    sources: list[str] = field(default_factory=list)
    affinity: float = 1.0
    # Carried from the Candidate purely so a row can be ORDERED by them (RowSpec.order). Persisted on
    # PickRow, because a carried-forward pick is rebuilt from the DB and would otherwise sort as if it
    # had no rating and no release year. TMDB's vote_average and release year — the only two the
    # candidate pool already holds, so ordering by them costs no extra lookups.
    rating: float = 0.0
    year: int | None = None
    # The `row_recipe` this pick was built under. Compared against tonight's on the next run: a
    # mismatch means the owner changed a setting that decides row contents, so the row rebuilds
    # instead of waiting for its refresh cadence.
    recipe: str = ""


@dataclass
class RowOverride:
    """One person's per-row tweaks. Any None/False field falls through to the row's own settings."""

    muted: bool = False  # this person doesn't get this row at all
    size: int | None = None  # override the row's size for this person
    recent_count: int | None = None  # override how many recent watches the web-search source searches


@dataclass
class UserProfile:
    """Everything the pipeline needs to know about one enabled user."""

    username: str
    plex_account_id: int
    user_type: UserType
    slug: str = ""
    # What a human should be called in a row title: their Shortlist nickname, else the friendly name
    # Tautulli knows them by, else their Plex username. Purely cosmetic — the SLUG (and therefore the
    # `shortlist_<slug>` label every share filter excludes) is derived from the username and never
    # moves, so renaming someone can't strand the exclusions that keep their row private.
    nickname: str = ""
    history: list[WatchedItem] = field(default_factory=list)
    excluded_genres: set[str] = field(default_factory=set)
    blocked_seeds: set[int] = field(default_factory=set)
    row_name_template: str | None = None
    request_tag: str = ""  # tag added to titles requested for this user (layered onto global + row tags)
    # Per-row overrides keyed by collection slug; a slug absent here uses the row's own settings.
    row_overrides: dict[str, RowOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = slugify(self.username)

    @property
    def display_name(self) -> str:
        """What `{user}` renders as — the nickname when they have one, else their Plex username."""
        return self.nickname.strip() or self.username

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}_{self.slug}"


# Shared ("popular on this server") rows live in a namespace no per-person label can collide with.
# `slugify` collapses any run of non-alphanumerics to a SINGLE "_" and strips leading ones, so a
# username can never produce a slug containing "__" — the DOUBLE underscore here makes a shared
# label unreachable from any user slug, so a private row can never be mistaken for a shared one.
SHARED_SLUG_PREFIX = "shared"
SHARED_LABEL_PREFIX = f"{LABEL_PREFIX}__shared_"


@dataclass
class PosterSpec:
    """How a row's Plex collection poster image is produced. Purely cosmetic — a poster never affects
    privacy, promotion, or the leak-safe ordering, and failing to make one never fails a run.

    ``mode`` is "upload" (use ``image`` bytes as-is on every collection of the row) or "generate"
    (compose a prompt from the text fields — which may contain the same ``{user}``/``{library_name}``/
    ``{top_seed}`` placeholders as a row name — and render it with the injected PosterArtist). An empty
    ``mode`` means "leave Plex's own artwork alone".
    """

    mode: str = ""
    image: bytes | None = None  # upload mode: the raw image bytes (the adapter loads them from poster_assets)
    title: str = ""  # generate mode: the headline text
    subtitle: str = ""  # generate mode: secondary text
    style: str = ""  # generate mode: art-style guidance


@dataclass
class RowSpec:
    """One curated-row definition the engine delivers, built by the adapter from a Collection row.

    A per-person spec produces one private row per audience member (label ``shortlist_<userslug>``); a
    shared spec produces one public row for the whole audience (label ``shortlist__shared_<slug>`` —
    the DOUBLE underscore puts it in a namespace no user slug can ever collide with; see
    ``SHARED_LABEL_PREFIX``).
    """

    slug: str
    name_template: str
    size: int
    media: str = "both"  # movie | show | both — the type filter; library_keys narrows to specific libraries
    # Specific Plex library section keys to deliver this row into; empty -> every library of the
    # allowed media type (the default, so a server with one movie + one show library is unchanged).
    library_keys: list[str] = field(default_factory=list)
    shared: bool = False
    # None -> visible to everyone; otherwise the set of plex_account_ids this row is built for / seen by.
    audience: set[int] | None = None
    # Shared rows only: a title must have been watched by at least this many distinct people to
    # qualify, so no one person's solo viewing can reach a public row (aggregate-privacy floor).
    min_watchers: int = 2
    request_tag: str = ""  # tag added to titles requested because they surfaced in this row
    # Whether requests from this row also carry the WANTING PERSON'S slug as a tag, so the owner
    # can tell in Sonarr/Radarr who a title was added for. None -> inherit the global
    # `requests.auto_user_tag`; True/False overrides it for this row alone. Governs only the
    # automatic slug — a tag the owner typed on a person is theirs and is never dropped here.
    auto_user_tag: bool | None = None
    # Per-row override of which discovery sources feed this row; empty -> inherit EngineConfig.candidate_sources.
    candidate_sources: list[str] = field(default_factory=list)
    # Per-row cap on already-watched titles, as a fraction of the row (0.0 = all fresh, 1.0 = no
    # filtering). None -> inherit EngineConfig.watched_pct.
    watched_pct: float | None = None
    # Build a REWATCH row: already-finished titles are what the row is FOR, so they are ordered first
    # and unwatched ones only fill what's left.
    #
    # `watched_pct` cannot express this. It is a CEILING — `_apply_watched_cap` shows unwatched titles
    # first and merely PERMITS up to that fraction of finished ones — so on a library with plenty of
    # unwatched candidates even 1.0 yields a mostly-unwatched row. A row named "Happy to see again"
    # needs the opposite preference, which is this flag.
    rewatch: bool = False
    # Shows only: drop any series this person has STARTED, however little of it. Stricter than the
    # normal watched filter, which only drops shows they have FINISHED (>= watched_show_pct) — one they
    # are three episodes into is otherwise still eligible. This is what makes "a series to start" true.
    # Meaningless for movies (a movie with any view is already finished), so it applies to shows only.
    unstarted_only: bool = False
    # How often this row re-picks its titles, in DAYS: 0 = never once built (frozen), 1 = nightly,
    # N = every N days. None -> inherit EngineConfig.refresh_days.
    refresh_days: int | None = None
    # How much a title's RELEASE DATE counts when ranking it: 0.0 = ignore age, 1.0 = strongly prefer
    # new. None -> inherit EngineConfig.recency.
    #
    # Not the same axis as `refresh_days`, and the pair is the reason the UI label is "Recent
    # releases" rather than "Newness": that is a CADENCE (how often this row
    # re-picks), this is a PREFERENCE (which titles win when it does). A row can rebuild nightly and
    # still fill with 1990s titles — that combination is exactly what this setting exists for.
    recency: float | None = None
    # How many of this person's most recent watched titles the WEB-SEARCH source searches for this row
    # — one cached search per title ("what to watch if you liked X"). Fewer = tighter/cheaper, more =
    # broader reach. Only affects the llm_web source; TMDB/Trakt still use the full seed set. None ->
    # inherit EngineConfig.recent_count.
    recent_count: int | None = None
    # How many of this person's watched titles SEED this row — the titles every source searches from.
    # Unlike recent_count (which only caps the web-search source), this caps the seed set itself, so it
    # decides what the whole row is derived from. Small values make a row about one or two things they
    # actually watched, which is what a `{top_seed}` ("Because you watched X") title claims; the default
    # blends the whole recent history. None -> inherit EngineConfig.max_seeds.
    max_seeds: int | None = None
    # What this row does for someone with too little history to recommend from ("popular" = the
    # cold-start fallback of top-rated titles, "skip" = don't build it for them at all).
    # None -> inherit EngineConfig.cold_start.
    #
    # Per ROW, not just global, because the right answer differs row to row: a `{top_seed}`
    # ("Because you watched X") row has no seed for a cold user and degrades to the bare default
    # title, so skipping it is usually right — while a plain "Picked for You" row is perfectly happy
    # holding popular titles. Deliberately NOT a per-person override: it answers "what is this row
    # for", like `pick_order`, not "how does this person want it".
    cold_start: str | None = None
    # The name to use when `name_template` cannot be rendered — a `{top_seed}` row for someone with
    # no watch behind any of their picks. Empty means the row is NOT built for that person, because
    # the engine never substitutes a name of its own (issue #84).
    #
    # Added LAST, deliberately: several call sites build a RowSpec positionally, so a new field in
    # the middle silently shifts every argument after it. The same hazard bit HubAnchor.anchor_row.
    fallback_name: str = ""
    # This row's own Sonarr/Radarr target and request floors; None -> inherit the global RequestConfig
    # entirely. Grouped into one dataclass rather than a dozen flat fields for the reason directly
    # above. Only per-person rows can carry these: a shared row is built from titles people have
    # already WATCHED, which are by definition already on the server, so it surfaces nothing missing
    # to request (see `_shared_row`, and `_record_demand` being reached only from `_warm_start`).
    request_overrides: RequestOverrides | None = None
    # How many of this person's most recent watches this row may be built from, of which ONE (per media
    # type) is chosen each run — the row cycles a step a day rather than sitting on their newest watch
    # for ever. 1 (the default) is the original behaviour: always the most recent.
    #
    # Distinct from `max_seeds`, which is the axis people reach for first and the wrong one: raising
    # that BLENDS more watches into one row, diluting the very claim a `{top_seed}` title makes. This
    # keeps the row about a single watch and moves WHICH one (issue #57).
    #
    # Per-row with a plain default rather than an inheritable global (like `pick_order`): whether a row
    # is about one fixed watch or a rotation is a property of what that row IS, not a server policy.
    seed_window: int = 1
    # How this row's picks are ORDERED in the delivered collection — "best" (our ranking), "rating"
    # (highest TMDB score first), "newest" (most recent release first), "shuffle" (a different order
    # each day), "new_first" (titles that arrived this run lead) or "rotate" (the front advances by one
    # title a day, so every pick gets a turn there). Plex only sorts a collection by release date,
    # alphabetically, or by the custom order we write, so every one of these is applied here and
    # delivered as that custom order.
    #
    # "new_first" and "rotate" are issue #63's two asks. Both are PRESENTATION, like the rest of this
    # setting: neither changes which titles the row holds or which ones leave it, so neither can be
    # used to make a row cycle faster — that is `refresh_days`, the refresh cadence.
    #
    # Per-row with a plain default rather than an inheritable global (like `media` and `rewatch`, not
    # like `refresh_days`): the right order is a property of what a row IS, so a server-wide default
    # would be a setting nobody sets.
    pick_order: str = "best"
    # Which surfaces the OWNER's own collection appears on: "both" (Home + Library Recommended, the
    # default), "home", "library", or "off" (neither — the Collections tab only, since promote()
    # always browse-hides). "off" is a STRING, not None: `placement_friends=None` already means
    # "inherit", so a None sentinel here would be two different things at once.
    placement: str = "both"
    # The same, for each FRIEND's (shared user's) own collection — "home" means Friends' Home there.
    # None = inherit from `placement` (backward compat); set explicitly to diverge.
    placement_friends: str | None = None
    # Pin the row to the TOP of its library's Recommended shelf (ManagedHub.move). This is a
    # server-wide managed-recommendations order, NOT per-viewing-user — Plex exposes no per-user order.
    pin_top: bool = False
    # Per-library override of where THIS row sits in the Recommended shelf, keyed by section key ->
    # HubAnchor. A library absent here inherits the global default (EngineConfig.hub_anchors); empty
    # -> inherit everywhere. Lets one row anchor differently from the rest (global default + override).
    hub_anchors: dict[str, HubAnchor] = field(default_factory=dict)
    # Optional custom poster for this row's Plex collection(s). None -> leave Plex's own artwork alone.
    poster: PosterSpec | None = None

    @property
    def _effective_friends_placement(self) -> str:
        """Resolved friends placement: explicit override, or inherit from owner placement."""
        return self.placement_friends if self.placement_friends is not None else self.placement

    @property
    def show_home(self) -> bool:
        """The owner sees their OWN row on their Home screen (Plex `promotedToOwnHome`)."""
        return self.placement in ("both", "home")

    @property
    def show_friends_home(self) -> bool:
        """Each friend sees their OWN row on their Home screen (Plex `promotedToSharedHome`)."""
        return self._effective_friends_placement in ("both", "home")

    @property
    def show_owner_library(self) -> bool:
        """The OWNER's own collection sits on its library's Recommended shelf."""
        return self.placement in ("both", "library")

    @property
    def show_friends_library(self) -> bool:
        """Each FRIEND's own collection sits on its library's Recommended shelf.

        Separate from `show_owner_library` because every person gets their OWN collection, so Plex's
        single `promotedToRecommended` flag is set per collection — the owner/friends split is real,
        not cosmetic. Friends only ever see their own row on that shelf (their share filter excludes
        everyone else's), but the OWNER has no share filter to hang an exclude on, so turning this on
        also puts every friend's row on the owner's shelf. Plex limitation, surfaced in the UI.
        """
        return self._effective_friends_placement in ("both", "library")

    @property
    def show_library(self) -> bool:
        """Recommended-shelf flag for a SHARED row — one public collection rather than one per
        person, so there is nothing to split: it shows if either audience asked for it."""
        return self.show_owner_library or self.show_friends_library

    @property
    def label(self) -> str | None:
        """The privacy label for a shared row; per-person rows use the user's own label instead."""
        return f"{SHARED_LABEL_PREFIX}{self.slug}" if self.shared else None


# How much of a show Sonarr takes, as `addOptions.monitor` (MonitorTypes in Sonarr's
# src/NzbDrone.Core/Tv/MonitoringOptions.cs). A SUBSET of that enum, and the cuts are measured rather
# than guessed — every mode below was added to a real Sonarr 4.0.19 and its resulting season/episode
# monitoring read back:
#
#   all 156/162 episodes · firstSeason 26 (season 1) · lastSeason 26 (the last) · pilot 1 · none 0
#
# Left out: `unknown` and `skip` are internal, `latestSeason` is [Obsolete], the two Specials entries
# only toggle season 0 (a Season Pass concern, not "how much of this show do I want"), and
# `future`/`existing`/`recent` all measured 0/162 on a show nobody has yet — `existing` monitors what
# is on disk, `future` what has not aired, `recent` a 90-day window an older show is nowhere near.
# Meaningful in Sonarr's own Season Pass; on the only thing Shortlist ever does — a NEW add — they are
# an obscure spelling of `none`, which says it plainly.
SONARR_MONITOR_MODES = (
    "all",
    "firstSeason",
    "lastSeason",
    "pilot",
    "none",
)


@dataclass(frozen=True)
class ArrTarget:
    """Where and how a Sonarr/Radarr instance should file a newly-requested title."""

    url: str
    api_key: str
    quality_profile_id: int
    root_folder: str
    tag: str = ""  # if set, tag every title Shortlist adds (created in the app if it doesn't exist)


@dataclass
class RequestConfig:
    """Whether — and how conservatively — to ask Sonarr/Radarr for picks the library lacks.

    Off by default and gated on several axes so an LLM's suggestions can never balloon a library.
    A title must clear the rating/vote floors of the chosen ``rating_source`` (a high score from a
    handful of votes is noise), be wanted by at least ``min_demand`` distinct people, fall inside the
    ``min_year``..``max_year`` release window, and even then only the top ``max_per_run`` across the
    whole run are requested.
    """

    enabled: bool = False
    radarr: ArrTarget | None = None  # None -> movie requests are skipped
    sonarr: ArrTarget | None = None  # None -> show requests are skipped
    # Which score gates a title. TMDB is always available (no setup); imdb/trakt/tomatoes/metacritic
    # come from MDBList (needs a key). The min_rating/min_votes floors read from whichever is chosen;
    # every non-TMDB score is normalised to 0..10 so one floor works across sources.
    rating_source: str = "tmdb"  # tmdb | imdb | trakt | tomatoes | metacritic
    mdblist_api_key: str = ""  # required for any non-TMDB source; else rating gating falls back to TMDB
    min_rating: float = 7.0  # rating floor, 0..10, on the chosen source
    min_votes: int = 100  # vote-count floor (audience-vote sources only: imdb/trakt/tmdb)
    min_demand: int = 1  # a title must be wanted by at least this many distinct people
    # Release-year window (a show's year is its first-air year). 0 disables that end of the range.
    min_year: int = 0  # 0 -> no lower bound; else request only titles from >= this year
    max_year: int = 0  # 0 -> no upper bound; else request only titles from <= this year
    max_per_run: int = 5  # hard cap on how many titles a single run may auto-request, total
    # Hybrid tier. A title that also clears these HIGHER bars (within max_per_run) is requested
    # automatically each run; every other title that still cleared the base floors above is queued
    # for the owner to approve by hand. Set auto_send False for a fully manual queue, or set these
    # equal to the base floors for fully automatic requesting (nothing is ever queued).
    auto_send: bool = True
    auto_min_demand: int = 3  # auto-send only titles wanted by at least this many distinct people
    auto_min_rating: float = 8.0  # ...and rated at least this high on the chosen source
    # Tag every request with the wanting person's slug (`moo_house` -> `moo-house`, the Arr charset),
    # so the owner can see IN Sonarr/Radarr who a title was added for — the Requests inbox why-line
    # never reaches the Arr. Off by default. A row may override it either way (`RowSpec.auto_user_tag`),
    # and an explicit per-user tag replaces the slug rather than stacking with it.
    #
    # The tag records who TRIGGERED the add, not everyone who has since wanted the title: a title the
    # Arr already tracks is skipped whole (`clients/arr.py` `add_movie`/`add_series`), tags included.
    auto_user_tag: bool = False
    # Populated by the context builder when a target was connected (URL+key) but incomplete (no
    # profile or folder selected). Surfaces in the run report so the UI can explain the skip.
    incomplete_targets: list[str] = field(default_factory=list)
    # This ROW's own ceiling on how many titles it may contribute to the run, for the allocator.
    # Only ever <= `max_per_run`: the run ceiling is what protects the library from ballooning, so a
    # row may make itself more restrictive and never less (`resolve_request_config` enforces it).
    #
    # None — not 0 — means "inherit the run ceiling". 0 is a REAL choice the UI offers and promises
    # ("this row never asks for anything on its own"), so using it as the unset sentinel handed such
    # a row the FULL run cap: the exact inverse of the control, on a path that adds titles to Radarr.
    max_per_row: int | None = None
    # Which episodes Sonarr monitors — and so searches for — when Shortlist adds a show. Passed
    # straight through as `addOptions.monitor`. "all" is Sonarr's own default and what every add did
    # before this setting existed, so an upgrade changes nothing until somebody picks another.
    # A long-running show on "all" backfills every season the night it is added (issue #100), where a
    # taster ("firstSeason") or a catch-up-from-here ("none", added unmonitored) is often what was meant.
    sonarr_monitor: str = "all"

    def __post_init__(self) -> None:
        if self.max_per_row is None:
            self.max_per_row = self.max_per_run


@dataclass(frozen=True)
class RequestOverrides:
    """One row's per-row request settings. Every field is optional; None/"" means inherit the global.

    Kept as its own dataclass rather than ten more flat ``RowSpec`` fields because several call sites
    build a ``RowSpec`` positionally, so a run of new fields in the middle silently shifts every
    argument after it — the hazard ``RowSpec.fallback_name`` and ``HubAnchor.anchor_row`` both record.
    One optional field appended at the end is safe; ten in the middle are not.

    Deliberately absent: ``enabled``, ``rating_source``, ``mdblist_api_key``, ``max_per_run``, the
    rating-lookup budget and ``tag``. Those are the run's ceilings and its single API account — a row
    that could raise one would turn the owner's global setting into a suggestion.

    The Arr overrides are the FILING choices — profile, root folder, and how much of a show Sonarr
    monitors. URL and API key stay global: the case this serves is one Radarr filing a kids row into
    ``/data/Kids`` at a lower profile, not a second Radarr. Overriding any of them on a row whose
    global target is unconfigured does nothing at all, because there is no URL or key to send to.
    """

    min_rating: float | None = None
    min_votes: int | None = None
    min_demand: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    auto_send: bool | None = None
    auto_min_demand: int | None = None
    auto_min_rating: float | None = None
    max_per_row: int | None = None
    radarr_quality_profile_id: int | None = None
    radarr_root_folder: str | None = None
    sonarr_quality_profile_id: int | None = None
    sonarr_root_folder: str | None = None
    sonarr_monitor: str | None = None


@dataclass(frozen=True)
class RequestWhy:
    """One reason a missing title is in the inbox: a person, the row that surfaced it, and what
    suggested it — so the owner can see exactly how a request got here, not just a bare count.

    ``seed`` is the history title behind it ("because you watched …"); empty for seedless sources
    (tmdb_discover / llm_web). ``source`` is the candidate source that produced it.

    ``row`` is the RENDERED name the user sees ("🎯 Because you watched Bluey"), which is why
    ``row_slug`` exists beside it: the rendered name carries the person's own seed and display name,
    so it identifies nothing stable. Resolving which row's Sonarr/Radarr target a title should be
    sent under — months later, when the owner approves it from the inbox — needs the slug.
    Empty for candidates queued before per-row settings existed; those fall back to the global config.
    """

    user: str
    row: str
    seed: str = ""
    source: str = ""
    row_slug: str = ""


@dataclass
class MissingTitle:
    """A candidate the candidate pool surfaced that no delivery library actually holds yet."""

    tmdb_id: int
    title: str
    media_type: MediaType
    year: int | None
    rating: float  # rating on the chosen source: TMDB vote_average, or the IMDb rating when rating_source="imdb"
    vote_count: int  # vote count on that same source
    demand: int = 1  # distinct users whose candidate pool contained it (multi-person demand ranks higher)
    imdb_id: str = ""  # "tt…" when TMDB has one — lets the inbox deep-link to IMDb instead of a search
    # TMDB poster path ("/abc.jpg") so the inbox can show the artwork. Free from the candidate's own
    # TMDB list response; filled in for the gated shortlist when a non-TMDB source surfaced the title.
    poster_path: str = ""
    # TMDB's synopsis, on the same terms as the poster: free from the candidate's list response, and
    # bought with a detail call only for the gated few a non-TMDB source surfaced. Empty is normal —
    # TMDB has no synopsis for some titles, and the inbox simply omits the paragraph.
    overview: str = ""
    # Per-user + per-row tags to apply on request, layered on top of the target's global tag. Unioned
    # across every user who wanted the title and every row it surfaced in (deduplication merges them).
    tags: set[str] = field(default_factory=set)
    # The usernames whose taste surfaced this title (the "who" behind the demand count) — the inbox
    # shows the names so an owner sees WHY a title is being requested. len(wanters) <= demand, equal
    # when every wanting user has a distinct, non-empty username (the real run always passes one).
    wanters: set[str] = field(default_factory=set)
    # The full provenance: one entry per (person, row) that wanted this title, with the seed/source
    # behind it. Richer than `wanters` (which is just the distinct names) — this answers "which row,
    # and why". Accumulated across every user and row, deduplicated so one (person, row, seed) is
    # listed once.
    why: list[RequestWhy] = field(default_factory=list)
    # Why this title is not on the server yet — either a real send failure ("Sonarr GET …/lookup
    # returned HTTP 503"), so a FAILED auto-send is queued back to the inbox with the reason visible
    # instead of vanishing and silently retrying every night, OR the threshold that kept it waiting
    # ("rating below auto_min_rating (7.5)"). Both answer the inbox's one question; a failure detail
    # outranks a threshold one when merging (`run_persistence._is_failure_detail`).
    detail: str = ""
    # A show's resolved TheTVDB id, cached once (Sonarr keys on TVDB) so the arr-presence check and
    # the eventual send don't each pay a separate TMDB lookup. None until resolved / for movies.
    tvdb_id: int | None = None
    # The arr titleSlug of a sent title, captured at send time so the inbox links straight to its
    # Sonarr/Radarr page. None until sent / for a title that never resolved on the arr.
    arr_slug: str | None = None
    # The row that CLAIMED this title, stamped by the request pass. Distinct from the slugs in `why`,
    # which after `_merge_across_rows` list every row that WANTED it — only one row's target was
    # actually used, and a later approval has to reuse that one.
    row_slug: str | None = None
    # True when the title sits on Sonarr/Radarr's import-exclusion list (from a past delete): it's
    # surfaced for the owner but never auto-sent, since the Arr would refuse it until un-excluded.
    excluded: bool = False


@dataclass
class RequestOutcome:
    """What happened when a single missing title was (or would be) requested."""

    tmdb_id: int
    title: str
    media_type: MediaType
    # requested | would_request | skipped_present | skipped_no_tvdb | skipped_no_target | error
    status: str
    detail: str = ""
    # The arr's own titleSlug (Sonarr/Radarr) for the resolved title, so the inbox can deep-link
    # straight to the series/movie page: Sonarr has no id-based URL, only `/series/<slug>`. None when
    # the arr didn't resolve it (error) or for a source that doesn't report one.
    arr_slug: str | None = None


@dataclass
class RequestReport:
    """Outcome of the whole request pass for one run."""

    considered: int = 0  # titles that cleared the rating/vote thresholds
    # How the run ARRIVED at `considered`, so that a zero can be read. "0 qualifying, 0 auto-sent, 0
    # queued" is the same sentence whether the base floors emptied the pool, the rating gate rejected
    # everything it rated, or the gate ran out of lookup budget before reaching anything good — and
    # for five days in production (2026-08-13..18) it was the third, with nothing anywhere saying so.
    # Reconstructing it afterwards meant diffing settings timestamps against the rating cache by hand.
    wanted: int = 0  # missing titles the run collected at all, BEFORE any floor
    pool_size: int = 0  # titles that cleared the base floors (demand, year) — what the gate was handed
    examined: int = 0  # of those, how many the rating gate actually rated
    lookups_spent: int = 0  # live rating-API calls that cost; cached ratings are free and are not counted
    # The same three, per row slug, plus what each row actually got. A run-wide total cannot answer
    # "why did the kids row send nothing" once every row gates on its own floors and its own share of
    # the lookup budget — which row was starved is exactly the question these exist to answer.
    pool_by_row: dict[str, int] = field(default_factory=dict)
    examined_by_row: dict[str, int] = field(default_factory=dict)
    considered_by_row: dict[str, int] = field(default_factory=dict)
    sent_by_row: dict[str, int] = field(default_factory=dict)
    # What each row CLAIMED, which is not what it sent: a claim can still be skipped at the send (no
    # TheTVDB id, an Arr that refuses it). Claims are what the caps actually decide, so this is the
    # figure that answers "did my row limit bind" — measured live on 2026-08-18, where sent_by_row
    # read picked:3/because:0 while the caps had in fact allocated picked:4/because:1.
    claimed_by_row: dict[str, int] = field(default_factory=dict)
    outcomes: list[RequestOutcome] = field(default_factory=list)
    # Titles handed back for the server to persist as pending so the owner can approve them by hand:
    # those that cleared the base floors but not the auto-send bar (or overflowed max_per_run), PLUS
    # any auto-send that was attempted and ERRORED (each carries its `.detail` so the inbox shows why).
    queued: list[MissingTitle] = field(default_factory=list)
    # The titles actually ASKED FOR this run. The server files these in the inbox as `sent`, which is
    # what stops tomorrow's run re-requesting a title that is merely still downloading — and spending
    # one of `max_per_run` on it every night, forever.
    sent: list[MissingTitle] = field(default_factory=list)
    # Every (tmdb_id, MediaType.value) an Arr already tracks, captured during the arr-state
    # reconcile. The server uses it to drop stale PENDING inbox rows: a title added to an Arr by
    # other means (manual add, another tool, an earlier send that predates the sent-ledger) is not
    # in Plex while it downloads — or ever, if unaired — so the Plex-presence prune never catches
    # it and the row would sit pending forever. Best-effort: empty when the reconcile skipped a
    # media type (none in this run's pool) or an Arr fetch failed (fail-open) — no drops that run.
    arr_present: set[tuple[int, str]] = field(default_factory=set)
    # True when MDBList hit its daily request cap mid-run: the rating gate fell back to TMDB for the
    # rest, and the server raises a notification so the owner knows some ratings weren't the chosen
    # source tonight.
    ratings_rate_limited: bool = False
    # User-facing warnings about the request config itself (e.g. incomplete Arr setup). Surfaced in
    # the run stats so the UI can explain WHY nothing was sent, not just that nothing was sent.
    warnings: list[str] = field(default_factory=list)

    @property
    def requested(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("requested", "would_request"))


@dataclass(frozen=True)
class HubAnchor:
    """Where a library's Shortlist rows should sit in Plex's managed-recommendation shelf: the very
    TOP (``to_top=True``), or right after (``before=False``) / before (``before=True``) either a
    foreign collection (``anchor_title``) or another Shortlist ROW (``anchor_row``, a row slug).
    ``to_top`` ignores both; ``anchor_row`` wins over ``anchor_title`` when both are set.

    ``anchor_row`` is a slug and not a title on purpose. A per-person row is one Plex collection PER
    PERSON — forty accounts means forty collections whose titles differ only by the invisible
    per-account marker — so a title can only ever name ONE person's copy, which is meaningless as
    "put my row after Picked for You". The slug names the row itself, and each library resolves it to
    whichever of its collections are that row's (issue #81).

    Re-applied at the end of every run so a co-managing tool (e.g. Kometa, which can push our rows to
    the bottom of the shelf) can't leave them buried. Only OUR hubs are moved; a FOREIGN anchor is
    read-only. A row anchor is one of ours and therefore also moves — which is why the rows of a
    library are placed in dependency order, never in one block.
    """

    anchor_title: str = ""
    before: bool = False
    to_top: bool = False
    # LAST, and it must stay last: `HubAnchor(title, before, to_top)` is constructed positionally in
    # places, so a new field anywhere earlier silently re-binds their arguments — inserting this one
    # second turned `HubAnchor("Gems Anchor", False)` into a row anchor of `False`.
    anchor_row: str = ""


# The seeded default row title. ``{library_name}`` renders each library's own name at delivery, so a
# multi-library server gets "✨ Movies Picked for You" / "✨ TV Shows Picked for You" — distinct titles,
# which per-person rows REQUIRE (they share one label and are told apart only by title). With no library
# (a preview or a row-level summary) it collapses to DEFAULT_ROW_NAME. Kept in lockstep with
# settings_store's ``row.name_template`` default and web's DEFAULT_ROW_TEMPLATE.
DEFAULT_ROW_TEMPLATE = "✨ {library_name} Picked for You"

# How large a row may be. THE definition — the server's three size validators (`row.size`, a row's
# own `size`, a per-user `row_size`) all import these rather than restating 5 and 40, and web mirrors
# them as ROW_SIZE_MIN/ROW_SIZE_MAX (pinned by tests/unit/test_web_constant_parity.py). Duplicating
# the maximum is how the pool cap below silently stopped clearing it.
MIN_ROW_SIZE = 5
MAX_ROW_SIZE = 40

# The slowest refresh cadence a row may be set to, in days. A VALIDATION bound (reject nonsense),
# not a behaviour cap: the engine handles any period. The old 0..1 freshness fraction was stretched
# onto 1..14 days, so a fortnight was the slowest expressible cadence and a monthly row could not be
# asked for at all — the ceiling is a year now because nothing about the mechanism objects.
MAX_REFRESH_DAYS = 365


@dataclass
class EngineConfig:
    """Static configuration for one engine run (adapters build this from settings)."""

    row_size: int = 15
    row_name_template: str = DEFAULT_ROW_TEMPLATE
    # How many candidates per media type survive the pre-rank cut and are offered to the picker.
    # DERIVED from the row ceiling, never restated: this was a flat 40 while `row.size` was validated
    # up to 40, so at the top of the range the pool and the row were the same size — every surviving
    # candidate had to go in, and a refresh night had nothing spare to swap the weakest third for.
    # Twice the ceiling because that is what a refresh actually needs: it keeps ~2/3 of the row and
    # must find the rest among candidates NOT already in it, with slack left for watched-filtering.
    # Only a sort and a slice over a list already in memory — the gather happened before this, and
    # MDBList ratings are fetched per PICK (rows.py), so a larger pool costs no extra API calls.
    candidates_pre_rank: int = MAX_ROW_SIZE * 2
    # How many of a person's most recent watched titles the web-search source searches per row (one
    # cached Exa search each). Row-overridable via RowSpec.recent_count.
    recent_count: int = 10
    # When True (default), a DISABLED (opted-out) Shortlist user has EVERY shared row hidden too — even
    # public "Popular on this server" rows — so disabling someone removes them from Shortlist entirely.
    hide_shared_from_disabled: bool = True
    min_history: int = 10  # below this -> cold-start row
    # What a cold-start user gets, server-wide: "popular" (a row of the server's top-rated titles) or
    # "skip" (no row built at all, and any row they already have is REMOVED — skipping has to mean
    # gone, or last month's row sits on their Home going stale for ever). Row-overridable via
    # RowSpec.cold_start. Defaults to "popular": the pre-existing behaviour, so an upgrade never
    # silently takes rows away.
    cold_start: str = "popular"
    min_completion: float = 0.7  # history completion threshold for "meaningful" watch
    # How many watched titles seed a row (the most recently watched win, balanced across media types).
    # Row-overridable via RowSpec.max_seeds.
    max_seeds: int = 30
    # Which service's score a row with `pick_order="rating"` sorts on: tmdb (free, already on every
    # candidate) or imdb/trakt/tomatoes/metacritic via MDBList. Deliberately NOT `requests.rating_source`:
    # ordering a row must not depend on whether the request feature is configured at all.
    rating_source: str = "tmdb"
    # Titles that must never seed a SHARED row, server-wide.
    #
    # Per-person blocks deliberately do NOT apply here: a shared row is public, and letting one
    # person's "don't seed this" quietly reshape what everyone else sees would make an individual
    # preference into a server-wide edit nobody else can see or undo.
    blocked_shared_seeds: set[int] = field(default_factory=set)
    # A title someone rated at or below this in Plex (0..10, so 2 = one star, which is also where
    # thumbs-down lands) stops seeding THEIR rows. None = ignore Plex ratings entirely.
    #
    # Like `blocked_shared_seeds` above, and for the same reason, this never applies to a shared row:
    # one person rating a film badly must not remove it from a row everyone else can see. It is
    # applied at the per-person seed derivation only.
    dislike_threshold: float | None = 2.0
    # Cap on already-watched titles in a row, as a fraction of the row. 0.0 (default): all fresh —
    # drop every finished title (a movie you watched, or a show you've seen >= watched_show_pct of;
    # a partly-watched show or one with a new season stays eligible). 1.0: no filtering. Between:
    # at most that fraction of the row may be things already finished. Overridable per row.
    watched_pct: float = 0.0
    # A show watched to >= this fraction of its episodes counts as finished. 0.8, not 0.9: a returning
    # show a person is caught up on sits a few episodes short of 100% (the newest ep just aired, or one
    # was marked-not-played), so 0.9 kept re-recommending shows they've clearly finished — MooHouse's
    # "Deadliest Catch: The Viking Returns" at 8/9 = 89% slipped under the 0.9 bar (2026-07-21). The
    # season-worth floor in `_watched_titles` catches long shows; this catches near-complete short ones.
    watched_show_pct: float = 0.8
    # Refresh cadence in DAYS: 0 (the dataclass default) = frozen, never rebuilt once built; 1 =
    # every night; N = every N days. Overridable per row. `settings_store` defaults the PRODUCT to 8.
    #
    # Was a 0..1 "freshness" fraction stretched onto 1..14 days by a curve, so the stored number
    # described nothing (0.55 meant 7 days), the far end of the scale was a constant duplicated in
    # TypeScript, and no cadence slower than a fortnight was expressible. Migration 0065 converted
    # every stored value through that same curve, so no row's cadence moved.
    #
    # It sets HOW OFTEN a row rebuilds, never how much of it turns over: a refresh keeps the strongest
    # ~two-thirds and swaps the weakest third (`_KEEP_FRACTION`, rows.py), at every cadence including
    # nightly. The old name promised "rotate the whole row daily and reach deep down the ranked list",
    # a magnitude nothing implements — and folding turnover in here would tie more variety to worse
    # picks, since the only way to swap more of a row is to reach further down the ranked list.
    refresh_days: int = 0
    # How much a title's release date counts when ranking it: 0.0 (default) = ignore age entirely,
    # which is how this ranked before the setting existed; 1.0 = every ~8 years of age halves a
    # title's weight. A WEIGHT, never a filter — an old title is only ever asked to be a better
    # match. Overridable per row (see RowSpec.recency for why it is not the refresh cadence).
    #
    # The DATACLASS defaults to 0.0 so a library caller opts in rather than inheriting an opinion.
    # The product does not: `settings_store` defaults `recommendations.recency` to 0.5 for every
    # install, existing servers included, and each row adopts it on its next refresh night.
    recency: float = 0.0
    # Which candidate sources to pool (see engine/candidates.py). Empty/default = TMDB similar only,
    # preserving legacy behaviour; owners widen recall by enabling more.
    candidate_sources: list[str] = field(default_factory=lambda: ["tmdb_similar"])
    # Which backend the llm_web source searches with — exactly one: 'native' (the provider's own
    # web-search tool, Claude/GPT/Gemini only), 'exa', or 'searxng'. Either external is the only path
    # for a local Ollama model. ('auto', which unioned native with an external, was removed in 1.3.)
    web_search_provider: str = "native"
    # Per-library placement of Shortlist's rows in Plex's Recommended shelf, keyed by section key
    # (str). Empty -> leave Plex's default order (rows land wherever they're created — last, under a
    # co-managing tool's collections). Applied at end of run, read-only against the anchor.
    hub_anchors: dict[str, HubAnchor] = field(default_factory=dict)
    # Master switch for touching the Recommended-shelf ORDER. False -> Shortlist never reorders the
    # shelf (skips the whole order phase), so a co-managing tool (agregarr/Kometa) owns the order and
    # the two don't fight. True (default) -> apply the configured anchors. Independent of delivery and
    # promotion — turning it off still delivers and hides rows; it only stops the reordering.
    manage_shelf_order: bool = True
    dry_run: bool = False
    # The curated rows to deliver. Empty -> a single default per-person row synthesized from
    # row_name_template/row_size, so existing callers behave exactly as before.
    rows: list[RowSpec] = field(default_factory=list)
    # Whether the caller MANAGES rows (the server does; direct/legacy engine callers may not). It is the difference
    # between "no rows configured" — synthesize the legacy default — and "every row is switched
    # OFF", which must deliver nothing. Without it, disabling every row in the UI silently rebuilt
    # "✨ Picked for You" for everyone: the Rows page said off, Plex said on.
    rows_defined: bool = False
    # Per-person rows DISABLED in the UI: no longer delivered, but their collections still sit on
    # their owners' Home (the label keeps them excluded from everyone else, so it's not a leak — just
    # "off" that isn't gone). Each is removed like a mute on the next run. Static-titled rows only; a
    # {top_seed} row can't be re-titled without picks, so it's left until the row is re-enabled.
    retired_rows: list[RowSpec] = field(default_factory=list)
    # Sonarr/Radarr requests for picks the library lacks. None -> the feature is entirely off, so
    # no missing-title bookkeeping happens at all (the common case pays nothing for it).
    requests: RequestConfig | None = None
    # Row slugs to actually (re)build this run — a per-row scheduled run only rebuilds its own rows.
    # None = build every row (a full run). Only the DELIVERY loop is scoped: privacy classification,
    # the leak-safe share-filter sync, the unhidable-row sweep, and shelf promotion all still see the
    # FULL `rows` set, so a row not built this run keeps its excludes, its placement, and its privacy.
    build_only: frozenset[str] | None = None
    # True when the caller handed us a SUBSET of the roster ("Run now" for one person) rather than
    # everyone. Shared rows are then not built at all: a "popular on this server" row assembled from
    # whoever happened to be selected is not a server-wide row, and it would be published to
    # everyone. It also stops the engine reporting "only 1 person is in this row's audience — it can
    # never build" about a perfectly healthy 10-person row. Default False = "this IS the roster",
    # the honest reading for a direct library caller.
    users_scoped: bool = False

    def should_build(self, spec: RowSpec) -> bool:
        """Whether this run rebuilds ``spec`` (scoped run) or every row (full run)."""
        return self.build_only is None or spec.slug in self.build_only

    def default_row_spec(self) -> RowSpec:
        """The single default per-person row, synthesized when no rows are configured.

        Its name_template is left empty so it falls through to the per-user override (or config
        default) at delivery — preserving the legacy per-user row-name behaviour.
        """
        return RowSpec(slug="picked", name_template="", size=self.row_size)

    def per_person_rows(self) -> list[RowSpec]:
        """Per-person specs to deliver; a single default row only when rows aren't managed at all."""
        if not self.rows:
            return [] if self.rows_defined else [self.default_row_spec()]
        return [row for row in self.rows if not row.shared]

    def shared_rows(self) -> list[RowSpec]:
        """Shared ('popular on this server') specs to deliver."""
        return [row for row in self.rows if row.shared]


@dataclass
class StageCounts:
    """Per-stage counts surfaced in run reports and SSE progress."""

    history: int = 0
    seeds: int = 0
    candidates: int = 0
    in_library: int = 0
    pre_ranked: int = 0
    picks: int = 0


@dataclass
class CollectionDiff:
    """What delivery changed (or would change, in dry-run) on the user's collections."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # rows destroyed this run (swept, or rebuilt)
    collection_title: str = ""
    created: bool = False
    # The Plex ratingKey of the collection this landed in. The delivery LEDGER's whole point: it is
    # the only stable handle on "which object on the server is this row, for this person, in this
    # library". Titles are not — a `{top_seed}` row renders differently every run, so nothing computed
    # from config can find it later. 0 in a dry run and whenever the PMS didn't hand one back.
    rating_key: int = 0


@dataclass
class OwnedRow:
    """Every Shortlist collection belonging to one user, across libraries.

    A user gets at most one collection per library section (movies, shows), all carrying the
    same `shortlist_<slug>` label — which is what the share-filter excludes key off. The privacy
    check must know about ALL of them: a leak in any library is a leak.
    """

    label: str  # as stored by Plex, which title-cases labels
    rating_keys: list[int] = field(default_factory=list)


@dataclass
class UserRunReport:
    """Outcome of the pipeline for a single user; users never affect each other."""

    username: str
    slug: str
    status: str = "pending"  # pending | ok | cold_start | skipped | error
    picks: list[Pick] = field(default_factory=list)
    counts: StageCounts = field(default_factory=StageCounts)
    diff: CollectionDiff | None = None
    # {"row_slug", "library_key"} for every collection this run DELETED in-run (a muted/retired row, or
    # one a cold start skips). The adapter forgets these ledger entries on persist, the same way the
    # on-demand reconciles call `_forget_deliveries`.
    #
    # Without it the ledger keeps a ratingKey whose collection is gone, and these paths RE-RUN: a cold
    # user is skipped again every night, re-presenting the same dead key for as long as they stay cold.
    # Plex reuses `metadata_items.id`, so that key can come to name a different collection under this
    # same label — and `promote_user_rows` reads the ledger too, so it is not only removals at stake.
    removed_deliveries: list[dict] = field(default_factory=list)
    # Each delivered collection TITLE mapped to the slug of the row that produced it, so the promote
    # phase applies the right row's placement/pin. Recorded per library because a {top_seed} title
    # differs library to library. Transient (not persisted); populated during delivery.
    placement_titles: dict[str, str] = field(default_factory=dict)
    # Per-(row, library) delivery result, so the UI can show "added X to Movies, Y to TV" instead of
    # one merged list. Each entry: row_slug/row_title, library_key/library_title, added/removed/kept/
    # deleted, created, and that library's own ranked picks. Persisted on RunUser.breakdown.
    breakdown: list[dict] = field(default_factory=list)
    privacy_synced: bool = False
    error: str | None = None
    # Why a NON-failing outcome happened — set alongside `skipped`, never for an error. "Skipped"
    # with no explanation sent a beta user hunting for a bug that wasn't there (issue #3): a shared
    # row with one enabled user can never reach its 2-watcher floor, and nothing on screen said so.
    # Distinct from `error` because the UI counts every non-null `error` as a failed user.
    reason: str | None = None
    duration_s: float = 0.0
    # Total AI tokens this user cost this run — the llm_web source (web-search title discovery) is
    # the only thing that spends them now.
    llm_tokens: int = 0
    # The same total split by WHERE it went: {"llm_web": N}. Lets the UI answer "what did the AI
    # actually spend tokens on" per person, not just a lump sum.
    llm_tokens_by_step: dict[str, int] = field(default_factory=dict)
    # Exa web searches run for this user (the llm_web external backend). Tracked apart from tokens:
    # Exa bills per search request, not per token, so the two must never be summed together.
    exa_searches: int = 0
    # Searches served from the shared 14-day cache instead of billed. Reported alongside exa_searches
    # so a fully-cached run reads "1 searched · N from cache", not a bare "1" that looks like nothing ran.
    exa_cache_hits: int = 0
    # A per-user, JSON-serializable record of the whole pipeline — seeds derived, each source's queries
    # and returns, the LLM/Exa prompts, and the ranked pool — so the UI can show "exactly what happened
    # for this person" without re-running anything. Purely diagnostic; the engine never reads it back.
    # {} when tracing produced nothing (a skipped/cold user). Persisted on RunUser.trace.
    trace: dict = field(default_factory=dict)
    # Every per-person row and what this run decided about it FOR THIS PERSON, as
    # ``{row_slug: "due" | "not_due" | "muted" | "not_in_audience"}``.
    #
    # `reason` says why somebody built nothing as one sentence for the whole person, which cannot be
    # attributed to a row — so a rows-first view had no way to put a skipped person under the rows
    # they were skipped for, and the largest group on a run page fell outside the tree entirely.
    # Recorded for EVERY user, not just skipped ones, so the tree is complete for a successful run too.
    #
    # "due" is intent, not outcome: it says this run meant to build the row, and the person's own
    # `status` says what became of it. Naming it "built" would claim a success that a later error in
    # the pipeline can still take away. {} on a cold-start skip, which never reaches the decision.
    rows_considered: dict[str, str] = field(default_factory=dict)
    # Seconds spent on work EVERY row shares — the watch-history fetch and the candidate gather.
    # All AI spend happens here (see `pool_costs`), so on a typical person this dwarfs the rows.
    # Reported as its own line rather than divided between rows, which would invent a split.
    setup_s: float = 0.0
    # Per-row cost keyed by row slug: {"duration_s": wall clock, "blocked_s": of which, waiting on
    # the shared Plex write lock}. duration_s INCLUDES blocked_s; work time is the difference.
    # At concurrency 1 blocked_s is always ~0; at 8 it is what explains a row that looks slow.
    row_timing: dict[str, dict[str, float]] = field(default_factory=dict)
    # One entry per candidate-pool COMPUTATION: {"label", "tokens", "exa_searches", "duration_s",
    # "rows": [slug, ...]}. Pools are memoised per `pool_key` and usually shared by every row, so
    # `rows` is what lets the UI say "one pool, used by both rows" instead of splitting the tokens.
    pool_costs: list[dict] = field(default_factory=list)
    # INTERNAL cursor, never persisted: which row `_timed_lock` charges write-lock waits to.
    # None means setup, whose wait is already inside `setup_s`.
    lock_bucket: str | None = None


@dataclass
class RunReport:
    """Aggregate outcome of one engine run."""

    started_at: datetime
    finished_at: datetime | None = None
    dry_run: bool = False
    users: list[UserRunReport] = field(default_factory=list)
    # Rows deleted because Plex could not hide them, keyed by the slug that owned them. Kept at
    # run level because the sweep covers the whole SERVER: a leaking row belonging to a paused or
    # disabled user is still a leaking row, and nobody would ever see it in a per-user report.
    swept_rows: dict[str, list[str]] = field(default_factory=dict)
    # Labels of rows the converge phase pulled off the OWNER's Home because this run's promote could
    # not reach them (their user is paused, disabled, deselected, errored — or the row was promoted
    # by an older build). Run level for the same reason as the sweep: these people are by definition
    # absent from the user list, so a per-user report would never show it (plex-safety rule 10).
    converged: list[str] = field(default_factory=list)
    # Labels of collections DELETED because Shortlist no longer knows the user they belong to.
    # Separate from `converged` because this is the one irreversible action converge takes, and
    # "what was destroyed at 03:31" must be answerable on its own (plex-safety rule 10).
    orphans_removed: list[str] = field(default_factory=list)
    # Share filters we changed, keyed by plex account id. Editing someone's Plex share permissions
    # is the most sensitive write Shortlist makes, and most of the accounts we write to are not in
    # any run's user list — so without this, "what changed on whose share at 03:31" would have no
    # answer for them at all (plex-safety rule 10).
    filter_writes: dict[int, dict] = field(default_factory=dict)
    # Managed-recommendation shelf reorders applied this run, one per library actually moved (a title
    # anchor + the row titles moved). Empty when no anchors are configured or everything was already
    # in place — a run-level audit of a server-wide Plex write (plex-safety rule 10).
    hub_orderings: list[dict] = field(default_factory=list)
    # Sonarr/Radarr requests made (or, in dry-run, that would be made) for picks the library lacks.
    # None when the feature is off — distinct from an empty report (on, but nothing qualified).
    requests: RequestReport | None = None
    # (tmdb_id, media_type) the delivery libraries now hold. Lets the server prune inbox candidates
    # that have since arrived on the server (bought/grabbed elsewhere) so they stop lingering.
    library_present: set[tuple[int, MediaType]] = field(default_factory=set)
    error: str | None = None  # a run-level failure (e.g. the sweep itself could not run)
    # Why promotion was blocked this run, one entry per account whose share filter could not be
    # written — the accounts a row would otherwise be visible to. Without these the operator sees
    # only "promotion skipped — a privacy sync failed", which names neither the account nor the
    # reason and sends them to the container logs (issue #1, mrjohnpoz).
    promotion_blockers: list[str] = field(default_factory=list)
    # {username: [ratingKey, ...]} — rows this account can SEE that are not its own, on an account
    # Plex will not accept a hide-list for. Not a blocker: nothing we do can hide these, so stopping
    # the run would punish everyone for one account. It is reported instead, because an exposure the
    # owner is not told about is the actual failure (see privacy.unhidden_rows_visible_to).
    unhideable_rows: dict[str, list[int]] = field(default_factory=dict)
    # {plex account id: why} — accounts the owner asked us to LEAVE ALONE whose excludes we could not
    # actually take back off. Not a blocker (failing to remove an exclude leaves them more private
    # than asked, never less), but it is a state change the owner made that did not reach Plex, and
    # §12's whole register is that shape. `pipeline._leave_sharing_alone` fills it.
    left_alone_failures: dict[int, str] = field(default_factory=dict)
    # {username: [ratingKey, ...]} — accounts whose share filter Shortlist DID write, that can still
    # see other people's rows. The read-back proves plex.tv STORED our exclusions; this asks whether
    # Plex ACTS on them.
    #
    # NOTE (2026-08-18): this field reached `dev` inside a per-row-requests commit by mistake — the
    # first commit of that branch staged the whole of models.py while the maintainer's privacy work
    # was uncommitted in the same file. Its producer and consumer live on that in-flight branch, so
    # in `dev` alone the field is currently written and read by nobody. It is left in place because
    # removing it breaks that working tree; it becomes live when the privacy work lands.
    filters_not_enforced: dict[str, list[int]] = field(default_factory=dict)
    # Whether the enforcement spot-check actually RAN. Without it an empty result is ambiguous — "we
    # looked and every account was clean" and "we never got that far" are the same empty dict — so the
    # alert could never clear itself: written only when non-empty, one bad night pinned an
    # undismissable red card through every clean run after it. Same shape as `unhideable_measured`.
    filters_enforcement_measured: bool = False
    # Whether this run actually GOT AS FAR AS looking. An empty `unhideable_rows` is ambiguous on its
    # own — "we checked and nobody is exposed" and "we died in the sweep phase" produce the same
    # dict — and the readers treat the latest measuring run as the truth. Without this flag a run
    # that failed early cleared a live exposure alert and every "Sees N rows of others'" badge while
    # the exposure was untouched, which is the exact silence the check exists to end.
    unhideable_measured: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and all(u.status != "error" for u in self.users)


@dataclass(frozen=True)
class FilterSnapshot:
    """A user's plex.tv share filters, captured before Shortlist's first mutation."""

    plex_account_id: int
    username: str
    taken_at: datetime
    filters: dict[str, str]  # filterAll/filterMovies/filterTelevision/filterMusic/filterPhotos
