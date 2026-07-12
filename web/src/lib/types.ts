// TODO: hand-written for now. Replace with types generated from the backend's
// OpenAPI schema (`pnpm -C web gen:api`) as soon as the FastAPI app ships one —
// per .claude/rules/frontend.md, request/response types must be generated, not
// hand-written. Keep this file byte-for-byte in sync with the API until then.

export type UserType = "owner" | "shared" | "managed";

/** GET /api/users — one row per Plex user Rowarr knows about. */
export interface User {
  id: number;
  username: string;
  slug: string;
  user_type: UserType;
  enabled: boolean;
  cold_start: boolean;
  history_depth: number;
  last_run_at: string | null;
  /** 0..1 fraction of recommended items watched within 30 days, or null before first measurement. */
  hit_rate: number | null;
  /** Saved per-user overrides — the same shape PATCH accepts. */
  prefs?: UserPrefs;
}

/** PATCH /api/users/{id} — per-user overrides. */
export interface UserPrefs {
  row_name_tpl?: string;
  row_size?: number;
  excluded_genres?: string[];
  max_rating?: string | null;
  paused?: boolean;
}

export interface UserPatch {
  enabled?: boolean;
  prefs?: UserPrefs;
}

export type RunTrigger = "schedule" | "manual" | "wizard";

export interface RunStats {
  users_ok: number;
  users_error: number;
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

/** Per-user slice of GET /api/runs/{id}. */
export interface RunUserResult {
  username: string;
  slug: string;
  status: string;
  error: string | null;
  duration_ms: number;
  llm_tokens: number;
  diff: RunDiff;
  picks: Pick[];
}

/** GET /api/runs/{id} — the run plus its per-user results. */
export interface RunDetail extends Run {
  users: RunUserResult[];
}

/** POST /api/runs body. */
export interface RunRequest {
  user_ids?: number[];
  dry_run?: boolean;
}

export interface RunCreated {
  run_id: number;
}

/** GET /api/privacy/status. */
export interface PrivacyStatus {
  last_check: string | null;
  passed: boolean | null;
  tiers: Record<string, boolean>;
}

/** POST /api/privacy/check {probe:true} response. */
export interface PrivacyCheckResult {
  passed: boolean;
  tiers: Record<string, boolean>;
}

/** GET /api/settings — free-form until the schema is generated. */
export type Settings = Record<string, unknown>;

/** POST /api/settings/test/{service} response. */
export interface ConnectionTestResult {
  ok: boolean;
  message: string;
}

export type TestableService = "plex" | "tautulli" | "tmdb" | "llm";

/** GET /api/system/health. */
export interface Health {
  status: string;
  version: string;
}

/** POST /api/system/uninstall response (also returned for dry-run previews). */
export interface UninstallResult {
  filters_restored: number;
  collections_deleted: string[];
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
}

/** Event `run.finished`. */
export interface RunFinishedEvent {
  run_id: number;
  status: string;
}

/** Event `privacy.probe.step` — one live log line during the Privacy Check. */
export interface PrivacyProbeStepEvent {
  message: string;
}

/**
 * A server plex.tv says this account can reach, with every advertised address already tried
 * from where Rowarr actually runs — only the owner's network knows which one works.
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
