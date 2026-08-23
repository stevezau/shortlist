import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { api } from "./api";
import { useSSE } from "./sse";
import type {
  ArrStatus,
  CollectionInput,
  ReportWindow,
  Run,
  RowOverridePatch,
  RunRequest,
  Settings,
  User,
  UserPatch,
  WatchedFilters,
} from "./types";

/**
 * Every React Query key used anywhere in the app, in one place.
 *
 * Query keys are also invalidation targets — get one wrong (a typo, a copy-pasted literal) and a
 * mutation silently stops refreshing the view it's supposed to. Scattering `["report"]`-style
 * literals across pages meant the same key was hand-typed in up to three different files with no
 * way to catch a drift between them; every key lives here now, so a rename is a one-line change
 * and `invalidateQueries` calls import the exact same array their query used.
 */
export const queryKeys = {
  users: ["users"] as const,
  runs: ["runs"] as const,
  run: (id: number) => ["runs", id] as const,
  runUserTrace: (runId: number, userId: number) =>
    ["runs", runId, "trace", userId] as const,
  runSharedRowTrace: (runId: number, slug: string) =>
    ["runs", runId, "trace", "row", slug] as const,
  runLog: (runId: number) => ["run-log", runId] as const,
  settings: ["settings"] as const,
  collections: ["collections"] as const,
  requests: ["requests"] as const,
  arrOptions: (service: "radarr" | "sonarr") =>
    ["arr-options", service] as const,
  arrStatus: ["arrStatus"] as const,
  curatorModels: (provider: string, credential: string) =>
    ["curator-models", provider, credential] as const,
  userRows: (id: number) => ["users", id, "rows"] as const,
  userRuns: (id: number) => ["users", id, "runs"] as const,
  userRunsSummary: (id: number) => ["users", id, "runs", "summary"] as const,
  homeUsers: ["watching-account", "candidates"] as const,
  userHistory: (id: number) => ["users", id, "history"] as const,
  userWatched: (id: number, filters: WatchedFilters) =>
    ["users", id, "watched", filters] as const,
  session: ["auth", "session"] as const,
  setupState: ["setup", "state"] as const,
  apiToken: ["api-token"] as const,
  logs: (level: string, q: string, limit: number) =>
    ["logs", level, q, limit] as const,
  // The base key covers every window ("30", "90", …) for broad invalidation; `reportWindow` is
  // what each windowed query itself is keyed on.
  report: ["report"] as const,
  reportWindow: (window: ReportWindow) => ["report", window] as const,
  engagement: (window: ReportWindow) => ["report", "engagement", window] as const,
  deletedRows: ["report", "deleted-rows"] as const,
  schedule: ["schedule"] as const,
  libraries: ["libraries"] as const,
  libraryCollections: (key: string) => ["library-collections", key] as const,
  ownedCollections: ["owned-collections"] as const,
  notifications: ["notifications"] as const,
  syncs: ["syncs"] as const,
  version: ["version"] as const,
  imageProvider: ["image-provider"] as const,
  backups: ["backups"] as const,
  // The base key ("jobs") covers every job-queue query for a broad "something changed" invalidation
  // (fired after every mutation — App.tsx); `jobsCatalog` is what the catalogue query itself uses.
  jobs: ["jobs"] as const,
  jobsCatalog: ["jobs", "catalog"] as const,
};

export function useSession() {
  return useQuery({
    queryKey: queryKeys.session,
    queryFn: api.getSession,
    staleTime: 60_000,
  });
}

export function useSetupState(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.setupState,
    queryFn: api.getSetupState,
    staleTime: 30_000,
    enabled: options.enabled ?? true,
  });
}

export function useUsers() {
  return useQuery({ queryKey: queryKeys.users, queryFn: api.getUsers });
}

export function useRuns(collection?: string) {
  return useQuery({
    queryKey: collection
      ? ([...queryKeys.runs, { collection }] as const)
      : queryKeys.runs,
    queryFn: () => api.getRuns(collection),
  });
}

/** How many runs a "Load more" click fetches. Matches the server's default page. */
export const RUNS_PAGE = 50;

/**
 * The runs list, paged backwards through history.
 *
 * Cursor, not offset: runs are inserted while you read, so an offset would skip or repeat rows as
 * the list shifts under you. A short page means there is nothing older — the list is the only place
 * that knows, since the endpoint returns a plain array.
 */
export function useRunsPaged(collection?: string) {
  return useInfiniteQuery({
    queryKey: collection
      ? ([...queryKeys.runs, "paged", { collection }] as const)
      : ([...queryKeys.runs, "paged"] as const),
    queryFn: ({ pageParam }) =>
      api.getRuns(collection, pageParam as number | undefined, RUNS_PAGE),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (lastPage: Run[]) =>
      lastPage.length < RUNS_PAGE
        ? undefined
        : lastPage[lastPage.length - 1]?.id,
  });
}

export function useRunsSummary() {
  return useQuery({
    queryKey: [...queryKeys.runs, "summary"] as const,
    queryFn: api.getRunsSummary,
  });
}

export function useBlockSeed(userId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (seed: {
      tmdbId: number;
      title: string;
      mediaType?: string;
      year?: number;
    }) => api.blockSeed(userId, seed),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useUnblockSeed(userId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (tmdbId: number) => api.unblockSeed(userId, tmdbId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useDeletedRows() {
  return useQuery({
    queryKey: queryKeys.deletedRows,
    queryFn: api.getDeletedRows,
  });
}

export function useClearDeletedRows() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug?: string) => api.clearDeletedRows(slug),
    // The dashboard totals change, so the report has to refetch — not just this list.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.report });
    },
  });
}

export function useSchedule() {
  return useQuery({ queryKey: queryKeys.schedule, queryFn: api.getSchedule });
}

export function useClearRuns() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.clearRuns,
    // Picks survive (metrics preserved), but the runs list and the dashboard's "Runs" card refresh.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      queryClient.invalidateQueries({ queryKey: queryKeys.report });
    },
  });
}

export function useRun(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.run(id),
    queryFn: () => api.getRun(id),
    enabled,
  });
}

/** The full-pipeline trace for one user in one run — fetched on demand (the blob is large), so
 *  callers gate it on `has_trace` and only enable it once the trace page is actually open. */
export function useRunUserTrace(runId: number, userId: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.runUserTrace(runId, userId),
    queryFn: () => api.getRunUserTrace(runId, userId),
    enabled,
  });
}

/** A SHARED row's trace. Same response shape as a user's — a shared row runs the same pipeline
 *  minus the per-person history stage — so one view renders both. */
export function useRunSharedRowTrace(
  runId: number,
  slug: string,
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.runSharedRowTrace(runId, slug),
    queryFn: () => api.getRunSharedRowTrace(runId, slug),
    enabled,
  });
}

export function useSettings() {
  return useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });
}

export function useSyncs() {
  return useQuery({ queryKey: queryKeys.syncs, queryFn: api.getSyncs });
}

/** Whether the AI provider can generate poster images — for the row editor's Generate gate. */
export function useImageProvider() {
  return useQuery({
    queryKey: queryKeys.imageProvider,
    queryFn: api.getImageProvider,
  });
}

export function useRemoveUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.removeUser(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function usePatchUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: UserPatch }) =>
      api.patchUser(id, patch),
    // Flip the switch in the cache immediately so the toggle responds to the click, not to the
    // round-trip. Only `enabled` drives the users-list UI; other patches settle via the refetch.
    onMutate: async ({ id, patch }) => {
      if (patch.enabled === undefined) return { previous: undefined };
      await queryClient.cancelQueries({ queryKey: queryKeys.users });
      const previous = queryClient.getQueryData<User[]>(queryKeys.users);
      queryClient.setQueryData<User[]>(queryKeys.users, (old) =>
        old?.map((u) =>
          u.id === id ? { ...u, enabled: patch.enabled ?? u.enabled } : u,
        ),
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous)
        queryClient.setQueryData(queryKeys.users, context.previous);
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useSetAllUsersEnabled() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (enabled: boolean) => api.setAllUsersEnabled(enabled),
    // Select all / none flips every row at once. Without this the switches don't move until every
    // write settles, so the click reads as "nothing happened" then everything jumps. Flip the cache
    // up front (one bulk request still runs in the background), and reconcile / roll back on settle.
    onMutate: async (enabled) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.users });
      const previous = queryClient.getQueryData<User[]>(queryKeys.users);
      queryClient.setQueryData<User[]>(queryKeys.users, (old) =>
        old?.map((u) => ({ ...u, enabled })),
      );
      return { previous };
    },
    onError: (_err, _enabled, context) => {
      if (context?.previous)
        queryClient.setQueryData(queryKeys.users, context.previous);
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useStartRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: RunRequest) => api.startRun(body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.runs }),
  });
}

export function useCancelRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.cancelRun(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.runs }),
  });
}

export function useSaveSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: Settings) => api.putSettings(settings),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.settings }),
  });
}

export function useApiToken() {
  return useQuery({
    queryKey: queryKeys.apiToken,
    queryFn: api.getApiToken,
    staleTime: 30_000,
  });
}

export function useCreateApiToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.createApiToken(),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apiToken }),
  });
}

export function useRevokeApiToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.revokeApiToken(),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apiToken }),
  });
}

export function useCollections() {
  return useQuery({
    queryKey: queryKeys.collections,
    queryFn: api.listCollections,
  });
}

export function useSaveCollection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number | null; body: CollectionInput }) =>
      id === null ? api.createCollection(body) : api.updateCollection(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.collections });
      // Renaming the default row writes the shared `row.name_template` setting, so refresh Settings
      // too — otherwise Settings → Defaults would still show the old name until a reload.
      queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
  });
}

export function useDeleteCollection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteCollection(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.collections }),
  });
}

/** Quality profiles + root folders for a Sonarr/Radarr — only fetched once it's connected. */
export function useArrOptions(service: "radarr" | "sonarr", enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.arrOptions(service),
    queryFn: () => api.getArrOptions(service),
    enabled,
    staleTime: 60_000,
    retry: false,
  });
}

/** A short non-crypto fingerprint (FNV-1a) so a credential can key the model-list cache without the
 * raw api key sitting in the query cache / React Query Devtools. Cache discriminator only. */
function fingerprint(value: string): string {
  if (!value) return "";
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

/** Model ids the AI provider offers, for the model picker. Sends the (possibly unsaved) provider +
 * key/URL being edited so switching provider or typing a key refetches; keyed on the provider plus a
 * fingerprint of the credential so the cache distinguishes them without holding the raw key. Callers
 * should pass a DEBOUNCED credential so typing a key doesn't refetch per keystroke. An empty result
 * leaves the free-text override. */
export function useCuratorModels(
  params: { provider: string; apiKey?: string; ollamaUrl?: string },
  enabled: boolean,
) {
  // BOTH inputs, not whichever is set first: a local/OpenAI-compatible server can now carry a key
  // as well as a URL, and keying on the key alone would serve a cached list from the previous
  // server when only the URL changed.
  const credential = `${params.apiKey ?? ""} ${params.ollamaUrl ?? ""}`;
  return useQuery({
    queryKey: queryKeys.curatorModels(params.provider, fingerprint(credential)),
    queryFn: () =>
      api.getCuratorModels({
        provider: params.provider,
        api_key: params.apiKey || undefined,
        // Sent under the legacy field name the endpoint still accepts; it feeds the one
        // local/self-hosted provider's base URL either way.
        ollama_url: params.ollamaUrl || undefined,
      }),
    enabled,
    staleTime: 60_000,
    retry: false,
  });
}

export function useLibraries() {
  return useQuery({
    queryKey: queryKeys.libraries,
    queryFn: () => api.getLibraries(),
    staleTime: 60_000,
    retry: false,
  });
}

export function useLibraryCollections(key: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.libraryCollections(key),
    queryFn: () => api.getLibraryCollections(key),
    staleTime: 60_000,
    retry: false,
    enabled,
  });
}

export function useOwnedCollections(enabled = false) {
  return useQuery({
    queryKey: queryKeys.ownedCollections,
    queryFn: () => api.getOwnedCollections(),
    retry: false,
    enabled, // on demand — this scans every Plex collection, so don't fire it on page load
  });
}

export function useUserRows(id: number) {
  return useQuery({
    queryKey: queryKeys.userRows(id),
    queryFn: () => api.getUserRows(id),
  });
}

export function useUserRuns(id: number) {
  return useQuery({
    queryKey: queryKeys.userRuns(id),
    queryFn: () => api.getUserRuns(id),
  });
}

export function useUserRunsSummary(id: number) {
  return useQuery({
    queryKey: queryKeys.userRunsSummary(id),
    queryFn: () => api.getUserRunsSummary(id),
  });
}

export function useUserHistory(id: number) {
  return useQuery({
    queryKey: queryKeys.userHistory(id),
    queryFn: () => api.getUserHistory(id),
    retry: false, // a live per-user Plex read; surface the error rather than hammering
  });
}

/** A page of someone's cached watched set. `placeholderData` keeps the previous page on screen while
 *  a new search resolves — without it every keystroke blanks the list to a skeleton, which reads as
 *  "no results" for a moment and makes typing feel broken. */
export function useUserWatched(id: number, filters: WatchedFilters) {
  return useQuery({
    queryKey: queryKeys.userWatched(id, filters),
    queryFn: () => api.getUserWatched(id, filters),
    placeholderData: (previous) => previous,
  });
}

/** Plex Home users the owner could move their watching to. `enabled: false` — it is a live plex.tv
 *  read behind a "look again" button, so it runs when asked rather than on mount. */
export function useHomeUserCandidates() {
  return useQuery({
    queryKey: queryKeys.homeUsers,
    queryFn: () => api.listHomeUsers(),
    retry: false,
  });
}

export function useTransferWatchHistory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      to_user_id: number;
      scrobble: boolean;
      dry_run: boolean;
    }) => api.transferWatchHistory(body),
    onSuccess: (result) => {
      // A dry run changed nothing, so refetching would only churn. A real one rewrote someone's
      // watched set, which the users list and every watch-history panel read from.
      if (result.dry_run) return;
      queryClient.invalidateQueries({ queryKey: queryKeys.users });
      queryClient.invalidateQueries({ queryKey: ["users"] });
    },
  });
}

export function useSetUserRowOverride(userId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      collectionId,
      patch,
    }: {
      collectionId: number;
      patch: RowOverridePatch;
    }) => api.setUserRowOverride(userId, collectionId, patch),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.userRows(userId) }),
  });
}

/**
 * The approval inbox, optionally narrowed to the people named in `wantedBy`.
 *
 * The narrowing is the SERVER's, not this page's: `GET /api/requests` applies it before its 500-row
 * cap, so picking a name searches the whole history rather than the page that happened to load.
 *
 * No names keeps the exact key (and URL) the unfiltered inbox has always used, so the page's own
 * unfiltered read and a `useRequests([])` share one cache entry instead of fetching twice. Names are
 * sorted into the key so ticking Sarah-then-Mike and Mike-then-Sarah are the same query. Every
 * mutation invalidates `["requests"]`, which is a prefix of these keys, so filtered reads refresh
 * with the rest.
 */
export function useRequests(wantedBy: string[] = []) {
  const names = [...wantedBy].sort();
  return useQuery({
    queryKey:
      names.length > 0 ? [...queryKeys.requests, names] : queryKeys.requests,
    queryFn: () => api.listRequests(names),
  });
}

/**
 * A title genuinely mid-transfer — the only state that changes on its own within seconds.
 *
 * `queued` is deliberately NOT here. It reads like a transient but is not: `_status_for` returns it
 * for "monitored, nothing on disk, nothing in the queue" (`engine/clients/arr.py`), which is the
 * resting state of a monitored title that is unreleased or simply unfindable. One of those in the
 * inbox would hold a 10-second whole-library poll open for as long as the tab stayed focused,
 * waiting on a change that may be months away.
 */
const ARR_DOWNLOADING = "downloading";

/** An Arr that didn't answer. Worth re-asking — this one clears itself the moment it comes back. */
const ARR_UNREACHABLE = "unreachable";

/** How often to re-ask the Arrs, given what they last said. `false` = don't. */
const ARR_FAST_MS = 10_000;
const ARR_RECOVER_MS = 30_000;

/**
 * How often to re-ask the Arrs, given their last answer. `false` = don't.
 *
 * Exported so the rule itself is testable. One fetch is a WHOLE-LIBRARY read from each Arr
 * (`RadarrClient.status_by_tmdb` pulls `/api/v3/movie` entire), so the difference between "poll
 * while something is moving" and "poll forever" is megabytes a minute on a large library for as
 * long as a tab happens to be open.
 *
 * Two things earn a timer, for opposite reasons. A DOWNLOADING title will change within seconds, so
 * it gets the fast pace. An UNREACHABLE app is the case that cannot recover on its own: a failed
 * lookup returns no statuses at all, so keying the timer off the titles meant the "Can't reach
 * Radarr" badge sat there until the operator happened to refocus the tab — the moment least likely
 * to coincide with the app coming back. Everything else has already settled and is left alone.
 */
export function arrStatusInterval(
  status: ArrStatus | undefined,
): number | false {
  if (!status) return false;
  if (
    Object.values(status.statuses ?? {}).some(
      (title) => title === ARR_DOWNLOADING,
    )
  ) {
    return ARR_FAST_MS;
  }
  return status.radarr === ARR_UNREACHABLE || status.sonarr === ARR_UNREACHABLE
    ? ARR_RECOVER_MS
    : false;
}

/**
 * Live Sonarr/Radarr state for the inbox's badges.
 *
 * It POLLS. It used to fetch once on mount with a 30s `staleTime` and no interval, so a title that
 * finished downloading while you watched the page went on reading "Searching" until you reloaded —
 * which is exactly the "it takes ages to say Downloaded" the inbox was reported for. Nothing
 * invalidated this key either, so a title you had just sent showed no status at all.
 *
 * Polls ONLY while a title is actually moving. One fetch is a whole-library read from each Arr
 * (`RadarrClient.status_by_tmdb` pulls `/api/v3/movie` entire), which is the right shape for asking
 * about a whole inbox at once and the wrong thing to repeat on a timer forever: an inbox where
 * everything has already downloaded has no reason to re-read the library every 30 seconds for as
 * long as the tab happens to be open, and on a large library that is megabytes a minute for nothing.
 *
 * So the timer exists exactly while it can change something — a title `queued` or `downloading` —
 * and stops once everything has settled. The two other ways the answer moves are both covered
 * without polling: sending a title invalidates this key outright (`useSendRequests`), and coming
 * back to the tab refetches on focus, which is when a title someone added to Radarr by hand shows
 * up. React Query also holds the interval to mounted components, so a closed page costs nothing.
 */
export function useArrStatus() {
  return useQuery({
    queryKey: queryKeys.arrStatus,
    queryFn: api.getArrStatus,
    refetchInterval: (query) => arrStatusInterval(query.state.data),
  });
}

export function useNotifications() {
  return useQuery({
    queryKey: queryKeys.notifications,
    queryFn: api.getNotifications,
    // Poll so a failed run / new release surfaces without a manual refresh.
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

export function useDismissNotification() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.dismissNotification(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.notifications }),
  });
}

export function useVersion() {
  return useQuery({
    queryKey: queryKeys.version,
    queryFn: api.getVersion,
    staleTime: 3600_000, // check once per hour
    refetchOnWindowFocus: false,
  });
}

export function useEngagement(window: ReportWindow = "30") {
  return useQuery({
    queryKey: queryKeys.engagement(window),
    queryFn: () => api.getEngagement(window),
  });
}

export function useReport(window: ReportWindow = "30") {
  return useQuery({
    queryKey: queryKeys.reportWindow(window),
    queryFn: () => api.getReport(window),
    staleTime: 60_000,
  });
}

/**
 * Kick off a watch-history sync, and refresh the report once it actually finishes.
 *
 * The sync runs in the background, so the POST returning tells you nothing about when it's done —
 * this used to guess with a flat 4s `setTimeout`, which could refetch before the sync landed (a
 * slow server) or long after (a fast one, leaving the "last synced" time stale in between). The
 * sync already emits `sync.finished` on the shared SSE bus the moment it's actually done; this
 * listens for that instead of guessing.
 */
export function useSyncWatched() {
  const queryClient = useQueryClient();
  useSSE({
    onSyncFinished: (event) => {
      if (event.kind === "watched") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.report });
      }
    },
  });
  return useMutation({
    mutationFn: api.syncWatched,
  });
}

export function useSendRequests() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, dryRun }: { ids: number[]; dryRun?: boolean }) =>
      api.sendRequests(ids, dryRun ?? false),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.requests });
      // The moment anything is worth asking the Arrs about is the moment you sent them something.
      // Nothing invalidated this key at all before, so a title you had just sent sat with no badge
      // until the next poll came round — or, with no poll, until a reload.
      queryClient.invalidateQueries({ queryKey: queryKeys.arrStatus });
    },
  });
}

export function useRejectRequests() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ids: number[]) => api.rejectRequests(ids),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.requests }),
  });
}

export function useDeleteRequests() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ids: number[]) => api.deleteRequests(ids),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.requests }),
  });
}

export function useRestoreRequests() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ids: number[]) => api.restoreRequests(ids),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.requests }),
  });
}

export function useClearRequests() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ids: number[]) => api.clearRequests(ids),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.requests }),
  });
}

/** The app's log file. `follow` polls so a live run narrates itself without the operator reloading;
 *  `keepPreviousData` stops the list blanking out on every poll or filter change. */
export function useLogs(
  level: string,
  q: string,
  limit: number,
  follow: boolean,
) {
  return useQuery({
    queryKey: queryKeys.logs(level, q, limit),
    queryFn: () => api.getLogs({ level, q, limit }),
    // Stop polling once it's failing: re-hitting a broken endpoint every 3s buys nothing and
    // buries the real error under a stream of identical ones. The error state offers Retry.
    refetchInterval: (query) => (follow && !query.state.error ? 3000 : false),
    placeholderData: (previous) => previous,
  });
}

/** How one row has actually performed. Only fetched for a SAVED row — a row being created has no
 *  history, and asking for one would 404. */
export function useCollectionEffectiveness(id: number | null) {
  return useQuery({
    queryKey: ["collection-effectiveness", id],
    queryFn: () => api.getCollectionEffectiveness(id as number),
    enabled: id !== null,
  });
}
