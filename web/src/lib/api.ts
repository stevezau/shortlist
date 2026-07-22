import type {
  ApiTokenCreated,
  ApiTokenStatus,
  AppNotification,
  ArrOptions,
  EffectivenessReport,
  OwnedCollectionsAudit,
  PlexLibrary,
  ConnectionTestResult,
  LinkRequest,
  PinCreated,
  CleanupResult,
  Collection,
  CollectionInput,
  ImageProviderStatus,
  PosterInput,
  PinStatus,
  PlexServer,
  PromptPreview,
  PromptPreviewRequest,
  ProbeRequest,
  ProbeResult,
  RequestCandidate,
  RequestSendResult,
  Run,
  RunCreated,
  RunDetail,
  RunLogEntry,
  LogPage,
  RunRequest,
  RunsSummary,
  RowOverridePatch,
  Session,
  Settings,
  SetupState,
  TestableService,
  UninstallResult,
  User,
  UserPatch,
  UserRow,
  UserRunSummary,
  WatchItem,
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

  /** Enable or disable every user at once. Disabling also removes their rows from Plex. */
  setAllUsersEnabled: (
    enabled: boolean,
  ): Promise<{ updated: number; cleaned: number; enabled: boolean }> =>
    request("/api/users/set-enabled", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),

  syncUsers: (): Promise<unknown> =>
    request("/api/users/sync", { method: "POST" }),

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

  getUserHistory: (id: number): Promise<WatchItem[]> =>
    request(`/api/users/${id}/history`),

  // --- Runs ---
  /** Recent runs; pass a row slug to get only the runs that built that row. */
  getRuns: (collection?: string): Promise<Run[]> =>
    request(
      collection
        ? `/api/runs?collection=${encodeURIComponent(collection)}`
        : "/api/runs",
    ),

  getRun: (id: number): Promise<RunDetail> => request(`/api/runs/${id}`),

  /** Totals for the Runs page header (count, succeeded/failed, last run). */
  getRunsSummary: (): Promise<RunsSummary> => request("/api/runs/summary"),

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

  /** The running app version (for the footer + prefilled bug reports). */
  getVersion: (): Promise<{ version: string }> =>
    request("/api/system/version"),

  /** Whether an owner API token exists (+ when it was made and its last-4 hint) — never the token. */
  getApiToken: (): Promise<ApiTokenStatus> => request("/api/system/api-token"),

  /** Generate (or replace) the owner API token. The plaintext is returned ONCE, here only. */
  createApiToken: (): Promise<ApiTokenCreated> =>
    request("/api/system/api-token", { method: "POST" }),

  /** Revoke the API token — any script still using it starts getting 401s. */
  revokeApiToken: (): Promise<ApiTokenStatus> =>
    request("/api/system/api-token", { method: "DELETE" }),

  /** The owner's current notifications (update available, failed/paused run, errors). */
  getNotifications: (): Promise<{ notifications: AppNotification[] }> =>
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

  /** The effectiveness report: delivered-vs-watched hit rates + a recent-watches feed. */
  getReport: (): Promise<EffectivenessReport> => request("/api/report"),

  /** Run the daily watch-status sync on demand (fires in the background). */
  syncWatched: (): Promise<{ started: boolean }> =>
    request("/api/report/sync", { method: "POST" }),

  /**
   * Reconcile watched status from Plex's own database — the only source that sees a
   * mark-as-watched. `configured` is false when no database is mounted; `added` is how many watched
   * events the reconcile discovered that the play history had never seen.
   */
  reconcileWatched: (): Promise<{
    configured: boolean;
    users: number;
    added: number;
  }> => request("/api/tools/reconcile-watched", { method: "POST" }),

  /** A library's managed collections — the candidate anchors for placing rows in the shelf. */
  getLibraryCollections: (key: string): Promise<{ title: string }[]> =>
    request(`/api/system/libraries/${encodeURIComponent(key)}/collections`),

  /** Cleanup audit: every shortlist-labelled collection on Plex, with drift/orphan flags. */
  getOwnedCollections: (): Promise<OwnedCollectionsAudit> =>
    request("/api/system/owned-collections"),

  // --- Collections (rows) ---
  listCollections: (): Promise<Collection[]> => request("/api/collections"),

  createCollection: (body: CollectionInput): Promise<Collection> =>
    request("/api/collections", { method: "POST", body: JSON.stringify(body) }),

  updateCollection: (id: number, body: CollectionInput): Promise<Collection> =>
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

  /** Assemble the prompt from a recipe against sample data, to preview its effect before saving. */
  previewPrompt: (body: PromptPreviewRequest): Promise<PromptPreview> =>
    request("/api/settings/prompt-preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** The built-in prompt as an editable template, to pre-fill the "write the whole prompt" box. */
  getPromptDefault: (shared: boolean): Promise<{ template: string }> =>
    request(`/api/settings/prompt-default?shared=${shared}`),

  // --- Requests (Sonarr/Radarr approval inbox) ---
  listRequests: (): Promise<RequestCandidate[]> => request("/api/requests"),

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
};

/** URL for the shared SSE stream (used by lib/sse.ts only). */
export function eventsUrl(): string {
  return apiUrl("/api/events");
}
