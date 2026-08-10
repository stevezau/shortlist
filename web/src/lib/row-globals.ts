import { asColdStart, COLD_START_LABELS } from "@/lib/cold-start";
import {
  FRESHNESS_DEFAULT,
  RECENCY_DEFAULT,
  WATCHED_PCT_DEFAULT,
} from "@/lib/constants";
import type { Settings } from "@/lib/types";

/** Mirrors the server's `recommendations.*` defaults (shortlist/server/settings_store.py) — used
 *  only as the seed when settings haven't loaded, never to display a value as if it were real. */
const RECENT_COUNT_DEFAULT = 10;
const MAX_SEEDS_DEFAULT = 30;

/**
 * The globals a row can inherit, resolved and formatted for reading.
 *
 * A row stores `null` (or `[]`) to mean "inherit", which tells the editor nothing about what it is
 * inheriting. These turn the stored global into a phrase short enough to sit under a toggle —
 * terse on purpose: the full explanation already lives under the control itself.
 *
 * Every getter returns `null` while settings are loading, so the UI omits the line rather than
 * claiming a default that may not be this server's.
 */

/** The raw global freshness, for callers that need to phrase it themselves rather than take the
 *  toggle caption. The row preview says what a row will actually DO, so "whatever the global is"
 *  would be the one line on that panel which answers nothing. */
export function watchedPctGlobalValue(
  settings: Settings | undefined,
): number | null {
  return num(settings, "recommendations.watched_pct");
}

export function freshnessGlobalValue(
  settings: Settings | undefined,
): number | null {
  return num(settings, "recommendations.freshness");
}

function num(settings: Settings | undefined, key: string): number | null {
  if (!settings) return null;
  const raw = settings[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/** 0..1 fraction → "0% — only fresh picks". */
export function watchedPctGlobal(
  settings: Settings | undefined,
): string | null {
  const fraction = num(settings, "recommendations.watched_pct");
  if (fraction === null) return null;
  const pct = Math.round(fraction * 100);
  if (pct <= 0) return "0% — only fresh picks";
  if (pct >= 100) return "100% — no filtering";
  return `${pct}% — up to ${pct}% already-watched`;
}

/** 0..1 fraction → "50% — refreshes about weekly". */
export function freshnessGlobal(settings: Settings | undefined): string | null {
  const fraction = num(settings, "recommendations.freshness");
  if (fraction === null) return null;
  const pct = Math.round(fraction * 100);
  if (pct <= 0) return "0% — frozen once built";
  if (pct >= 100) return "100% — rebuilds every night";
  // Mirrors refreshEveryDays() in constants.ts: 100 → nightly, lower → longer, capped near a fortnight.
  const days = Math.max(1, Math.round(1 + (1 - pct / 100) * 13));
  return `${pct}% — refreshes about every ${days} days`;
}

export function recencyGlobalValue(
  settings: Settings | undefined,
): number | null {
  return num(settings, "recommendations.recency");
}

/** 0..1 fraction → "50% — leans towards recent releases". */
export function recencyGlobal(settings: Settings | undefined): string | null {
  const fraction = num(settings, "recommendations.recency");
  if (fraction === null) return null;
  const pct = Math.round(fraction * 100);
  // "Any era" rather than a bare "0%": the number alone reads as a setting that is doing something
  // faint, when 0 means release date is not consulted at all.
  if (pct <= 0) return "Any era — release date ignored";
  if (pct >= 100) return "100% — strongly prefers new releases";
  return `${pct}% — leans towards recent releases`;
}

export function recentCountGlobal(
  settings: Settings | undefined,
): string | null {
  const count = num(settings, "recommendations.recent_count");
  if (count === null) return null;
  return `${count} recent ${count === 1 ? "watch" : "watches"}`;
}

export function maxSeedsGlobal(settings: Settings | undefined): string | null {
  const count = num(settings, "recommendations.max_seeds");
  if (count === null) return null;
  return `${count} ${count === 1 ? "watch" : "watches"}`;
}

/** The global cold-start choice, phrased for the caption under a row's inherit toggle. */
export function coldStartGlobal(settings: Settings | undefined): string | null {
  if (!settings) return null;
  const raw = settings["recommendations.cold_start"];
  if (typeof raw !== "string") return null;
  return COLD_START_LABELS[asColdStart(raw)];
}

/** The value a toggle should drop to when someone turns "use the global" OFF: the global itself,
 *  so flipping the switch never silently changes the row's behaviour — it only stops tracking. */
export function watchedPctSeed(settings: Settings | undefined): number {
  return (
    num(settings, "recommendations.watched_pct") ?? WATCHED_PCT_DEFAULT / 100
  );
}

export function freshnessSeed(settings: Settings | undefined): number {
  return num(settings, "recommendations.freshness") ?? FRESHNESS_DEFAULT / 100;
}

export function recencySeed(settings: Settings | undefined): number {
  return num(settings, "recommendations.recency") ?? RECENCY_DEFAULT / 100;
}

export function recentCountSeed(settings: Settings | undefined): number {
  return num(settings, "recommendations.recent_count") ?? RECENT_COUNT_DEFAULT;
}

export function maxSeedsSeed(settings: Settings | undefined): number {
  return num(settings, "recommendations.max_seeds") ?? MAX_SEEDS_DEFAULT;
}
