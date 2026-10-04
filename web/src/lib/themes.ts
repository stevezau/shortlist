import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import type {
  AiInstructions,
  Theme,
  ThemePreview,
  ThemePreviewInput,
  ThemeSaveInput,
  ThemeStats,
} from "@/lib/types";

/**
 * The AI row's theme (#138): query and mutation hooks, and the small pure helpers the editor's AI
 * sections share. The theme is written by ONE AI call per build and then stored; nothing here ever
 * asks the AI on its own, because every call spends the owner's tokens.
 */

/** A theme the owner has built or edited in this editor session and not saved yet. */
export interface PendingTheme {
  draft: Theme;
  /** The preview's counts, kept with the theme once saved. Absent for a hand edit. */
  stats: ThemeStats | null;
  /** Whether an AI call wrote it (`ai`) or the owner edited it by hand (`manual`). */
  origin: "ai" | "manual";
}

export function useThemeCapabilities() {
  return useQuery({
    queryKey: queryKeys.themeCapabilities,
    queryFn: () => api.getThemeCapabilities(),
    staleTime: 60_000,
  });
}

export function useThemePrompts() {
  return useQuery({
    queryKey: queryKeys.themePrompts,
    queryFn: () => api.getThemePrompts(),
    staleTime: Infinity,
  });
}

/** The stored theme an AI row follows; `null` for a row with no theme yet (nothing is fetched). */
export function useTheme(id: number | null) {
  return useQuery({
    queryKey: queryKeys.theme(id ?? 0),
    queryFn: () => api.getTheme(id as number),
    enabled: id !== null,
  });
}

/** Everything that decides what a preview says, in one string: the same request is the same answer. */
function previewKey(input: ThemePreviewInput, salt: string): string {
  return JSON.stringify([
    input.brief.trim(),
    input.media ?? "both",
    input.current_theme_id ?? null,
    input.collection_id ?? null,
    input.person_id ?? null,
    (input.guidance ?? "").trim(),
    salt,
  ]);
}

export interface BuiltTheme {
  preview: ThemePreview;
  /** True when the owner asked the same question again, so no AI call was made and nothing was spent. */
  cached: boolean;
}

/**
 * Write or refine a theme with one AI call. The same request, asked again, is answered from what the
 * last call returned instead of spending tokens a second time — a double-click, or pressing Build on
 * an unchanged brief, costs nothing.
 *
 * `salt` is anything else the answer depends on that the request does not carry, such as the stored
 * theme's content hash: a refinement asked after the theme changed is a new question.
 */
export function useThemePreview() {
  const answers = useRef(new Map<string, ThemePreview>());
  const mutation = useMutation({
    mutationFn: async ({ input, salt }: { input: ThemePreviewInput; salt: string }): Promise<BuiltTheme> => {
      const key = previewKey(input, salt);
      const known = answers.current.get(key);
      if (known) return { preview: known, cached: true };
      const preview = await api.previewTheme({ ...input, brief: input.brief.trim() });
      answers.current.set(key, preview);
      return { preview, cached: false };
    },
  });
  return {
    build: (input: ThemePreviewInput, salt = "") => mutation.mutateAsync({ input, salt }),
    isPending: mutation.isPending,
    isError: mutation.isError,
    error: mutation.error,
    reset: mutation.reset,
  };
}

/** Save a theme: a new one (`id` null) or an existing one's replacement contents. */
export function useSaveTheme() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number | null; body: ThemeSaveInput }) =>
      id === null ? api.createTheme(body) : api.updateTheme(id, body),
    onSuccess: (theme) => {
      if (theme.id !== null) queryClient.setQueryData(queryKeys.theme(theme.id), theme);
      queryClient.invalidateQueries({ queryKey: queryKeys.collections });
    },
  });
}

/** Pause or resume an AI row's AI. It takes effect at once, apart from Save changes. */
export function useSetAiPause() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ collectionId, paused }: { collectionId: number; paused: boolean }) =>
      api.setAiPause(collectionId, paused),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.collections }),
  });
}

/**
 * The guidance to send "Build the list" for a row's AI instructions: nothing for the default (the
 * server then uses its own wording), the owner's text for "own", and the default followed by the
 * owner's text for "add". The mechanics are never part of it.
 */
export function themeGuidance(instructions: AiInstructions, defaultGuidance: string): string {
  const text = instructions.text.trim();
  if (instructions.mode === "own") return text;
  if (instructions.mode === "add" && text) return `${defaultGuidance.trim()} ${text}`;
  return "";
}

/** The system prompt "Build the list" sends, joined the way the server joins it. */
export function buildPrompt(guidance: string, defaultGuidance: string, mechanics: string): string {
  return `${guidance.trim() || defaultGuidance.trim()} ${mechanics}`;
}

/** How many titles the AI named that the server doesn't have (found on TMDB, absent from the libraries). */
export function missingFromServer(stats: Pick<ThemeStats, "resolved" | "in_library">): number {
  return Math.max(0, stats.resolved - stats.in_library);
}

/** Whether a theme would select nothing: the server refuses it, so the editor says so first. */
export function selectsNothing(theme: Pick<Theme, "tags" | "genres" | "collections" | "picks">): boolean {
  return (
    theme.tags.length === 0 &&
    theme.genres.length === 0 &&
    theme.collections.length === 0 &&
    theme.picks.length === 0
  );
}

/** A theme as the save endpoint takes it. The hash and slug are never sent: the server works them out. */
export function toSaveBody(
  pending: PendingTheme,
  { tokens, collectionId }: { tokens: number; collectionId: number | null },
): ThemeSaveInput {
  const { draft } = pending;
  const stats = pending.stats
    ? {
        named: pending.stats.named,
        resolved: pending.stats.resolved,
        in_library: pending.stats.in_library,
        after_rules: pending.stats.after_rules,
      }
    : undefined;
  return {
    draft: {
      name: draft.name,
      emoji: draft.emoji,
      brief: draft.brief,
      origin: pending.origin,
      media: draft.media.filter((m): m is "movie" | "show" => m === "movie" || m === "show"),
      tags: draft.tags,
      genres: draft.genres,
      excluded_genres: draft.excluded_genres,
      collections: draft.collections,
      picks: draft.picks,
      rules: draft.rules,
    },
    tokens,
    collection_id: collectionId,
    ...(stats ? { stats } : {}),
  };
}

/** A theme's hard limits in words, one short phrase each; none for a limit that is not set. */
export function rulesSummary(rules: Theme["rules"]): string[] {
  const parts: string[] = [];
  if (rules.max_runtime != null) parts.push(`Up to ${rules.max_runtime} min`);
  const { min_year: from, max_year: to } = rules;
  if (from != null && to != null) parts.push(`Released ${from}–${to}`);
  else if (from != null) parts.push(`Released ${from} or later`);
  else if (to != null) parts.push(`Released ${to} or earlier`);
  if (rules.min_rating != null) parts.push(`Rating ${rules.min_rating}+`);
  if (rules.min_votes != null) parts.push(`At least ${rules.min_votes.toLocaleString()} votes`);
  return parts;
}

/** The counts a saved theme kept from the build that wrote it, or null when it has none (a hand edit). */
export function savedStats(theme: Pick<Theme, "stats">): Pick<ThemeStats, "named" | "resolved" | "in_library" | "after_rules"> | null {
  const { named, resolved, in_library, after_rules } = theme.stats;
  if (named === undefined || resolved === undefined || in_library === undefined || after_rules === undefined) {
    return null;
  }
  return { named, resolved, in_library, after_rules };
}
