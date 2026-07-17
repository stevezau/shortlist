import type {
  ArrOptions,
  OwnedCollectionsAudit,
  PlexLibrary,
  ConnectionTestResult,
  LinkRequest,
  PinCreated,
  CleanupResult,
  Collection,
  CollectionInput,
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
  RunRequest,
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
  getRuns: (): Promise<Run[]> => request("/api/runs"),

  getRun: (id: number): Promise<RunDetail> => request(`/api/runs/${id}`),

  getRunLog: (id: number): Promise<RunLogEntry[]> =>
    request(`/api/runs/${id}/log`),

  startRun: (body: RunRequest = {}): Promise<RunCreated> =>
    request("/api/runs", { method: "POST", body: JSON.stringify(body) }),

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

  /** The server's Plex libraries, for the Rows editor's per-row delivery-target picker. */
  getLibraries: (): Promise<PlexLibrary[]> => request("/api/system/libraries"),

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

  /** Assemble the prompt from a recipe against sample data, to preview its effect before saving. */
  previewPrompt: (body: PromptPreviewRequest): Promise<PromptPreview> =>
    request("/api/settings/prompt-preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

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
