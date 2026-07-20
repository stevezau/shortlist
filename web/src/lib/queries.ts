import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type {
  CollectionInput,
  RowOverridePatch,
  RunRequest,
  Settings,
  User,
  UserPatch,
} from "./types";

export const queryKeys = {
  users: ["users"] as const,
  runs: ["runs"] as const,
  run: (id: number) => ["runs", id] as const,
  settings: ["settings"] as const,
  collections: ["collections"] as const,
  requests: ["requests"] as const,
  arrOptions: (service: "radarr" | "sonarr") =>
    ["arr-options", service] as const,
  curatorModels: (provider: string) => ["curator-models", provider] as const,
  userRows: (id: number) => ["users", id, "rows"] as const,
  userRuns: (id: number) => ["users", id, "runs"] as const,
  userHistory: (id: number) => ["users", id, "history"] as const,
  session: ["auth", "session"] as const,
  setupState: ["setup", "state"] as const,
  apiToken: ["api-token"] as const,
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

export function useRunsSummary() {
  return useQuery({
    queryKey: [...queryKeys.runs, "summary"] as const,
    queryFn: api.getRunsSummary,
  });
}

export function useClearRuns() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.clearRuns,
    // Clearing runs also empties picks, so the dashboard report resets too — refresh both.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      queryClient.invalidateQueries({ queryKey: ["report"] });
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

export function useSettings() {
  return useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });
}

/** Whether the AI provider can generate poster images — for the row editor's Generate gate. */
export function useImageProvider() {
  return useQuery({
    queryKey: ["image-provider"],
    queryFn: api.getImageProvider,
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
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.collections }),
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

/** Model ids the saved AI provider offers, for the setup picker — only fetched once a key is on file.
 * Keyed by provider so switching providers refetches; the query reads the saved key server-side. */
export function useCuratorModels(provider: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.curatorModels(provider),
    queryFn: api.getCuratorModels,
    enabled,
    staleTime: 60_000,
    retry: false,
  });
}

export function useLibraries() {
  return useQuery({
    queryKey: ["libraries"],
    queryFn: () => api.getLibraries(),
    staleTime: 60_000,
    retry: false,
  });
}

export function useLibraryCollections(key: string, enabled = true) {
  return useQuery({
    queryKey: ["library-collections", key],
    queryFn: () => api.getLibraryCollections(key),
    staleTime: 60_000,
    retry: false,
    enabled,
  });
}

export function useOwnedCollections(enabled = false) {
  return useQuery({
    queryKey: ["owned-collections"],
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

export function useUserHistory(id: number) {
  return useQuery({
    queryKey: queryKeys.userHistory(id),
    queryFn: () => api.getUserHistory(id),
    retry: false, // a live Plex/Tautulli fetch; surface the error rather than hammering
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

export function useRequests() {
  return useQuery({ queryKey: queryKeys.requests, queryFn: api.listRequests });
}

export function useNotifications() {
  return useQuery({
    queryKey: ["notifications"],
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
      queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

export function useVersion() {
  return useQuery({
    queryKey: ["version"],
    queryFn: api.getVersion,
    staleTime: Infinity, // the running build doesn't change under the user's feet
  });
}

export function useReport() {
  return useQuery({
    queryKey: ["report"],
    queryFn: api.getReport,
    staleTime: 60_000,
  });
}

export function useSyncWatched() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.syncWatched,
    // The sync runs in the background; give it a moment, then refresh the report to pick up the new
    // "last synced" time and any freshly-credited watches.
    onSuccess: () => {
      setTimeout(
        () => queryClient.invalidateQueries({ queryKey: ["report"] }),
        4000,
      );
    },
  });
}

export function useSendRequests() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, dryRun }: { ids: number[]; dryRun?: boolean }) =>
      api.sendRequests(ids, dryRun ?? false),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.requests }),
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
