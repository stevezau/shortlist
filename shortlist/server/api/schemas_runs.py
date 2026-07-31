"""Response models for the runs router and the effectiveness report derived from them.

These models exist to DESCRIBE the payloads, never to filter them. Both routers were annotated
`-> dict` / `-> list[dict]`, so the OpenAPI schema published `{[key: string]: unknown}` and the SPA
had to hand-write its own response interfaces — which `.claude/rules/frontend.md` forbids, and which
had already drifted from the server in six places.

Every model here therefore inherits :class:`PassthroughModel`; read its docstring before adding one.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from shortlist.server.api.schemas import PassthroughModel


class PassthroughModel(PassthroughModel):
    """Base for every response model in this module: documents a payload without filtering it.

    A *default* Pydantic response model silently DROPS any key it does not declare — a field
    forgotten here would simply stop arriving, only in production, and blank out part of a page with
    no error anywhere. ``extra="allow"`` makes that impossible: undeclared keys pass through
    untouched, so these models can only ever add documentation, never remove data.

    Which means: **never** declare a response model in this module on a bare ``BaseModel``.
    """


# --- runs -----------------------------------------------------------------------------


class PickOut(PassthroughModel):
    """One delivered recommendation, as the run detail lists it."""

    rank: int
    title: str
    reason: str
    seed_title: str | None  # the watched title that produced it, when the pipeline knows it
    sources: list[str]  # candidate-source ids; [] on picks written before provenance existed
    affinity: float | None  # 0..1, how near the top of the suggesting source's list it sat


class RunUserOut(PassthroughModel):
    """One person's outcome in a run. Users who haven't finished yet appear with the same keys and
    `status: "pending"`, so the shape does not change mid-run."""

    username: str
    display_name: str  # nickname → Tautulli friendly name → username
    slug: str
    status: str
    error: str | None
    reason: str | None  # why a `skipped` result happened — an explanation, not a failure
    duration_ms: int | None  # null while the user is still pending
    llm_tokens: int
    llm_tokens_by_step: dict[str, int]
    exa_searches: int
    #: The engine's own blobs, deliberately left as open maps: their keys belong to the engine, they
    #: have gained fields several times, and legacy runs carry fewer of them.
    diff: dict[str, Any]
    picks: list[PickOut]
    breakdown: list[dict[str, Any]]
    has_trace: bool


class RunSummaryOut(PassthroughModel):
    """One run, as the Runs list shows it."""

    id: int
    trigger: str
    started_at: str | None
    finished_at: str | None
    status: str
    dry_run: bool
    #: Engine counters (`users_ok`, `titles_added`, …). An open map for the same reason as `diff`.
    stats: dict[str, Any]
    error: str | None  # why the run failed, when the failure belongs to no single person
    promotion_blockers: list[str]  # accounts whose share filter Plex refused


class RunDetailOut(RunSummaryOut):
    """A run plus every person in it."""

    users: list[RunUserOut]


class RunsSummaryOut(PassthroughModel):
    """Totals for the Runs page header."""

    total: int
    ok: int
    error: int
    last_finished: str | None
    last_status: str | None


class TraceRequestOut(PassthroughModel):
    """What became of one wanted-but-missing title, keyed `"<tmdb_id>:<media_type>"` on the trace."""

    status: str  # pending | sent | rejected
    detail: str
    arr_slug: str | None
    excluded: bool  # on the arr's import-exclusion list — approving is a no-op


class RunUserTraceOut(PassthroughModel):
    """The full pipeline trace for one user in one run."""

    username: str
    display_name: str
    status: str
    error: str | None
    reason: str | None
    #: The engine's trace blob and the delivered per-(row, library) ending. Open shapes on purpose —
    #: the trace's stages are the engine's and have changed several times.
    trace: dict[str, Any]
    breakdown: list[dict[str, Any]]
    requests: dict[str, TraceRequestOut]


class RunLogLineOut(PassthroughModel):
    """One line of a run's activity log.

    Everything but `seq` is optional because the live in-memory tail hands back exactly what the
    progress hook pushed, key for key — only the durable `run_log_lines` read fills every field in.
    `seq` is the exception: the buffer's sink stamps it on the way in, so it is always there.
    """

    seq: int  # monotonic per-run counter; the dedupe key (several lines share a millisecond)
    ts: str | None = None
    run_id: int | None = None
    user: str = ""  # a user slug, or "Shortlist" for the server-wide phases
    stage: str = ""
    #: A tally the UI renders as "113 history · 40 seeds". Values are numbers for every counter and
    #: strings for the label-ish ones (`row`), so it stays an open map.
    counts: dict[str, Any] = Field(default_factory=dict)
    #: Only emitted when there is something to say — null on the lines that have nothing.
    reason: str | None = None


class RunCreatedOut(PassthroughModel):
    run_id: int


class RunCancelledOut(PassthroughModel):
    cancelling: bool


class RunsDeletedOut(PassthroughModel):
    deleted: int


# --- effectiveness report -------------------------------------------------------------


class LandingOut(PassthroughModel):
    """The landing rate over a matured cohort — picks old enough to have had their full 30 days."""

    delivered: int
    watched: int
    rate: float | None
    cohort_from: str | None
    cohort_to: str | None
    matured_days: int


class OverallOut(PassthroughModel):
    """Headline counts for the window, with the change vs the previous equal period."""

    delivered: int
    watched: int
    watched_prev: int | None  # null on the `all` window, which has no previous period
    watched_delta: int | float | None
    avg_days_to_watch: float | None
    avg_days_to_watch_delta: int | float | None
    landing: LandingOut


class WatchSyncOut(PassthroughModel):
    last: str | None
    next: str | None


class CoverageOut(PassthroughModel):
    """Who is actually covered. `users_enabled`/`rows_enabled` describe the server as it is NOW and
    are deliberately not windowed."""

    users_enabled: int
    users_total: int
    users_with_picks: int
    users_watched: int
    users_watched_delta: int | float | None
    rows_enabled: int


class ReportRunsOut(PassthroughModel):
    total: int  # all-time: it is the odometer
    in_window: int
    in_window_delta: int | float | None
    last_finished: str | None
    last_status: str | None
    errors_last: int


class ReportRequestsOut(PassthroughModel):
    sent: int
    pending: int
    watched_after_sent: int  # requests that paid off: asked for, then watched


class TrendPointOut(PassthroughModel):
    week: str  # "%Y-%W"
    watched: int


class PerUserOut(PassthroughModel):
    username: str
    display_name: str
    slug: str
    delivered: int
    watched: int


class PerRowOut(PassthroughModel):
    """One (row, library) line. A row targeting >1 library is one Plex collection per library."""

    slug: str
    section_key: str
    library: str
    name: str
    deleted: bool  # history from a row that no longer exists
    delivered: int
    watched: int


class TopTitleOut(PassthroughModel):
    tmdb_id: int
    media_type: str
    title: str
    watchers: int


class RecentWatchOut(PassthroughModel):
    username: str
    display_name: str
    title: str
    media_type: str
    row: str
    library: str
    seed_title: str
    watched_at: str | None


class EffectivenessReportOut(PassthroughModel):
    """The dashboard tracking report for one window."""

    window: str
    window_days: int | None  # null on the `all` window
    since: str | None
    first_pick: str | None  # the oldest pick on record — lets the UI explain a flat window selector
    overall: OverallOut
    watch_sync: WatchSyncOut
    coverage: CoverageOut
    runs: ReportRunsOut
    requests: ReportRequestsOut
    trend: list[TrendPointOut]
    per_user: list[PerUserOut]
    per_row: list[PerRowOut]
    top_titles: list[TopTitleOut]
    recent: list[RecentWatchOut]


class DeletedRowOut(PassthroughModel):
    """Pick history belonging to a row that no longer exists."""

    slug: str
    picks: int
    first_seen: str | None
    last_seen: str | None


class ClearedDeletedRowsOut(PassthroughModel):
    cleared: int
    picks: int
    slugs: list[str]


class SyncStartedOut(PassthroughModel):
    started: bool
