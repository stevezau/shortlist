"""Engine dataclasses: inputs, intermediate stages, and run reports."""

from __future__ import annotations

import re
import unicodedata
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


def slugify(name: str) -> str:
    """Normalize a username into the slug used in labels: ``rowarr_<slug>``."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "user"


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
    seeds: list[Seed] = field(default_factory=list)  # every seed that suggested it
    rating_key: int | None = None  # set once matched to the library

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


@dataclass
class UserProfile:
    """Everything the pipeline needs to know about one enabled user."""

    username: str
    plex_account_id: int
    user_type: UserType
    slug: str = ""
    history: list[WatchedItem] = field(default_factory=list)
    excluded_genres: set[str] = field(default_factory=set)
    max_rating: str | None = None
    row_size: int | None = None  # None -> engine default
    row_name_template: str | None = None

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = slugify(self.username)

    @property
    def label(self) -> str:
        return f"rowarr_{self.slug}"


@dataclass
class EngineConfig:
    """Static configuration for one engine run (adapters build this from settings)."""

    row_size: int = 15
    row_name_template: str = "✨ Picked for You"
    label_prefix: str = "rowarr"
    candidates_pre_rank: int = 40  # heuristic pre-rank keeps this many for the curator
    min_history: int = 10  # below this -> cold-start row
    min_completion: float = 0.7  # history completion threshold for "meaningful" watch
    max_seeds: int = 30
    staleness_runs: int = 3  # don't repeat picks recommended in the last N runs
    dry_run: bool = False


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
    """Every Rowarr collection belonging to one user, across libraries.

    A user gets at most one collection per library section (movies, shows), all carrying the
    same `rowarr_<slug>` label — which is what the share-filter excludes key off. The privacy
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
    # is the most sensitive write Rowarr makes, and most of the accounts we write to are not in
    # any run's user list — so without this, "what changed on whose share at 03:31" would have no
    # answer for them at all (plex-safety rule 10).
    filter_writes: dict[int, dict] = field(default_factory=dict)
    error: str | None = None  # a run-level failure (e.g. the sweep itself could not run)

    @property
    def ok(self) -> bool:
        return self.error is None and all(u.status != "error" for u in self.users)


@dataclass(frozen=True)
class FilterSnapshot:
    """A user's plex.tv share filters, captured before Rowarr's first mutation."""

    plex_account_id: int
    username: str
    taken_at: datetime
    filters: dict[str, str]  # filterAll/filterMovies/filterTelevision/filterMusic/filterPhotos


@dataclass
class PrivacyCheckResult:
    """Outcome of a T1/T2 verification pass."""

    tier: str
    passed: bool
    detail: dict[str, object] = field(default_factory=dict)
