import type { api } from "@/lib/api";

/** What a sync-check preview found; empty until it has actually run (the queue may defer it). */
export function driftFindings(preview: Awaited<ReturnType<typeof api.runJob>> | undefined): { drifted: string[]; orphans: string[] } {
  if (preview?.status !== "done") return { drifted: [], orphans: [] };
  return { drifted: preview.fixed ?? [], orphans: preview.orphans ?? [] };
}
