import type { RunLogEntry } from "@/lib/types";

/**
 * Stable identity for a stage event.
 *
 * `seq` is a monotonic per-run counter stamped server-side, and is the real key: several lines land
 * in the same millisecond, so a timestamp alone let "merging share filters 1/5" and "2/5" collapse
 * into one. The ts|user|stage fallback covers entries from an older server that predates `seq`.
 */
function logKey(entry: RunLogEntry): string {
  if (typeof entry.seq === "number") return `seq:${entry.seq}`;
  return `${entry.ts ?? ""}|${entry.user}|${entry.stage}`;
}

/**
 * Merge new stage events into a run's activity log, dropping duplicates and events for other runs.
 *
 * Both the seed snapshot and the live SSE stream flow through here, so an event captured by both
 * appears once, and a stray event tagged for a different run never pollutes this feed. An event with
 * no `run_id` belongs to the single in-flight run and is kept. The result is ordered by `seq` where
 * both entries have one (the order the engine actually emitted them) and by timestamp otherwise.
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
  return [...prev, ...added].sort((a, b) => {
    if (typeof a.seq === "number" && typeof b.seq === "number") {
      return a.seq - b.seq;
    }
    return (a.ts ?? "").localeCompare(b.ts ?? "");
  });
}

/** The highest `seq` seen, for asking the server only for what's new. Null before anything with a
 *  seq has arrived, which means "fetch the whole log". */
export function latestSeq(entries: RunLogEntry[]): number | null {
  let max: number | null = null;
  for (const entry of entries) {
    if (typeof entry.seq === "number" && (max === null || entry.seq > max)) {
      max = entry.seq;
    }
  }
  return max;
}
