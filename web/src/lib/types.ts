// TODO: hand-written for now. Replace with types generated from the backend's
// OpenAPI schema (`pnpm -C web gen:api`) as soon as the FastAPI app ships one —
// per .claude/rules/frontend.md, request/response types must be generated, not
// hand-written. Keep this file byte-for-byte in sync with the API until then.

export type UserType = "owner" | "shared" | "managed";

/** GET /api/users — one row per Plex user Shortlist knows about. */
export interface User {
  id: number;
  username: string;
  slug: string;
  /** The owner's override for what to call them in a row title; "" = fall back. */
  nickname?: string;
  /** What Tautulli calls them, when it has its own name for them. The blank-nickname fallback. */
  friendly_name?: string;
  /** nickname → friendly_name → username, resolved server-side. */
  display_name?: string;
  user_type: UserType;
  restricted: boolean;
  /** Plex parental preset on a managed account: "little_kid" | "older_kid" | "teen", "" for none.
   *  `restricted` is true for BOTH — only this says whether Plex actually hides anything. */
  restriction_profile?: string;
  enabled: boolean;
  cold_start: boolean;
  history_depth: number;
  last_run_at: string | null;
  /** Tag added in Sonarr/Radarr to titles requested for this user (layered onto the global + row tags). */
  request_tag: string;
  /** 0..1 fraction of recommended items watched within 30 days, or null before first measurement. */
  hit_rate: number | null;
  /** A few of their most recent pick titles, for the dashboard card's preview strip. */
  preview_titles?: string[];
  /** Saved per-user overrides — the same shape PATCH accepts. */
  prefs?: UserPrefs;
}

/**
 * Which of Plex's two surfaces one side of a row claims: the Home screen, the library's Recommended
 * shelf, both, or neither. Held once per audience (`placement` / `placement_friends`) because every
 * person gets their own Plex collection, so each flag is set per collection.
 */
export type Placement = "both" | "home" | "library" | "off";

/** A curated-row definition (GET/POST/PATCH /api/collections). */
export interface Collection {
  id: number;
  slug: string;
  name: string;
  /** The most recent run that built this row, for a "last run" link; null until it's ever run. */
  last_run_id: number | null;
  build: "per_person" | "shared";
  audience: "everyone" | "subset";
  audience_user_ids: number[];
  enabled: boolean;
  /** This row's own run schedule as a cron string; "" = never runs on a schedule (manual only). */
  schedule: string;
  size: number;
  media: "movie" | "show" | "both";
  sort_order: number;
  name_template: string;
  min_watchers: number;
  /** Tag added in Sonarr/Radarr to titles requested because they surfaced in this row. */
  request_tag: string;
  /** Per-row discovery sources; [] inherits the global candidates.sources setting. */
  candidate_sources: string[];
  /** Specific Plex library section keys to build in; [] = every library of the row's media type. */
  library_keys: string[];
  /** Max fraction of the row that may be already-watched (0..1); null inherits the global cap. */
  watched_pct: number | null;
  /** Day-to-day variability (0..1); null inherits the global freshness. */
  freshness: number | null;
  /** Recent watches the web-search source searches (1..25); null inherits the global recent_count. */
  recent_count: number | null;
  /** Watched titles this row is built FROM (1..100); null inherits the engine default (30). */
  max_seeds: number | null;
  /** Surfaces the OWNER's own collection appears on; "off" = neither (Collections tab only). */
  placement: Placement;
  /** Surfaces each FRIEND's own collection appears on; "home" means Friends' Home here. */
  placement_friends: Placement;
  /** Pin the row to the top of its library's Recommended shelf (server-wide, not per viewer). */
  pin_top: boolean;
  /** Per-library Recommended-shelf override for THIS row; {} inherits the global default. */
  hub_anchor: HubAnchorMap;
  /** This row's custom poster; mode "" leaves Plex's own artwork alone. */
  poster: Poster;
}

/** A poster mode. "" = Plex default, "upload" = your image, "text" = built-in renderer, "ai" = image
 *  model. "generate" is the legacy name for "ai", still returned for rows saved before the split. */
export type PosterMode = "" | "upload" | "text" | "ai" | "generate";

/** A row's custom collection poster (as returned by the API — never the image bytes). */
export interface Poster {
  mode: PosterMode;
  /** Text-poster fields; support {user}/{library_name}/{top_seed} placeholders. */
  title: string;
  subtitle: string;
  style: string;
  /** True when an image is viewable for this row (uploaded, a text poster, or a cached AI one). */
  has_image: boolean;
}

/** Poster fields sent on save (no image bytes — those go through the upload endpoint). */
export interface PosterInput {
  mode: Exclude<PosterMode, "generate">;
  title: string;
  subtitle: string;
  style: string;
}

/** Whether the configured AI provider can generate images (GET /api/system/image-provider). */
export interface ImageProviderStatus {
  capable: boolean;
  provider: string;
  reason: string;
}

/** Where a row sits in a library's Recommended shelf, keyed by library (section) key. A `top` entry
 *  means the very top; otherwise `anchor` places it after/before that collection. */
export type HubAnchorMap = Record<
  string,
  { anchor?: string; before?: boolean; top?: boolean }
>;

/** A Plex library on the server (GET /api/system/libraries). */
export interface PlexLibrary {
  key: string;
  title: string;
  type: "movie" | "show";
}

/** One shortlist-labelled collection found on Plex by the cleanup audit. */
export interface OwnedCollection {
  library: string;
  title: string;
  label: string;
  rating_key: number;
  kind: "user" | "shared";
  slug: string;
  /** Its user (per-person) or shared row is gone from the app — drift a cleanup would remove. */
  orphan: boolean;
}

/** GET /api/system/owned-collections — the cleanup audit result. */
export interface OwnedCollectionsAudit {
  collections: OwnedCollection[];
  total: number;
  orphans: number;
}

/** Body for POST / PATCH /api/collections. */
export interface CollectionInput {
  name: string;
  build: "per_person" | "shared";
  audience: "everyone" | "subset";
  audience_user_ids: number[];
  enabled: boolean;
  /** This row's own run schedule as a cron string; "" = never runs on a schedule (manual only). */
  schedule: string;
  size: number;
  media: "movie" | "show" | "both";
  sort_order: number;
  name_template: string;
  min_watchers: number;
  request_tag: string;
  candidate_sources: string[];
  library_keys: string[];
  watched_pct: number | null;
  freshness: number | null;
  recent_count: number | null;
  max_seeds: number | null;
  placement: Placement;
  placement_friends: Placement;
  pin_top: boolean;
  hub_anchor: HubAnchorMap;
  poster: PosterInput;
}

/** PATCH /api/users/{id} — per-user overrides. */
export interface UserPrefs {
  row_name_tpl?: string;
  row_size?: number;
  excluded_genres?: string[];
  /** Bare ints on installs that predate the richer shape; records since. Read with `blockedSeeds`. */
  blocked_seeds?: (number | BlockedSeed)[];
  max_rating?: string | null;
  paused?: boolean;
}

export interface UserPatch {
  nickname?: string;
  enabled?: boolean;
  request_tag?: string;
  prefs?: UserPrefs;
}

export type RunTrigger = "schedule" | "manual" | "wizard";

/** Owner API-token status. The token is revealable (stored encrypted at rest), so the owner-gated
 *  endpoint returns it in plaintext for the owner to unhide/copy — like Sonarr/Radarr's key. */
export interface ApiTokenStatus {
  enabled: boolean;
  created_at: string | null;
  token: string | null;
}

/** The response to generating a token. */
export interface ApiTokenCreated {
  token: string;
  created_at: string;
}

export interface RunStats {
  users_ok: number;
  users_error: number;
  /** Built nothing, but nothing went wrong (no row was due for them). Absent on legacy runs. */
  users_skipped?: number;
  /** Titles added to rows across all users this run. */
  titles_added?: number;
  /** Titles rotated out of rows across all users this run. */
  titles_removed?: number;
  /** Titles requested from Sonarr/Radarr this run (0 when requests are off). */
  titles_requested?: number;
  /** Warnings about incomplete Arr config (e.g. missing quality profile or root folder). */
  requests_warnings?: string[];
  /** Total AI tokens this run cost (curate + the AI candidate sources). Absent on legacy runs. */
  llm_tokens?: number;
  /** That total split by where it went: { curate, llm_web, llm_library }. */
  llm_tokens_by_step?: Record<string, number>;
  /** Exa web searches run this run (billed per search, not per token — shown separately). */
  exa_searches?: number;
  /** Searches served from the shared 14-day cache instead of billed — "1 searched · N from cache". */
  exa_cache_hits?: number;
}

/** GET /api/runs — one row per pipeline run. */
export interface Run {
  id: number;
  trigger: RunTrigger;
  started_at: string;
  finished_at: string | null;
  status: string;
  dry_run: boolean;
  stats: RunStats;
  /** Why the run failed, when the failure belongs to no single person. Null on a clean run. */
  error?: string | null;
  /** Accounts whose share filter Plex refused — the reason nothing was promoted. */
  promotion_blockers?: string[];
}

export interface Pick {
  rank: number;
  title: string;
  reason: string;
  /** Which watched title produced this pick, when the pipeline knows it. */
  seed_title?: string;
  /** Candidate source ids that surfaced this pick; empty on picks written before provenance existed. */
  sources?: string[];
  /** 0..1, how near the top of the suggesting source's list it sat. 1.0 also means "unranked source". */
  affinity?: number;
  media_type?: string;
  /** Which row this pick belongs to (Collection slug). */
  collection_slug?: string;
}

/** GET /api/users/{id}/rows — one row this user gets, with their override and latest picks. */
export interface UserRow {
  collection_id: number;
  slug: string;
  name: string;
  media: string;
  /** Which Plex library this card represents (e.g. "Movies", "TV Shows"). Empty for legacy/single-lib rows. */
  library: string;
  section_key: string;
  size: number;
  /** The row's effective recent-watches depth (its own, else the global) — what an override falls back to. */
  recent_count: number;
  is_default: boolean;
  muted: boolean;
  override: {
    row_size: number | null;
    recent_count: number | null;
  };
  picks: Pick[];
}

/** PUT /api/users/{id}/rows/{collection_id} body. */
export interface RowOverridePatch {
  muted?: boolean;
  row_size?: number | null;
  recent_count?: number | null;
}

/** GET /api/users/{id}/runs — one of this user's recent run results. */
export interface UserRunSummary {
  run_id: number;
  started_at: string | null;
  finished_at: string | null;
  /** THIS person's outcome in the run: ok | cold_start | skipped | error | pending. Not the run's. */
  status: string;
  error: string | null;
  /** Why a non-failing outcome happened ("no watch history yet"). Blank when there's nothing to say. */
  reason: string;
  duration_ms: number | null;
  /** The run's own status, for when the run failed around this person rather than because of them. */
  run_status: string | null;
  dry_run: boolean;
  diff: RunDiff;
  picks: Pick[];
}

/** Alias kept short for the components that render one line of this. */
export type UserRun = UserRunSummary;

/** GET /api/users/{id}/runs/summary — how many runs included this person, of how many exist. */
export interface UserRunsCount {
  included: number;
  total: number;
}

/** GET /api/users/{id}/history — one recent watch. */
export interface WatchItem {
  title: string;
  media_type: string;
  watched_at: string;
  year: number | null;
  /** For a show watch, the specific episode (title is the show name). Null for movies. */
  season: number | null;
  episode: number | null;
  episode_title: string | null;
}

/**
 * A user's collection diff. Every field is optional: the API returns `{}` for a user the run
 * left alone (no picks produced, so no row was touched), not a diff of three empty lists.
 */
export interface RunDiff {
  added?: string[];
  removed?: string[];
  kept?: string[];
  /** Rows deleted because Plex could not hide them (wrong type for their library). */
  deleted?: string[];
}

/** One (row, library) slice of a user's run result: what changed in that library + its own picks. */
export interface RunLibraryBreakdown {
  row_slug: string;
  row_title: string;
  library_key: string;
  library_title: string;
  added: string[];
  removed: string[];
  kept: string[];
  deleted: string[];
  created: boolean;
  picks: Pick[];
  /** AI tokens the curate call for this (row, library) cost. Absent on legacy runs. */
  llm_tokens?: number;
}

/** Per-user slice of GET /api/runs/{id}. */
export interface RunUserResult {
  username: string;
  /** nickname → friendly_name → username, resolved server-side (same as User.display_name). */
  display_name?: string;
  slug: string;
  status: string;
  error: string | null;
  /** Why a `skipped` result happened, in plain English. Null unless skipped (and on legacy runs). */
  reason: string | null;
  duration_ms: number;
  llm_tokens: number;
  /** This user's AI tokens split by where they went: { curate, llm_web, llm_library }. */
  llm_tokens_by_step?: Record<string, number>;
  /** Exa web searches run for this user (billed per search, not per token). */
  exa_searches?: number;
  diff: RunDiff;
  picks: Pick[];
  /** Per-(row, library) breakdown; empty on legacy runs (render the merged diff + picks instead). */
  breakdown: RunLibraryBreakdown[];
  /** Whether a full pipeline trace was recorded for this user (fetch it from the trace endpoint). */
  has_trace?: boolean;
}

/** GET /api/runs/{id} — the run plus its per-user results. */
export interface RunDetail extends Run {
  users: RunUserResult[];
}

/** One recent watch shown in a trace. */
export interface TraceWatch {
  title: string;
  media: string;
  /** Display name of the Plex library it lives in ("" when unknown — fall back to a media label). */
  library: string;
  year: number | null;
  watched_at: string | null;
}

/** A seed derived from history — a history title used to find candidates. */
export interface TraceSeed {
  title: string;
  media: string;
  /** Display name of the Plex library it lives in ("" when unknown — fall back to a media label). */
  library: string;
  tmdb_id: number;
  weight: number;
  /** The two ingredients behind `weight` — so the influence bar reads "watched 4×, 3 days ago". */
  watch_count?: number;
  recency_days?: number;
}

/** What happened to a candidate a source returned: kept into the pool, or dropped and why. */
export type TraceFate =
  | "kept"
  | "already_watched"
  | "not_in_your_libraries"
  | "excluded_genre"
  | "lost_ranking_cutoff"
  | "not_returned";

/** One title a source returned for a seed, tagged with its fate through selection. */
export interface TraceReturn {
  tmdb_id: number;
  title: string;
  /** Kept/dropped verdict (absent on legacy runs recorded before disposition tracking). */
  fate?: TraceFate;
}

/** One seed's query against a source: what it searched for and a sample of what came back. */
export interface TraceSeedQuery {
  seed: string;
  media: string;
  returned: TraceReturn[];
  /** Total returned before the `returned` sample was capped — so the UI can say "+N more". */
  total: number;
}

/** One candidate source's contribution in a gather. */
export interface TraceSource {
  source: string;
  status: "ok" | "failed";
  contributed: number;
  detail: string;
  /** Per-seed query sample (seeded TMDB/Trakt sources only; empty for discover/llm_web). */
  queries?: TraceSeedQuery[];
  /** Fate tally across this source's returned sample: {kept, already_watched, ...} counts. */
  disposition?: Record<string, number>;
}

/** One Exa search: the query sent for a seed and the titles it returned. */
export interface TraceWebSearch {
  seed: string;
  query: string;
  cached: boolean;
  returned: string[];
}

/** One title the AI proposed from the web search, resolved to a real TMDB id, tagged with whether it
 *  made the library's shortlist (kept) or the reason it fell out — the same fate a TMDB/Trakt return
 *  carries. Hallucinations (no TMDB match) never reach here; they stay in `unresolved`. */
export interface TraceWebProposal {
  title: string;
  tmdb_id: number;
  media: string;
  fate?: TraceFate;
}

/** The web-search (llm_web) detail of a gather: what was searched and what the LLM proposed. */
export interface TraceWeb {
  mode: string;
  searches?: TraceWebSearch[];
  rag_system?: string;
  rag_user?: string;
  proposed?: string[];
  native_proposed?: string[];
  resolved?: string[];
  unresolved?: string[];
  /** Resolved proposals with their fate through selection — kept into the row, or why they dropped. */
  proposals?: TraceWebProposal[];
}

/** One candidate pool a user's rows gathered (usually one, shared across rows). */
export interface TraceGather {
  pool: string;
  sources?: TraceSource[];
  discover_genres?: Record<string, string[]>;
  web?: TraceWeb;
}

/** What the request subsystem did with a wanted-but-missing title (Sonarr/Radarr). Overlaid onto a
 *  "not in your libraries" fate so a drop reads "→ requested from Radarr" instead of a dead end. */
export interface TraceRequestOutcome {
  /** pending = queued for the owner's approval; sent = asked of Sonarr/Radarr; rejected = dismissed. */
  status: "pending" | "sent" | "rejected";
  /** The send outcome, or why it's queued. */
  detail: string;
  /** Sonarr/Radarr titleSlug once sent, for a deep link (null when queued/before this was recorded). */
  arr_slug: string | null;
  /** On the arr's import-exclusion list — approving it is a no-op until the owner clears it. */
  excluded: boolean;
}

/** The full pipeline trace for one user in one run (GET /api/runs/{id}/users/{uid}/trace). */
export interface RunUserTrace {
  history?: {
    total: number;
    recent: TraceWatch[];
    watched_movies: number;
    watched_shows: number;
    /** True distinct-title watched totals per library NAME, split by media type — exact per library
     *  even when several libraries share a media type. Absent on runs recorded before this was added. */
    watched_by_library?: Record<string, { movie: number; show: number }>;
  };
  seeds?: TraceSeed[];
  gathers?: TraceGather[];
}

/** GET /api/runs/{id}/users/{uid}/trace response. */
export interface RunUserTraceResponse {
  username: string;
  display_name?: string;
  status: string;
  /** Why the run failed for this person (null unless status is "error"). */
  error: string | null;
  /** Plain-English reason a non-failing person was skipped (null otherwise). */
  reason: string | null;
  trace: RunUserTrace;
  /** The delivered ending: per-(row, library) picks with reasons. [] on legacy runs. */
  breakdown: RunLibraryBreakdown[];
  /** What the request subsystem did with each wanted-but-missing title, keyed "<tmdb_id>:<media_type>"
   *  — the trace overlays it onto "not in your libraries" drops. {} when requests are off/legacy. */
  requests?: Record<string, TraceRequestOutcome>;
}

/** POST /api/runs body. */
export interface RunRequest {
  user_ids?: number[];
  /** Scope the run to specific rows (omit = every row). */
  collection_ids?: number[];
  dry_run?: boolean;
}

export interface RunCreated {
  run_id: number;
}

/** GET /api/settings — free-form until the schema is generated. */
export type Settings = Record<string, unknown>;

/** POST /api/settings/test/{service} response. */
export interface ConnectionTestResult {
  ok: boolean;
  message: string;
}

export type TestableService =
  | "plex"
  | "tautulli"
  | "tmdb"
  | "llm"
  | "radarr"
  | "sonarr"
  | "mdblist"
  | "trakt"
  | "exa";

/** GET /api/settings/arr/{service}/options — dropdown data for a connected Sonarr/Radarr. */
export interface ArrOptions {
  quality_profiles: { id: number; name: string }[];
  root_folders: { id: number; path: string }[];
}

/** POST /api/system/uninstall response (also returned for dry-run previews). */
export interface UninstallResult {
  filters_restored: number;
  collections_deleted: string[];
  /** Rows switched off so the next scheduled run can't rebuild what uninstall removed. */
  rows_disabled: number;
  dry_run: boolean;
  message: string;
}

/** POST /api/collections/{id}/cleanup — remove a row's collections from Plex. */
export interface CleanupResult {
  removed: string[];
  dry_run: boolean;
  message: string;
}

// --- Auth (Plex PIN login) ---

/** POST /api/auth/pin. */
export interface PinCreated {
  id: number;
  code: string;
  client_id: string;
}

/**
 * GET /api/auth/pin/{id}. The Plex token is deliberately NOT here: the backend holds it
 * server-side for the setup session, so an XSS anywhere in this UI cannot steal it.
 */
export interface PinStatus {
  linked: boolean;
  account_id?: number;
  username?: string;
}

/** GET /api/auth/session. */
export interface Session {
  authenticated: boolean;
  /**
   * Does this instance have anything worth protecting yet — a linked server, OR a Plex token
   * seeded from the environment? If not, the wizard opens without a login; connecting Plex is
   * step 1, and it is what claims the instance.
   */
  login_required: boolean;
  account_id?: number;
  username?: string;
}

// --- Setup wizard ---

/** POST /api/setup/probe body. */
export interface ProbeRequest {
  plex_url: string;
  tautulli_url?: string;
  tautulli_apikey?: string;
}

export interface ProbeCheck {
  ok: boolean;
  message: string;
  value?: string;
}

export interface LibrarySection {
  key: string;
  title: string;
  type: string;
  count: number;
}

/** POST /api/setup/probe response. */
export interface ProbeResult {
  checks: {
    pms_version: ProbeCheck;
    plex_pass: ProbeCheck;
    libraries: ProbeCheck;
    tautulli?: ProbeCheck;
  };
  machine_id: string;
  server_name: string;
  owner_account_id: number;
  libraries: LibrarySection[];
}

/** POST /api/setup/link body. */
export interface LinkRequest {
  plex_url: string;
  machine_id: string;
  server_name: string;
  version: string;
  owner_account_id: number;
  plex_pass: boolean;
}

/** GET/PUT /api/setup/state — wizard progress, persisted per step change. */
export interface SetupState {
  step: number;
  state: Record<string, unknown>;
  completed: boolean;
}

// --- SSE payloads (GET /api/events) ---

/** Event `run.user.stage`. */
export interface RunUserStageEvent {
  user: string;
  stage: string;
  counts: Record<string, number>;
  /** Why this user was skipped — kept out of `counts`, which is a tally of numbers. */
  reason?: string | null;
  /** Present on run-scoped stage events; lets a run page ignore other runs' events. */
  run_id?: number | null;
  /** ISO timestamp the server stamped the stage, when available. */
  ts?: string | null;
}

/** One line of a run's activity log (GET /api/runs/{id}/log + the SSE stage stream). */
export interface RunLogEntry {
  /** Monotonic per-run counter stamped server-side. The dedup key: several lines land in the same
   *  millisecond, so a timestamp alone collapsed "1/5" and "2/5" into one entry. Absent on entries
   *  from a server that predates it. */
  seq?: number;
  ts?: string | null;
  run_id?: number | null;
  /** A user slug, or "Shortlist" for the server-wide phases. */
  user: string;
  stage: string;
  counts: Record<string, number | string>;
  reason?: string | null;
}

/** Event `run.finished`. */
export interface RunFinishedEvent {
  run_id: number;
  status: string;
  /** On failure, the reason so the UI can show it inline. */
  error?: string | null;
}

/** One live step streamed while a real uninstall runs (SSE `uninstall.progress`). */
export interface UninstallProgressEvent {
  /** Human-readable line for the live log, e.g. "Restored Sarah's share filter". */
  label: string;
  /** For filter-restore steps: how many done out of the total. */
  done?: number;
  total?: number;
}

/** Which Tools-page sync a `sync.*` event belongs to. */
export type SyncKind = "watched" | "users";

/**
 * Live progress for a Tools-page sync (SSE `sync.progress`).
 *
 * The watched sync is one determinate loop (`done`/`total` users). The users sync has two phases:
 * an indeterminate `fetch` (the opaque plex.tv round-trip), then a determinate `save` bar.
 */
export interface SyncProgressEvent {
  kind: SyncKind;
  /** Only the users sync sends phases; the watched sync is a single implicit "save" loop. */
  phase?: "fetch" | "save";
  done?: number;
  total?: number;
}

/** A Tools-page sync finished (SSE `sync.finished`). */
export interface SyncFinishedEvent {
  kind: SyncKind;
  ok: boolean;
  /** watched sync: how many users were refreshed. */
  count?: number;
  /** users sync: the same counts the POST returns, echoed so the bar can settle on them. */
  added?: number;
  updated?: number;
  total?: number;
  /** On failure (watched sync), the exception class name — never a tokened message (rule 9). */
  error?: string | null;
}

/**
 * A server plex.tv says this account can reach, with every advertised address already tried
 * from where Shortlist actually runs — only the owner's network knows which one works.
 */
export interface PlexServer {
  name: string;
  machine_id: string;
  owned: boolean;
  version: string;
  connections: {
    uri: string;
    local: boolean;
    relay: boolean;
    ok: boolean;
  }[];
}

/** GET /api/requests — one wanted-but-missing title in the Sonarr/Radarr approval inbox. */
/** The dashboard effectiveness report — did delivered picks get watched? */
export interface AppNotification {
  id: string;
  severity: "info" | "warning" | "error";
  title: string;
  body: string;
  action_url: string;
  action_label: string;
  dismissable: boolean;
}

export interface RunsSummary {
  total: number;
  ok: number;
  error: number;
  last_finished: string | null;
  last_status: string | null;
}

/** Report windows, in days. "all" is lifetime. */
export type ReportWindow = "7" | "30" | "90" | "all";

export interface EffectivenessReport {
  window: ReportWindow;
  /** null for "all". */
  window_days: number | null;
  /** Start of the window (ISO), null for "all". */
  since: string | null;
  overall: {
    /** Picks DELIVERED in the window. */
    delivered: number;
    /** Picks WATCHED in the window — not the same set as `delivered`, deliberately: a pick
     *  delivered last month and watched this week is a watch this week. Counts, never a ratio. */
    watched: number;
    watched_prev: number | null;
    watched_delta: number | null;
    avg_days_to_watch: number | null;
    avg_days_to_watch_delta: number | null;
    /** The one surviving ratio, over a MATURED cohort: picks delivered in the window that are also
     *  old enough to have had their full `matured_days` to be watched. Numerator and denominator
     *  describe the same picks, which is what makes it mean anything. */
    landing: {
      delivered: number;
      watched: number;
      rate: number | null;
      cohort_from: string | null;
      cohort_to: string;
      matured_days: number;
    };
  };
  /** The daily watch-status sync: when it last ran and next fires (ISO), so the report reads as live. */
  watch_sync: { last: string | null; next: string | null };
  coverage: {
    /** Current server state, deliberately NOT windowed — "3 of 11 people" only reads if 11 is now. */
    users_enabled: number;
    users_total: number;
    /** People delivered a pick in the window. */
    users_with_picks: number;
    /** People who watched a pick in the window. */
    users_watched: number;
    users_watched_delta: number | null;
    rows_enabled: number;
  };
  runs: {
    /** All-time odometer. */
    total: number;
    in_window: number;
    in_window_delta: number | null;
    last_finished: string | null;
    last_status: string | null;
    errors_last: number;
  };
  requests: { sent: number; pending: number; watched_after_sent: number };
  top_titles: {
    tmdb_id: number;
    media_type: string;
    title: string;
    watchers: number;
  }[];
  trend: { week: string; watched: number }[];
  /** Counts for the window, sorted by watched desc. No per-person rate: at these sample sizes a
   *  percentage is noise dressed as precision, and sorting by it put 1/31 above 3/103. */
  per_user: {
    username: string;
    /** nickname → friendly_name → username, resolved server-side. */
    display_name?: string;
    slug: string;
    delivered: number;
    watched: number;
  }[];
  /** One line per (row × library): a row targeting >1 library is a separate Plex collection in each.
   *  `section_key` disambiguates rows sharing a slug. */
  per_row: {
    slug: string;
    section_key: string;
    library: string;
    name: string;
    /** History from a row that has since been deleted — `name` is its slug, all it has left. */
    deleted?: boolean;
    delivered: number;
    watched: number;
  }[];
  recent: {
    username: string;
    /** nickname → friendly_name → username, resolved server-side. */
    display_name?: string;
    title: string;
    media_type: string;
    row: string;
    library: string;
    seed_title: string;
    watched_at: string | null;
  }[];
}

export interface RequestCandidate {
  id: number;
  tmdb_id: number;
  media_type: "movie" | "show";
  title: string;
  year: number | null;
  /** "tt…" when known — the inbox deep-links to IMDb; "" falls back to an IMDb search. */
  imdb_id: string;
  /** TMDB poster path ("/abc.jpg"); "" when TMDB has no artwork. The UI picks the size bucket. */
  poster_path: string;
  /** Rating on the chosen source (TMDB, or IMDb when that source is selected). */
  rating: number;
  vote_count: number;
  /** Distinct people whose picks wanted it. */
  demand: number;
  /** Per-user + per-row tags recorded when queued; applied in Sonarr/Radarr on send. */
  tags: string[];
  /** The usernames whose picks wanted it — the "who" behind the demand count. */
  wanters: string[];
  /** Per (person, row) provenance: which row wanted it and why (the seed behind it). */
  why: RequestWhy[];
  status: "pending" | "sent" | "rejected";
  /** Send outcome, or why it's queued. */
  detail: string;
  /** On Sonarr/Radarr's import-exclusion list (usually a past delete) — approving is a no-op until
   *  the owner removes the exclusion there. */
  excluded: boolean;
  /** The arr's titleSlug, captured at send time — lets the sent log deep-link straight to the
   *  Sonarr/Radarr page. Null for items sent before this was recorded (falls back to the arr home). */
  arr_slug: string | null;
  /** When this row last changed state — the "sent at" for a sent item. */
  updated_at: string | null;
}

/** One reason a missing title is in the inbox: a person, the row that wanted it, and what suggested it. */
export interface RequestWhy {
  user: string;
  row: string;
  /** The history title behind it ("because you watched …"); "" for seedless sources. */
  seed: string;
  /** The candidate source that produced it (tmdb_similar, trakt, llm_web, …). */
  source: string;
}

/** One title's result from POST /api/requests/send. */
export interface RequestSendOutcome {
  id: number;
  title: string;
  status: string;
  detail: string;
}

/** POST /api/requests/send response. */
export interface RequestSendResult {
  sent: number;
  dry_run: boolean;
  outcomes: RequestSendOutcome[];
}

/** One parsed line from the rotating log file (GET /api/system/logs). A traceback is folded into
 *  the entry it belongs to, so `message` can span several lines. */
export interface LogLine {
  ts: string | null;
  level: string;
  source: string;
  message: string;
}

export interface LogPage {
  lines: LogLine[];
  /** How many lines matched the filter before the newest-N cap was applied. */
  total_matched: number;
  truncated: boolean;
  /** The file these came from, or null when the instance has not written any logs yet. */
  file: string | null;
}

/** Sync schedule info for the Tools page — when each sync last ran and next fires. */
export interface SyncsInfo {
  watched: { last: string | null; next: string | null; cron: string };
  users: { last: string | null; next: string | null; cron: string };
  backup: { next: string | null; cron: string; max_keep: number };
}

export interface Backup {
  name: string;
  size_bytes: number;
  created_at: string;
}

export interface VersionInfo {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
  install_type: string;
}

/** One background maintenance job (GET /api/system/jobs). Runs are NOT jobs — they have their own page. */
export interface Job {
  id: number;
  kind: string;
  /** queued -> running -> done | failed. `queued` after a failure means it will be retried. */
  status: "queued" | "running" | "done" | "failed";
  attempts: number;
  max_attempts: number;
  /** Human-readable outcome, e.g. "Checked every row; corrected 3". */
  detail: string;
  error: string | null;
  /** What the job was asked to do (a slug, a row). Data by design, never a secret — kinds that
   *  touch tokens read them from the settings store at run time. */
  payload?: Record<string, unknown>;
  /** Structured outcome, e.g. `{fixed: [...], orphans: [...]}`. */
  result?: Record<string, unknown>;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** GET /api/system/jobs/catalog — one entry per job kind Shortlist knows how to run.
 *
 *  The Jobs page is organised by JOB, not by chronology: "is the roster sync healthy?" can't be
 *  answered from a flat list of the last 25 rows with every kind mixed together. */
export interface JobCatalogEntry {
  kind: string;
  label: string;
  description: string;
  /** Can the owner start it from a button? False for the kinds that take a target and delete
   *  or hide that target's rows — those are queued by the mutation that knows the target. */
  manual: boolean;
  /** For an automatic job, what causes it to be queued. */
  trigger: string;
  scheduled: boolean;
  /** ISO time of the next scheduled firing, when this kind runs on a timer. */
  next_run: string | null;
  /** The most recent job of this kind, or null if it has never run. */
  last: Job | null;
  total: number;
  queued: number;
  running: number;
  failed: number;
}

/** POST /api/system/jobs — the job as it stood after the inline drain. */
export interface JobResult {
  id: number;
  kind: string;
  status: Job["status"];
  detail: string;
  error: string | null;
  /** sync.check only: the row labels it corrected (or, in a dry run, would correct). */
  fixed?: string[];
  /** sync.check only: collections DELETED (or, in a dry run, that would be) because their user is
   *  gone. Kept apart from `fixed` — this is the one action that cannot be undone. */
  orphans?: string[];
}

/** GET /api/schedule — one recurring thing, whether it is a job or a group of rows. */
export interface ScheduleEntry {
  type: "job" | "rows";
  cron: string;
  next_run: string | null;
  /** Jobs only. */
  kind?: string;
  label?: string;
  description?: string;
  /** The settings key that holds this job's cron — what the UI writes to change it. */
  setting?: string;
  /** True when `cron` came from the built-in default rather than something the owner set — so the UI
   *  can say so instead of implying they chose it. */
  using_default?: boolean;
  /** Blank cron means "off" rather than "use the default". Only the opt-in kinds. */
  optional?: boolean;
  writes_plex?: boolean;
  /** Row groups only: every enabled row sharing this cron. One trigger builds all of them. */
  rows?: { id: number; slug: string; name: string }[];
}

export interface ScheduleResponse {
  jobs: ScheduleEntry[];
  rows: ScheduleEntry[];
}

/** One blocked seed: a title that must never shape this person's recommendations. */
export interface BlockedSeed {
  tmdb_id: number;
  title: string;
  media_type: string;
  year: number | null;
}

/** `prefs.blocked_seeds` as records, whatever shape it is stored in. Mirrors the server's
 *  `blocked_entries` — an old install's bare-int list is valid data and keeps working. */
export function blockedSeeds(
  prefs: UserPrefs | undefined,
): BlockedSeed[] {
  return (prefs?.blocked_seeds ?? []).map((entry) =>
    typeof entry === "number"
      ? { tmdb_id: entry, title: "", media_type: "", year: null }
      : entry,
  );
}

/** GET /api/report/deleted-rows — pick history left behind by a row that no longer exists. */
export interface DeletedRowHistory {
  slug: string;
  picks: number;
  first_seen: string | null;
  last_seen: string | null;
}
