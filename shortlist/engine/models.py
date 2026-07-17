"""Engine dataclasses: inputs, intermediate stages, and run reports."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum


class MediaType(StrEnum):
    MOVIE = "movie"
    SHOW = "show"


class UserType(StrEnum):
    OWNER = "owner"
    SHARED = "shared"
    MANAGED = "managed"


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
    """One meaningful watch from the user's history."""

    title: str
    media_type: MediaType
    watched_at: datetime
    tmdb_id: int | None = None
    year: int | None = None
    rating_key: int | None = None
    completion: float = 1.0  # 0..1 fraction watched


@dataclass(frozen=True)
class Seed:
    """A history title used to seed candidate discovery."""

    tmdb_id: int
    title: str
    media_type: MediaType
    weight: float = 1.0  # recency/frequency weight


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
    seeds: list[Seed] = field(default_factory=list)  # every seed that suggested it
    rating_key: int | None = None  # set once matched to the library
    # Which candidate source(s) produced it. Ranking needs this: seedless sources (tmdb_discover,
    # llm_library, llm_web) would otherwise be crowded out wholesale by the seeded ones — see
    # ranking.pre_rank, which gives each source a fair share of the pool it hands the curator.
    sources: set[str] = field(default_factory=set)

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


@dataclass
class PromptConfig:
    """User-tunable curation instructions for the LLM.

    The fixed output contract (JSON schema, "use only provided titles", the reason-length cap) is
    enforced in code regardless of these values, so any tone/guidance/template here is safe: it can
    steer taste and wording but can never produce an unavailable title or leak history.
    """

    tone: str = "balanced"  # a TONE_PRESETS key
    guidance: str = ""  # free-text extra instructions injected into the system prompt
    template: str = ""  # full custom system prompt; empty -> built-in skeleton
    shared: bool = False  # True -> aggregate ("popular on this server") framing, no "because you watched"


def overlay_prompt(base: PromptConfig | None, over: PromptConfig | None) -> PromptConfig | None:
    """Lay ``over``'s SET fields on top of ``base``. A blank field means INHERIT.

    Used twice, with the same meaning both times: a row's recipe over the global one, and one
    person's override over their row's. Guidance is additive (the house note plus the specific one),
    matching how the global+per-user recipe has always resolved; tone and template are replacements.

    Blank-means-inherit is the whole point. When an override replaced the recipe wholesale, setting
    just the tone for one person silently wiped that row's guidance and custom prompt.
    """
    if over is None:
        return base
    if base is None:
        return over
    return replace(
        base,
        tone=over.tone or base.tone,
        guidance="\n".join(part for part in (base.guidance, over.guidance) if part),
        template=over.template or base.template,
    )


@dataclass
class RowOverride:
    """One person's per-row tweaks. Any None/False field falls through to the row's own settings."""

    muted: bool = False  # this person doesn't get this row at all
    size: int | None = None  # override the row's size for this person
    prompt: PromptConfig | None = None  # override the row's curation recipe for this person


@dataclass
class UserProfile:
    """Everything the pipeline needs to know about one enabled user."""

    username: str
    plex_account_id: int
    user_type: UserType
    slug: str = ""
    history: list[WatchedItem] = field(default_factory=list)
    excluded_genres: set[str] = field(default_factory=set)
    row_name_template: str | None = None
    prompt: PromptConfig | None = None  # resolved effective recipe; None -> built-in defaults
    request_tag: str = ""  # tag added to titles requested for this user (layered onto global + row tags)
    # Per-row overrides keyed by collection slug; a slug absent here uses the row's own settings.
    row_overrides: dict[str, RowOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = slugify(self.username)

    @property
    def label(self) -> str:
        return f"shortlist_{self.slug}"


# Shared ("popular on this server") rows live in a namespace no per-person label can collide with.
# `slugify` collapses any run of non-alphanumerics to a SINGLE "_" and strips leading ones, so a
# username can never produce a slug containing "__" — the DOUBLE underscore here makes a shared
# label unreachable from any user slug, so a private row can never be mistaken for a shared one.
SHARED_SLUG_PREFIX = "shared"
SHARED_LABEL_PREFIX = "shortlist__shared_"


@dataclass
class RowSpec:
    """One curated-row definition the engine delivers, built by the adapter from a Collection row.

    A per-person spec produces one private row per audience member (label ``shortlist_<userslug>``); a
    shared spec produces one public row for the whole audience (label ``shortlist_shared_<slug>``).
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
    # Per-collection recipe. None on the default 'picked' row -> use the per-user prompt on the
    # profile (the Phase A global+per-user tuning), so that behaviour is preserved exactly.
    prompt: PromptConfig | None = None
    # Shared rows only: a title must have been watched by at least this many distinct people to
    # qualify, so no one person's solo viewing can reach a public row (aggregate-privacy floor).
    min_watchers: int = 2
    request_tag: str = ""  # tag added to titles requested because they surfaced in this row
    # Per-row override of which discovery sources feed this row; empty -> inherit EngineConfig.candidate_sources.
    candidate_sources: list[str] = field(default_factory=list)
    # Per-row cap on already-watched titles, as a fraction of the row (0.0 = all fresh, 1.0 = no
    # filtering). None -> inherit EngineConfig.watched_pct.
    watched_pct: float | None = None
    # How much this row varies day to day, as a fraction: 0.0 = stable (same strong picks daily,
    # best quality), 1.0 = fresh (rotate the whole row + reach deep for novelty). None -> inherit
    # EngineConfig.freshness.
    freshness: float | None = None
    # Where the row's collection appears once promoted: "both" (Home + Library Recommended, the
    # default and legacy behaviour), "home" (Home only), or "library" (Library Recommended only).
    placement: str = "both"
    # Pin the row to the TOP of its library's Recommended shelf (ManagedHub.move). This is a
    # server-wide managed-recommendations order, NOT per-viewing-user — Plex exposes no per-user order.
    pin_top: bool = False
    # Per-library override of where THIS row sits in the Recommended shelf, keyed by section key ->
    # HubAnchor. A library absent here inherits the global default (EngineConfig.hub_anchors); empty
    # -> inherit everywhere. Lets one row anchor differently from the rest (global default + override).
    hub_anchors: dict[str, HubAnchor] = field(default_factory=dict)

    @property
    def show_home(self) -> bool:
        return self.placement in ("both", "home")

    @property
    def show_library(self) -> bool:
        return self.placement in ("both", "library")

    @property
    def label(self) -> str | None:
        """The privacy label for a shared row; per-person rows use the user's own label instead."""
        return f"{SHARED_LABEL_PREFIX}{self.slug}" if self.shared else None


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
    # Which score gates a title: TMDB (always available, no setup) or IMDb (needs an OMDb key). The
    # min_rating/min_votes floors read from whichever source is chosen.
    rating_source: str = "tmdb"  # "tmdb" | "imdb"
    omdb_api_key: str = ""  # required when rating_source == "imdb"; else IMDb gating falls back to TMDB
    min_rating: float = 7.0  # rating floor, 0..10, on the chosen source
    min_votes: int = 100  # vote-count floor on the chosen source
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


@dataclass(frozen=True)
class RequestWhy:
    """One reason a missing title is in the inbox: a person, the row that surfaced it, and what
    suggested it — so the owner can see exactly how a request got here, not just a bare count.

    ``seed`` is the history title behind it ("because you watched …"); empty for seedless sources
    (tmdb_discover / llm_library / llm_web). ``source`` is the candidate source that produced it.
    """

    user: str
    row: str
    seed: str = ""
    source: str = ""


@dataclass
class MissingTitle:
    """A candidate the curator's pool surfaced that no delivery library actually holds yet."""

    tmdb_id: int
    title: str
    media_type: MediaType
    year: int | None
    rating: float  # rating on the chosen source: TMDB vote_average, or the IMDb rating when rating_source="imdb"
    vote_count: int  # vote count on that same source
    demand: int = 1  # distinct users whose candidate pool contained it (multi-person demand ranks higher)
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


@dataclass
class RequestOutcome:
    """What happened when a single missing title was (or would be) requested."""

    tmdb_id: int
    title: str
    media_type: MediaType
    # requested | would_request | skipped_present | skipped_no_tvdb | skipped_no_target | error
    status: str
    detail: str = ""


@dataclass
class RequestReport:
    """Outcome of the whole request pass for one run."""

    considered: int = 0  # titles that cleared the rating/vote thresholds
    outcomes: list[RequestOutcome] = field(default_factory=list)
    # Cleared the base floors but not the auto-send bar (or overflowed max_per_run): not requested,
    # handed back for the server to persist so the owner can approve them by hand.
    queued: list[MissingTitle] = field(default_factory=list)
    # The titles actually ASKED FOR this run. The server files these in the inbox as `sent`, which is
    # what stops tomorrow's run re-requesting a title that is merely still downloading — and spending
    # one of `max_per_run` on it every night, forever.
    sent: list[MissingTitle] = field(default_factory=list)

    @property
    def requested(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("requested", "would_request"))


@dataclass(frozen=True)
class HubAnchor:
    """Where a library's Shortlist rows should sit in Plex's managed-recommendation shelf: the very
    TOP (``to_top=True``), or right after (``before=False``) / before (``before=True``) an existing
    collection matched by ``anchor_title``. ``to_top`` ignores ``anchor_title``.

    Re-applied at the end of every run so a co-managing tool (e.g. Kometa, which can push our rows to
    the bottom of the shelf) can't leave them buried. Only OUR hubs are moved; the anchor is read-only.
    """

    anchor_title: str = ""
    before: bool = False
    to_top: bool = False


@dataclass
class EngineConfig:
    """Static configuration for one engine run (adapters build this from settings)."""

    row_size: int = 15
    row_name_template: str = "✨ Picked for You"
    label_prefix: str = "shortlist"
    candidates_pre_rank: int = 40  # heuristic pre-rank keeps this many for the curator
    min_history: int = 10  # below this -> cold-start row
    min_completion: float = 0.7  # history completion threshold for "meaningful" watch
    max_seeds: int = 30
    staleness_runs: int = 3  # don't repeat picks recommended in the last N runs
    # Cap on already-watched titles in a row, as a fraction of the row. 0.0 (default): all fresh —
    # drop every finished title (a movie you watched, or a show you've seen >= watched_show_pct of;
    # a partly-watched show or one with a new season stays eligible). 1.0: no filtering. Between:
    # at most that fraction of the row may be things already finished. Overridable per row.
    watched_pct: float = 0.0
    watched_show_pct: float = 0.9  # a show watched to >= this fraction of its episodes counts as finished
    # Day-to-day variability, as a fraction: 0.0 (default) = stable (the strongest picks every day);
    # 1.0 = fresh (rotate the whole row daily and reach deep down the ranked list). Overridable per row.
    freshness: float = 0.0
    # Which candidate sources to pool (see engine/candidates.py). Empty/default = TMDB similar only,
    # preserving legacy behaviour; owners widen recall by enabling more.
    candidate_sources: list[str] = field(default_factory=lambda: ["tmdb_similar"])
    # How the llm_web source searches: 'native' (the provider's own web-search tool), 'exa' (the
    # external search provider — the only path for Ollama), or 'auto' (native where supported, else Exa).
    web_search_provider: str = "auto"
    # Per-library placement of Shortlist's rows in Plex's Recommended shelf, keyed by section key
    # (str). Empty -> leave Plex's default order (rows land wherever they're created — last, under a
    # co-managing tool's collections). Applied at end of run, read-only against the anchor.
    hub_anchors: dict[str, HubAnchor] = field(default_factory=dict)
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
    duration_s: float = 0.0
    llm_tokens: int = 0


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
