import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { RunRequest, Settings, UserPatch } from "./types";

export const queryKeys = {
  users: ["users"] as const,
  runs: ["runs"] as const,
  run: (id: number) => ["runs", id] as const,
  privacy: ["privacy"] as const,
  settings: ["settings"] as const,
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

export function useSetupState() {
  return useQuery({
    queryKey: queryKeys.setupState,
    queryFn: api.getSetupState,
    staleTime: 30_000,
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
    mutationFn: api.runPrivacyCheck,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.privacy }),
  });
}
