import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type {
  CollectionInput,
  RowOverridePatch,
  RunRequest,
  Settings,
  UserPatch,
} from "./types";

export const queryKeys = {
  users: ["users"] as const,
  runs: ["runs"] as const,
  run: (id: number) => ["runs", id] as const,
  privacy: ["privacy"] as const,
  settings: ["settings"] as const,
  collections: ["collections"] as const,
  arrOptions: (service: "radarr" | "sonarr") =>
    ["arr-options", service] as const,
  userRows: (id: number) => ["users", id, "rows"] as const,
  userRuns: (id: number) => ["users", id, "runs"] as const,
  userHistory: (id: number) => ["users", id, "history"] as const,
  health: ["health"] as const,
  session: ["auth", "session"] as const,
  setupState: ["setup", "state"] as const,
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

export function useRuns() {
  return useQuery({ queryKey: queryKeys.runs, queryFn: api.getRuns });
}

export function useRun(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.run(id),
    queryFn: () => api.getRun(id),
    enabled,
  });
}

export function usePrivacyStatus() {
  return useQuery({
    queryKey: queryKeys.privacy,
    queryFn: api.getPrivacyStatus,
  });
}

export function useSettings() {
  return useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });
}

export function useHealth() {
  return useQuery({ queryKey: queryKeys.health, queryFn: api.getHealth });
}

export function usePatchUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: UserPatch }) =>
      api.patchUser(id, patch),
    onSuccess: () =>
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

export function useSaveSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: Settings) => api.putSettings(settings),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.settings }),
  });
}

export function useRunPrivacyCheck() {
  const queryClient = useQueryClient();
  return useMutation({
    // probe:false (default) = fast read-only T1/T2; probe:true = full ~90s
    // probe with a throwaway collection (the wizard's step 5).
    mutationFn: (opts?: { probe?: boolean }) => api.runPrivacyCheck(opts ?? {}),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.privacy }),
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
