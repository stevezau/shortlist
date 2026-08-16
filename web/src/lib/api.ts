import type {
  Job,
  RowEffectiveness,
  JobCatalogEntry,
  JobResult,
  JobStatus,
  ApiTokenCreated,
  ApiTokenStatus,
  NotificationsPage,
  ArrOptions,
  Backup,
  BlockedSeed,
  DeletedRowHistory,
  EffectivenessReport,
  OwnedCollectionsAudit,
  PlexLibrary,
  ConnectionTestResult,
  LinkRequest,
  PinCreated,
  CleanupResult,
  Collection,
  CollectionBody,
  ImageProviderStatus,
  PosterInput,
  PinStatus,
  PlexServer,
  ProbeRequest,
  ProbeResult,
  ArrStatus,
  RequestCandidate,
  RequestSendResult,
  Run,
  RunCreated,
  RunDetail,
  RunLogEntry,
  LogPage,
  RunRequest,
  ReportWindow,
  RunsSummary,
  ScheduleResponse,
  RunUserTraceResponse,
  RowOverridePatch,
  SupportHealth,
  SupportLibraries,
  SupportPerson,
  SupportResult,
  SupportSuggestions,
  SupportRows,
  SupportStatus,
  SupportTitleLookup,
  Session,
  Settings,
  SetupState,
  SyncsInfo,
  TestableService,
  TitleMatch,
  UninstallResult,
  User,
  VersionInfo,
  UserPatch,
  UserRow,
  UserRunsCount,
  UserRunSummary,
  HomeUserCandidate,
  TransferResult,
  WatchItem,
  WatchedFilters,
  WatchedPage,
} from "./types";

/**
 * Error thrown for any failed API call, normalized so the UI can always show
 * a plain-English message. `status` is 0 when the server was unreachable.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * The plain-English message for a caught error: an {@link ApiError}'s own normalized message, or
 * the caller's fallback for anything else. Replaces the `error instanceof ApiError ? … : …` ternary
 * that was repeated at every mutation/query error site.
 */
export function apiErrorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function trimTrailingSlash(value: string): string {
  return value.endsWith("/") ? value.slice(0, -1) : value;
}

// Base path is configurable for subpath deployments (APP_BASE_PATH behind a
// reverse proxy). Defaults to same-origin root; override at build time with
// VITE_API_BASE or at runtime with configureApiBase().
let apiBase = trimTrailingSlash(
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "",
);

export function configureApiBase(base: string): void {
  apiBase = trimTrailingSlash(base);
}

export function apiUrl(path: string): string {
  return `${apiBase}${path}`;
}

/** What a reverse proxy's status actually means, in the terms the person can act on. */
function proxyErrorMessage(status: number): string {
  if (status === 502 || status === 503 || status === 504) {
    return "Shortlist didn't answer — it may have been restarting, or the request took too long. Wait a moment and try again.";
  }
  return `Something between your browser and Shortlist returned ${status}. Try again, and check your reverse proxy if it keeps happening.`;
}

async function errorMessageFrom(response: Response): Promise<string> {
  try {
    const body: unknown = await response.clone().json();
    if (typeof body === "object" && body !== null) {
      const detail = (body as Record<string, unknown>).detail;
      if (typeof detail === "string" && detail.length > 0) return detail;
    }
  } catch {
    // Not JSON — fall through to text.
  }
  try {
    const text = await response.text();
    // An HTML body is never ours — it is a reverse proxy's own error page, and dumping it put an
    // Apache banner and the server's HOSTNAME on screen where a diagnostic report was expected
    // (seen for real: a 502 during a container restart rendered
    // "Apache/2.4.66 (Ubuntu) Server at <host> Port 443" into the panel). Someone screenshotting
    // that into a public issue publishes their hostname, so the body is dropped and the status
    // explained instead.
    if (/^\s*(<!doctype|<html)/i.test(text))
      return proxyErrorMessage(response.status);
    if (text.length > 0 && text.length <= 500) return text;
  } catch {
    // Unreadable body — fall through to the status line.
  }
  return `The server responded with ${response.status} ${response.statusText}`.trim();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const mutating =
    method !== "GET" && method !== "HEAD" && method !== "OPTIONS";

  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      headers: {
        Accept: "application/json",
        // Backend rejects any mutation without this header (CSRF guard).
        ...(mutating ? { "x-shortlist-csrf": "1" } : {}),
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
      },
      ...init,
    });
  } catch {
    throw new ApiError(
      0,
      "Could not reach the Shortlist server. Is it running?",
    );
  }

  if (!response.ok) {
    throw new ApiError(response.status, await errorMessageFrom(response));
  }

  if (response.status === 204) {
    return undefined as T;
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(
      response.status,
      "The server returned a response Shortlist could not read.",
    );
  }
}

export const api = {
  // --- Auth ---
  createPin: (): Promise<PinCreated> =>
    request("/api/auth/pin", { method: "POST" }),

  getPin: (id: number): Promise<PinStatus> => request(`/api/auth/pin/${id}`),

  getSession: (): Promise<Session> => request("/api/auth/session"),

  logout: (): Promise<void> => request("/api/auth/logout", { method: "POST" }),

  // --- Setup wizard ---
  /** Servers this account can see, each advertised address already probed for reachability. */
  getServers: (): Promise<PlexServer[]> => request("/api/setup/servers"),

  setupProbe: (body: ProbeRequest): Promise<ProbeResult> =>
    request("/api/setup/probe", { method: "POST", body: JSON.stringify(body) }),

  setupLink: (body: LinkRequest): Promise<void> =>
    request("/api/setup/link", { method: "POST", body: JSON.stringify(body) }),

  getSetupState: (): Promise<SetupState> => request("/api/setup/state"),

  putSetupState: (state: SetupState): Promise<SetupState> =>
    request("/api/setup/state", {
      method: "PUT",
      body: JSON.stringify(state),
    }),

  // --- Users ---
  getUsers: (): Promise<User[]> => request("/api/users"),

  patchUser: (id: number, patch: UserPatch): Promise<User> =>
    request(`/api/users/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  /** File away someone Plex no longer lists: drops their picks and run history and hides them from
   *  the list. Not a delete — the users row anchors the pre-Shortlist filter snapshot uninstall
   *  restores from. Refused (409) for anyone still on the share. */
  removeUser: (
    id: number,
  ): Promise<{
    user_id: number;
    picks_deleted: number;
    runs_deleted: number;
  }> => request(`/api/users/${id}`, { method: "DELETE" }),

  /** Enable or disable every user at once. Disabling also removes their rows from Plex. */
  setAllUsersEnabled: (
    enabled: boolean,
  ): Promise<{ updated: number; cleaned: number; enabled: boolean }> =>
    request("/api/users/set-enabled", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),

  /** Re-pull shared + Home users (and the owner) from plex.tv/Tautulli. Returns how many rows the
   *  sync added vs. updated, and the total roster size, so the UI can report a real result. */
  syncUsers: (): Promise<{
    added: number;
    updated: number;
    total: number;
    /** True when a run held the writer lock, so the sync is queued rather than done. */
    queued: boolean;
  }> => request("/api/users/sync", { method: "POST" }),

  // --- Background jobs ---
  getJobs: (kind?: string, limit = 25, status?: JobStatus): Promise<Job[]> =>
    request(
      `/api/system/jobs?limit=${limit}` +
        (kind ? `&kind=${encodeURIComponent(kind)}` : "") +
        // Server-side, not a client filter over a fetched page: the "N failed" badge counts every
        // failed row in the table, and on a real server all eight sat past the newest hundred.
        (status ? `&status=${encodeURIComponent(status)}` : ""),
    ),

  /** Every job Shortlist can run, with its schedule and how it went last time. */
  getJobCatalog: (): Promise<JobCatalogEntry[]> =>
    request("/api/system/jobs/catalog"),

  runJob: (
    kind: string,
    payload: Record<string, unknown> = {},
    background = false,
  ): Promise<JobResult> =>
    request("/api/system/jobs", {
      method: "POST",
      body: JSON.stringify({ kind, payload, background }),
    }),

  blockSeed: (
    userId: number,
    seed: { tmdbId: number; title: string; mediaType?: string; year?: number },
  ): Promise<{ blocked_seeds: BlockedSeed[] }> =>
    request(`/api/users/${userId}/blocked-seeds`, {
      method: "POST",
      body: JSON.stringify({
        tmdb_id: seed.tmdbId,
        title: seed.title,
        media_type: seed.mediaType ?? "",
        year: seed.year ?? null,
      }),
    }),

  unblockSeed: (
    userId: number,
    tmdbId: number,
  ): Promise<{ blocked_seeds: BlockedSeed[] }> =>
    request(`/api/users/${userId}/blocked-seeds/${tmdbId}`, {
      method: "DELETE",
    }),

  /** TMDB's best guess for a title, for the "block a seed" picker. Not a {@link BlockedSeed}: TMDB
   *  can answer without an id, and a match with `tmdb_id: null` is not blockable. */
  searchTitles: (
    q: string,
    mediaType: "movie" | "show",
  ): Promise<TitleMatch[]> =>
    request(
      `/api/users/search/titles?q=${encodeURIComponent(q)}&media_type=${mediaType}`,
    ),

  getUserRows: (id: number): Promise<UserRow[]> =>
    request(`/api/users/${id}/rows`),

  setUserRowOverride: (
    id: number,
    collectionId: number,
    patch: RowOverridePatch,
  ): Promise<unknown> =>
    request(`/api/users/${id}/rows/${collectionId}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  getUserRuns: (id: number): Promise<UserRunSummary[]> =>
    request(`/api/users/${id}/runs`),

  getUserRunsSummary: (id: number): Promise<UserRunsCount> =>
    request(`/api/users/${id}/runs/summary`),

  getUserHistory: (id: number): Promise<WatchItem[]> =>
    request(`/api/users/${id}/history`),

  /** Search one person's cached watched set. Unlike `getUserHistory` this never touches Plex, so it
   *  can search the whole set rather than the page on screen. */
  getUserWatched: (
    id: number,
    { q, mediaType, limit }: WatchedFilters,
  ): Promise<WatchedPage> => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (q) params.set("q", q);
    if (mediaType) params.set("media_type", mediaType);
    return request(`/api/users/${id}/watched?${params}`);
  },

  // --- Watching account (the owner's escape from seeing everyone's rows) ---

  listHomeUsers: (): Promise<HomeUserCandidate[]> =>
    request("/api/watching-account/candidates"),

  /** Copy the owner's watched set onto their watching account. `scrobble` also marks the titles
   *  played in Plex — thousands of writes, all dated today because Plex cannot backdate them. */
  transferWatchHistory: (body: {
    to_user_id: number;
    scrobble: boolean;
    dry_run: boolean;
  }): Promise<TransferResult> =>
    request("/api/watching-account/transfer", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- Runs ---
  /** Recent runs; pass a row slug to get only the runs that built that row. `beforeId` pages
   *  backwards — the id of the oldest run you already have. */
  getRuns: (
    collection?: string,
    beforeId?: number,
    limit?: number,
  ): Promise<Run[]> => {
    const params = new URLSearchParams();
    if (collection) params.set("collection", collection);
    if (beforeId !== undefined) params.set("before_id", String(beforeId));
    if (limit !== undefined) params.set("limit", String(limit));
    const query = params.toString();
    return request(query ? `/api/runs?${query}` : "/api/runs");
  },

  getRun: (id: number): Promise<RunDetail> => request(`/api/runs/${id}`),

  /** The full pipeline trace for one user in one run (fetched on demand — the blob is large). */
  getRunUserTrace: (
    runId: number,
    userId: number,
  ): Promise<RunUserTraceResponse> =>
    request(`/api/runs/${runId}/users/${userId}/trace`),

  /** The same for a SHARED row, which belongs to no user and is keyed by its collection slug. */
  getRunSharedRowTrace: (
    runId: number,
    slug: string,
  ): Promise<RunUserTraceResponse> =>
    request(`/api/runs/${runId}/rows/${encodeURIComponent(slug)}/trace`),

  /** Totals for the Runs page header (count, succeeded/failed, last run). */
  getRunsSummary: (): Promise<RunsSummary> => request("/api/runs/summary"),

  /** Everything on a timer — rows and jobs together, for the Schedule page. */
  getSchedule: (): Promise<ScheduleResponse> => request("/api/schedule"),

  /** Delete ALL run history (runs, per-user rows, picks — and thus the report). Irreversible. */
  clearRuns: (): Promise<{ deleted: number }> =>
    request("/api/runs", { method: "DELETE" }),

  getRunLog: (id: number): Promise<RunLogEntry[]> =>
    request(`/api/runs/${id}/log`),

  /** The app's own log file, filtered server-side. Every line is redacted before it is sent. */
  getLogs: (params: {
    level: string;
    q: string;
    limit: number;
  }): Promise<LogPage> =>
    request(
      `/api/system/logs?level=${encodeURIComponent(params.level)}&q=${encodeURIComponent(
        params.q,
      )}&limit=${params.limit}`,
    ),

  /** Where the browser downloads the redacted log zip from (a plain link — the session cookie
   *  authenticates it, so it needs no fetch/blob dance). */
  logsDownloadUrl: (): string => apiUrl("/api/system/logs/download"),

  startRun: (body: RunRequest = {}): Promise<RunCreated> =>
    request("/api/runs", { method: "POST", body: JSON.stringify(body) }),

  /** Ask an in-flight run to stop (finishes the person it's on, then stops). 409 if not running. */
  cancelRun: (id: number): Promise<{ cancelling: boolean }> =>
    request(`/api/runs/${id}/cancel`, { method: "POST" }),

  // --- Settings ---
  getSettings: (): Promise<Settings> => request("/api/settings"),

  /** PUT /api/settings — send only the keys being changed; the server merges. */
  putSettings: (values: Settings): Promise<Settings> =>
    request("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ values }),
    }),

  testConnection: (service: TestableService): Promise<ConnectionTestResult> =>
    request(`/api/settings/test/${service}`, { method: "POST" }),

  /** Quality profiles + root folders for a connected Sonarr/Radarr (for the request-setup dropdowns). */
  getArrOptions: (service: "radarr" | "sonarr"): Promise<ArrOptions> =>
    request(`/api/settings/arr/${service}/options`),

  /** Model ids a provider offers, for the model picker. The body carries the (possibly unsaved)
   *  provider + key/URL being edited so the list reflects the current form; blank fields fall back to
   *  saved settings and a redacted key means "use the saved key" (empty result = free-text fallback). */
  getCuratorModels: (body: {
    provider: string;
    api_key?: string;
    ollama_url?: string;
  }): Promise<{ provider: string; models: string[] }> =>
    request("/api/settings/curator/models", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** The server's Plex libraries, for the Rows editor's per-row delivery-target picker. */
  getLibraries: (): Promise<PlexLibrary[]> => request("/api/system/libraries"),

  /** The running app version + update check (for the footer + update banner). */
  getVersion: (): Promise<VersionInfo> => request("/api/system/version"),

  /** Whether an owner API token exists (+ when it was made and its last-4 hint) — never the token. */
  getApiToken: (): Promise<ApiTokenStatus> => request("/api/system/api-token"),

  /** Generate (or replace) the owner API token. The plaintext is returned ONCE, here only. */
  createApiToken: (): Promise<ApiTokenCreated> =>
    request("/api/system/api-token", { method: "POST" }),

  /** Revoke the API token — any script still using it starts getting 401s. */
  revokeApiToken: (): Promise<ApiTokenStatus> =>
    request("/api/system/api-token", { method: "DELETE" }),

  /** The owner's current notifications (update available, failed/paused run, errors). */
  getNotifications: (): Promise<NotificationsPage> =>
    request("/api/notifications"),

  /** Dismiss a notification by id (a new failure / newer release re-surfaces on its own). */
  dismissNotification: (id: string): Promise<{ ok: boolean }> =>
    request("/api/notifications/dismiss", {
      method: "POST",
      body: JSON.stringify({ id }),
    }),

  /** The plain-text diagnostics bundle for bug reports (secrets-free). */
  getDebugBundle: async (): Promise<string> => {
    const response = await fetch(apiUrl("/api/system/debug"), {
      headers: { Accept: "text/plain" },
    });
    if (!response.ok)
      throw new ApiError(
        response.status,
        "Couldn't build the diagnostics bundle.",
      );
    return response.text();
  },

  /** When each sync last ran and when it next fires (Tools page). */
  getSyncs: (): Promise<SyncsInfo> => request("/api/system/syncs"),

  /** The effectiveness report: delivered-vs-watched hit rates + a recent-watches feed. */
  getReport: (window: ReportWindow = "30"): Promise<EffectivenessReport> =>
    request(`/api/report?window=${window}`),

  /** Pick history belonging to rows that no longer exist, and how much of it there is. */
  getDeletedRows: (): Promise<DeletedRowHistory[]> =>
    request("/api/report/deleted-rows"),

  /** Permanently delete that history. Omit `slug` to clear every deleted row at once. */
  clearDeletedRows: (
    slug?: string,
  ): Promise<{ cleared: number; picks: number; slugs: string[] }> =>
    request(
      slug
        ? `/api/report/deleted-rows?slug=${encodeURIComponent(slug)}`
        : "/api/report/deleted-rows",
      { method: "DELETE" },
    ),

  /** Run the daily watch-status sync on demand (fires in the background). */
  syncWatched: (): Promise<{ started: boolean }> =>
    request("/api/report/sync", { method: "POST" }),

  /** A library's managed collections — the candidate anchors for placing rows in the shelf. */
  /** A library's FOREIGN collections — ours are excluded server-side, because a Shortlist row is
   *  anchored by row slug rather than by title (a per-person row is one collection per person). */
  getLibraryCollections: (key: string): Promise<{ title: string }[]> =>
    request(`/api/system/libraries/${encodeURIComponent(key)}/collections`),

  /** Cleanup audit: every shortlist-labelled collection on Plex, with drift/orphan flags. */
  getOwnedCollections: (): Promise<OwnedCollectionsAudit> =>
    request("/api/system/owned-collections"),

  // --- Collections (rows) ---
  listCollections: (): Promise<Collection[]> => request("/api/collections"),

  /** How one row has actually performed. Its own endpoint, not a slice of the dashboard report —
   *  that one is ~30 queries and opening a row's settings must not cost what opening the dashboard
   *  costs. */
  getCollectionEffectiveness: (id: number): Promise<RowEffectiveness> =>
    request(`/api/collections/${id}/effectiveness`),

  createCollection: (body: CollectionBody): Promise<Collection> =>
    request("/api/collections", { method: "POST", body: JSON.stringify(body) }),

  updateCollection: (id: number, body: CollectionBody): Promise<Collection> =>
    request(`/api/collections/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deleteCollection: (id: number): Promise<void> =>
    request(`/api/collections/${id}`, { method: "DELETE" }),

  /** Remove this row's collections from Plex for everyone (or dry-run a preview). Removal only. */
  cleanupCollection: (id: number, dryRun: boolean): Promise<CleanupResult> =>
    request(`/api/collections/${id}/cleanup`, {
      method: "POST",
      body: JSON.stringify({ dry_run: dryRun }),
    }),

  // --- Row posters ---
  /** Whether the AI provider can generate poster images (drives the Generate option's gate). */
  getImageProvider: (): Promise<ImageProviderStatus> =>
    request("/api/system/image-provider"),

  /** The <img src> for a row's current poster image (add a cache-buster after changing it). */
  posterImageUrl: (id: number): string =>
    apiUrl(`/api/collections/${id}/poster/image`),

  /** Store an uploaded poster image and switch the row into upload mode. */
  uploadPosterImage: async (
    id: number,
    file: File,
  ): Promise<{ ok: boolean; mode: string }> => {
    const form = new FormData();
    form.append("file", file);
    // No Content-Type header: the browser sets the multipart boundary itself.
    const response = await fetch(
      apiUrl(`/api/collections/${id}/poster/upload`),
      {
        method: "POST",
        headers: { "x-shortlist-csrf": "1" },
        body: form,
      },
    );
    if (!response.ok)
      throw new ApiError(response.status, await errorMessageFrom(response));
    return response.json();
  },

  /** Remove a row's uploaded poster image. */
  deletePosterImage: (id: number): Promise<void> =>
    request(`/api/collections/${id}/poster/image`, { method: "DELETE" }),

  /** Generate a sample poster from the given text/style; returns the image as a Blob. */
  previewPoster: async (id: number, body: PosterInput): Promise<Blob> => {
    const response = await fetch(
      apiUrl(`/api/collections/${id}/poster/preview`),
      {
        method: "POST",
        headers: {
          "x-shortlist-csrf": "1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok)
      throw new ApiError(response.status, await errorMessageFrom(response));
    return response.blob();
  },

  // --- Requests (Sonarr/Radarr approval inbox) ---
  /** The approval inbox. `wantedBy` names the people to keep (bare Plex usernames, as stored in
   *  `wanters`); the server applies it BEFORE its 500-row cap, so a name reaches the whole history
   *  rather than the page. Empty/omitted = everyone, the unfiltered inbox. */
  listRequests: (wantedBy: string[] = []): Promise<RequestCandidate[]> => {
    const params = new URLSearchParams();
    for (const name of wantedBy) params.append("wanted_by", name);
    const query = params.toString();
    return request(query ? `/api/requests?${query}` : "/api/requests");
  },

  sendRequests: (ids: number[], dryRun = false): Promise<RequestSendResult> =>
    request("/api/requests/send", {
      method: "POST",
      body: JSON.stringify({ ids, dry_run: dryRun }),
    }),

  rejectRequests: (ids: number[]): Promise<{ rejected: number }> =>
    request("/api/requests/reject", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  // Un-reject: move rejected titles back to Waiting (pending) right now, metadata intact.
  restoreRequests: (ids: number[]): Promise<{ restored: number }> =>
    request("/api/requests/restore", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  // Hard-delete (no tombstone) — a later run can re-surface the title.
  deleteRequests: (ids: number[]): Promise<{ deleted: number }> =>
    request("/api/requests/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  // Clear SENT titles from the send log — hides them (the sent tombstone stays, so they're not
  // re-requested), never un-sends from Sonarr/Radarr.
  clearRequests: (ids: number[]): Promise<{ cleared: number }> =>
    request("/api/requests/clear", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  /** Live Sonarr/Radarr status for every waiting and sent title, plus whether each app answered. */
  getArrStatus: (): Promise<ArrStatus> => request("/api/requests/status"),

  // --- System ---
  /**
   * Full uninstall (or a dry-run preview of it). The backend requires the
   * literal confirm string; the typed-phrase gate in the dialog is UX only.
   */
  uninstall: (dryRun: boolean): Promise<UninstallResult> =>
    request("/api/system/uninstall", {
      method: "POST",
      body: JSON.stringify({ confirm: "UNINSTALL", dry_run: dryRun }),
    }),

  getBackups: (): Promise<Backup[]> => request("/api/system/backups"),

  createBackup: (): Promise<{ name: string; size_bytes: number }> =>
    request("/api/system/backups", { method: "POST" }),

  restoreBackup: (
    name: string,
  ): Promise<{ restored: string; message: string; privacy_note?: string }> =>
    request("/api/system/backups/restore", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  // --- Support Mode ---
  // The tools 403 until the mode is on; `supportStatus` is what the page uses to tell the two
  // states apart rather than rendering a wall of permission errors.
  supportStatus: (): Promise<SupportStatus> => request("/api/support/status"),

  enableSupport: (): Promise<SupportStatus> =>
    request("/api/support/enable", { method: "POST" }),

  disableSupport: (): Promise<SupportStatus> =>
    request("/api/support/disable", { method: "POST" }),

  supportHealth: (): Promise<SupportHealth> => request("/api/support/health"),

  supportTitle: (q: string): Promise<SupportTitleLookup> =>
    request(`/api/support/title?q=${encodeURIComponent(q)}`),

  supportPerson: (slug: string): Promise<SupportPerson> =>
    request(`/api/support/person/${encodeURIComponent(slug)}`),

  supportLibraries: (): Promise<SupportLibraries> =>
    request("/api/support/libraries"),

  supportRows: (): Promise<SupportRows> => request("/api/support/rows"),

  // The remaining checks return their own shapes plus a `text` block. The page reads them through
  // one generic panel, so they are typed as open records rather than nineteen near-identical
  // interfaces — the one field every caller actually touches is `text`, which is always present.
  supportRowSchedule: (): Promise<SupportResult> =>
    request("/api/support/row-schedule"),

  supportConnection: (): Promise<SupportResult> =>
    request("/api/support/connection"),

  supportReadAs: (
    user: string,
    endpoint: string,
    section: string,
  ): Promise<SupportResult> =>
    request(
      `/api/support/read-as?user=${encodeURIComponent(user)}&endpoint=${encodeURIComponent(endpoint)}&section=${encodeURIComponent(section)}`,
    ),

  supportSharing: (): Promise<SupportResult> => request("/api/support/sharing"),

  /** Where each row is ACTUALLY showing, vs where it should be.
   *
   *  The one check that can see the owner's own Home screen. Share filters hide a row from everyone
   *  else, but the owner has no share filter (plex-safety rule 5), so nothing but the row's own
   *  `promotedToOwnHome` flag keeps somebody else's row off it — which `supportSharing` cannot read
   *  at all. Built for issue #75 and then never wired to the UI, leaving the page's highest-stakes
   *  question ("someone can see another person's row") answerable only in half. */
  supportSurfaces: (): Promise<SupportResult> =>
    request("/api/support/surfaces"),

  supportDrift: (): Promise<SupportResult> => request("/api/support/drift"),

  supportPick: (user: string, title: string): Promise<SupportResult> =>
    request(
      `/api/support/pick?user=${encodeURIComponent(user)}&title=${encodeURIComponent(title)}`,
    ),

  supportMissing: (user: string, title: string): Promise<SupportResult> =>
    request(
      `/api/support/missing?user=${encodeURIComponent(user)}&title=${encodeURIComponent(title)}`,
    ),

  supportFunnel: (user: string): Promise<SupportResult> =>
    request(`/api/support/funnel?user=${encodeURIComponent(user)}`),

  supportAi: (user: string): Promise<SupportResult> =>
    request(`/api/support/ai?user=${encodeURIComponent(user)}`),

  supportTimeline: (user: string): Promise<SupportResult> =>
    request(`/api/support/timeline?user=${encodeURIComponent(user)}`),

  supportSettingsHistory: (): Promise<SupportResult> =>
    request("/api/support/settings-history"),

  supportJobs: (): Promise<SupportResult> => request("/api/support/jobs"),

  supportClocks: (): Promise<SupportResult> => request("/api/support/clocks"),

  supportDatabase: (): Promise<SupportResult> =>
    request("/api/support/database"),

  supportConfig: (): Promise<SupportResult> => request("/api/support/config"),

  supportErrors: (): Promise<SupportResult> => request("/api/support/errors"),

  supportRecentRuns: (): Promise<SupportResult> => request("/api/support/runs"),

  /** People and titles for the type-ahead on checks that take a name. DB-only, so it still populates
   *  when Plex is unreachable — which is exactly when these checks get used. */
  supportSuggestions: (): Promise<SupportSuggestions> =>
    request("/api/support/suggestions"),

  /** Plain links — the session cookie authenticates them, so no fetch/blob dance.
   *
   *  `supportReportZipUrl` is the one to offer: the text report PLUS every redacted log file. The
   *  `.txt` remains for anyone who wants only the pasteable part. */
  supportReportZipUrl: (): string => apiUrl("/api/support/report.zip"),

  supportBundleUrl: (): string => apiUrl("/api/support/bundle.txt"),

  /** The pasteable report. Not `request()`: the response is text/plain. */
  getSupportBundle: async (): Promise<string> => {
    const response = await fetch(apiUrl("/api/support/bundle.txt"), {
      headers: { Accept: "text/plain" },
    });
    if (!response.ok) {
      throw new ApiError(response.status, await errorMessageFrom(response));
    }
    return response.text();
  },
};

/** URL for the shared SSE stream (used by lib/sse.ts only). */
export function eventsUrl(): string {
  return apiUrl("/api/events");
}
