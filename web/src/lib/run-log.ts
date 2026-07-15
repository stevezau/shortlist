import type { RunLogEntry } from "@/lib/types";

/** Stable identity for a stage event: the server stamps one `ts` per emit, so this dedups the same
 * event arriving both in the seed snapshot (GET /log) and over the live SSE stream. */
function logKey(entry: RunLogEntry): string {
  return `${entry.ts ?? ""}|${entry.user}|${entry.stage}`;
}

/**
 * Merge new stage events into a run's activity log, dropping duplicates and events for other runs.
 *
 * Both the seed snapshot and the live SSE stream flow through here, so an event captured by both
 * appears once, and a stray event tagged for a different run never pollutes this feed. An event with
 * no `run_id` belongs to the single in-flight run and is kept. The result is sorted by timestamp so
 * seed and live events interleave in the order they happened, whichever arrived first.
 */
export function mergeRunLog(
  prev: RunLogEntry[],
  incoming: RunLogEntry[],
  runId: number,
): RunLogEntry[] {
  const seen = new Set(prev.map(logKey));
  const added: RunLogEntry[] = [];
  for (const entry of incoming) {
    if (entry.run_id != null && entry.run_id !== runId) continue;
    const key = logKey(entry);
    if (seen.has(key)) continue;
    seen.add(key);
    added.push(entry);
  }
  if (added.length === 0) return prev;
  return [...prev, ...added].sort((a, b) =>
    (a.ts ?? "").localeCompare(b.ts ?? ""),
  );
}
