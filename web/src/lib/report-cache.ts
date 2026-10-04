import type { EffectivenessReport, ReportWindow } from "@/lib/types";

const KEY_PREFIX = "shortlist.report.";
// Bump v1 whenever the report response shape changes, so an old browser copy is ignored, not rendered.
const keyFor = (window: ReportWindow) => `${KEY_PREFIX}v1.${window}`;

const isObject = (value: unknown): boolean => typeof value === "object" && value !== null;

// Every field ReportBody reads without a guard: a cached copy missing one would crash the page.
const OBJECT_FIELDS = ["overall", "runs", "coverage", "requests", "watch_sync"] as const;
const ARRAY_FIELDS = ["trend", "per_user", "per_row", "top_titles", "recent"] as const;

function isReportShape(value: unknown): value is EffectivenessReport {
  if (!isObject(value)) return false;
  const candidate = value as Record<string, unknown>;
  return (
    OBJECT_FIELDS.every((field) => isObject(candidate[field])) &&
    ARRAY_FIELDS.every((field) => Array.isArray(candidate[field]))
  );
}

/**
 * The last report fetched for this window, or undefined. Every failure — blocked storage, private
 * windows, corrupt JSON, a value of the wrong shape — reads as "no cache".
 */
export function loadCachedReport(window: ReportWindow): EffectivenessReport | undefined {
  try {
    const raw = localStorage.getItem(keyFor(window));
    if (raw === null) return undefined;
    const parsed: unknown = JSON.parse(raw);
    return isReportShape(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

/** Remember a fetched report so the next page load can show it while the real fetch runs. */
export function saveCachedReport(window: ReportWindow, report: EffectivenessReport): void {
  try {
    localStorage.setItem(keyFor(window), JSON.stringify(report));
  } catch {
    // Storage is a nicety; a full or blocked store just means a cold load next time.
  }
}

/** Forget every remembered report (all windows, all versions). Called on sign-out: reports are owner data. */
export function clearCachedReports(): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith(KEY_PREFIX)) keys.push(key);
    }
    keys.forEach((key) => localStorage.removeItem(key));
  } catch {
    // Blocked storage has nothing of ours to clear.
  }
}
