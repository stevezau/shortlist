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
  user_type: UserType;
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
  /** Where the row shows once promoted: both (Home + Library), home only, or library only. */
  placement: "both" | "home" | "library";
  /** Pin the row to the top of its library's Recommended shelf (server-wide, not per viewer). */
  pin_top: boolean;
  /** Per-library Recommended-shelf override for THIS row; {} inherits the global default. */
  hub_anchor: HubAnchorMap;
  prompt: { tone?: string; guidance?: string; template?: string };
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
  placement: "both" | "home" | "library";
  pin_top: boolean;
  hub_anchor: HubAnchorMap;
  prompt: { tone: string; guidance: string; template: string };
  poster: PosterInput;
}

/** PATCH /api/users/{id} — per-user overrides. */
export interface UserPrefs {
  row_name_tpl?: string;
  row_size?: number;
  excluded_genres?: string[];
  max_rating?: string | null;
  paused?: boolean;
  // Per-person curation-recipe overrides. Empty string = inherit the global default.
  prompt_tone?: string;
  prompt_guidance?: string;
  prompt_template?: string;
}

/** POST /api/settings/prompt-preview request + response. */
export interface PromptPreviewRequest {
  tone?: string;
  guidance?: string;
  template?: string;
  shared?: boolean;
}

export interface PromptPreview {
  system: string;
  user: string;
}

/** The tone presets the curation recipe offers. */
export const PROMPT_TONES = [
  "balanced",
  "warm",
  "concise",
  "cinephile",
  "playful",
] as const;
export type PromptTone = (typeof PROMPT_TONES)[number];

export interface UserPatch {
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
  /** Titles added to rows across all users this run. */
  titles_added?: number;
  /** Titles rotated out of rows across all users this run. */
  titles_removed?: number;
  /** Titles requested from Sonarr/Radarr this run (0 when requests are off). */
  titles_requested?: number;
  /** Total AI tokens this run cost (curate + the AI candidate sources). Absent on legacy runs. */
  llm_tokens?: number;
  /** That total split by where it went: { curate, llm_web, llm_library }. */
  llm_tokens_by_step?: Record<string, number>;
  /** Exa web searches run this run (billed per search, not per token — shown separately). */
  exa_searches?: number;
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
}

export interface Pick {
  rank: number;
  title: string;
  reason: string;
  /** Which watched title produced this pick, when the pipeline knows it. */
  seed_title?: string;
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
  size: number;
  is_default: boolean;
  muted: boolean;
  override: {
    row_size: number | null;
    prompt_tone: string;
    prompt_guidance: string;
    prompt_template: string;
  };
  picks: Pick[];
}

/** PUT /api/users/{id}/rows/{collection_id} body. */
export interface RowOverridePatch {
  muted?: boolean;
  row_size?: number | null;
  prompt_tone?: string;
  prompt_guidance?: string;
  prompt_template?: string;
}

/** GET /api/users/{id}/runs — one of this user's recent run results. */
export interface UserRunSummary {
  run_id: number;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  error: string | null;
  dry_run: boolean;
  diff: RunDiff;
  picks: Pick[];
}

/** GET /api/users/{id}/history — one recent watch. */
export interface WatchItem {
  title: string;
  media_type: string;
  watched_at: string;
  year: number | null;
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
  slug: string;
  status: string;
  error: string | null;
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
}

/** GET /api/runs/{id} — the run plus its per-user results. */
export interface RunDetail extends Run {
  users: RunUserResult[];
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
  /** Present on run-scoped stage events; lets a run page ignore other runs' events. */
  run_id?: number | null;
  /** ISO timestamp the server stamped the stage, when available. */
  ts?: string | null;
}

/** One line of a run's activity log (GET /api/runs/{id}/log + the SSE stage stream). */
export interface RunLogEntry {
  ts?: string | null;
  run_id?: number | null;
  user: string;
  stage: string;
  counts: Record<string, number>;
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

export interface EffectivenessReport {
  overall: {
    delivered: number;
    watched: number;
    hit_rate: number | null;
    watched_last_7d: number;
    avg_days_to_watch: number | null;
  };
  /** The daily watch-status sync: when it last ran and next fires (ISO), so the report reads as live. */
  watch_sync: { last: string | null; next: string | null };
  coverage: {
    users_enabled: number;
    users_total: number;
    users_with_picks: number;
    rows_enabled: number;
  };
  runs: {
    total: number;
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
  per_user: {
    username: string;
    slug: string;
    delivered: number;
    watched: number;
    hit_rate: number | null;
  }[];
  /** One line per (row × library): a row targeting >1 library is a separate Plex collection in each,
   *  so each library gets its own hit rate. `section_key` disambiguates rows sharing a slug. */
  per_row: {
    slug: string;
    section_key: string;
    library: string;
    name: string;
    delivered: number;
    watched: number;
    hit_rate: number | null;
  }[];
  recent: {
    username: string;
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
