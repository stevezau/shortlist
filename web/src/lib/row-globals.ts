import { asColdStart, COLD_START_LABELS } from "@/lib/cold-start";
import {
  REFRESH_DAYS_DEFAULT,
  RECENCY_DEFAULT,
  WATCHED_PCT_DEFAULT,
} from "@/lib/constants";
import {
  asLanguageMode,
  languageName,
  otherLanguageBar,
} from "@/lib/request-language";
import { asSonarrMonitor, SONARR_MONITOR_LABELS } from "@/lib/sonarr-monitor";
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

/** The raw global value, for callers that need to phrase it themselves rather than take the
 *  toggle caption. The row preview says what a row will actually DO, so "whatever the global is"
 *  would be the one line on that panel which answers nothing. */
export function watchedPctGlobalValue(
  settings: Settings | undefined,
): number | null {
  return num(settings, "recommendations.watched_pct");
}

export function refreshDaysGlobalValue(
  settings: Settings | undefined,
): number | null {
  return num(settings, "recommendations.refresh_days");
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

/** Day count → "every 8 days". The percent this replaced needed a third copy of the
 *  fraction→days curve right here just to say it. */
export function refreshDaysGlobal(
  settings: Settings | undefined,
): string | null {
  const days = num(settings, "recommendations.refresh_days");
  if (days === null) return null;
  if (days <= 0) return "never — frozen once built";
  if (days === 1) return "every night";
  return `every ${days} days`;
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

export function refreshDaysSeed(settings: Settings | undefined): number {
  return num(settings, "recommendations.refresh_days") ?? REFRESH_DAYS_DEFAULT;
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

/** The request globals a row can inherit, phrased for the caption under its inherit toggle.
 *
 * Same contract as the recommendation getters above: `null` while settings are loading, so the UI
 * omits the line rather than claiming a default that may not be this server's. */
export function requestRatingGlobal(
  settings: Settings | undefined,
): string | null {
  const rating = num(settings, "requests.min_rating");
  return rating === null ? null : `at least ${rating}`;
}

export function requestDemandGlobal(
  settings: Settings | undefined,
): string | null {
  const demand = num(settings, "requests.min_demand");
  if (demand === null) return null;
  return `${demand} ${demand === 1 ? "person" : "people"}`;
}

export function requestYearGlobal(
  settings: Settings | undefined,
): string | null {
  const from = num(settings, "requests.min_year");
  const to = num(settings, "requests.max_year");
  if (from === null && to === null) return null;
  if (!from && !to) return "any year";
  if (from && !to) return `${from} or newer`;
  if (!from && to) return `${to} or older`;
  return `${from}–${to}`;
}

export function requestAutoSendGlobal(
  settings: Settings | undefined,
): string | null {
  if (!settings) return null;
  const raw = settings["requests.auto_send"];
  if (typeof raw !== "boolean") return null;
  return raw ? "send automatically" : "wait for approval";
}

/** Whether requests are globally tagged with the wanting person's slug. */
export function requestAutoUserTagGlobal(
  settings: Settings | undefined,
): string | null {
  if (!settings) return null;
  const raw = settings["requests.auto_user_tag"];
  if (typeof raw !== "boolean") return null;
  return raw ? "tag by person" : "no person tag";
}

/** The run-wide cap, which a row may only ever restrict BELOW — never raise. */
export function requestMaxPerRunGlobal(
  settings: Settings | undefined,
): string | null {
  const cap = num(settings, "requests.max_per_run");
  if (cap === null) return null;
  return `up to ${cap} per run, shared between rows`;
}

export function requestRootFolderGlobal(
  settings: Settings | undefined,
  app: "radarr" | "sonarr",
): string | null {
  if (!settings) return null;
  const raw = settings[`requests.${app}.root_folder`];
  return typeof raw === "string" && raw ? raw : null;
}

/** Sonarr's monitor choice, in its own words — "All Episodes", "First Season". */
export function requestSonarrMonitorGlobal(
  settings: Settings | undefined,
): string | null {
  if (!settings) return null;
  return SONARR_MONITOR_LABELS[
    asSonarrMonitor(settings["requests.sonarr.monitor"])
  ];
}

/**
 * The owner's language choice, as one readable phrase — "prefer English, others need 8.5".
 *
 * All three settings collapse into a single caption because the row editor offers one toggle for the
 * lot: a row either follows the owner's language policy or sets its own, and three separate captions
 * under one toggle would be three answers to a question nobody asked separately.
 */
export function requestLanguageGlobal(
  settings: Settings | undefined,
): string | null {
  if (!settings) return null;
  const mode = asLanguageMode(settings["requests.language_mode"]);
  if (mode === "any") return "any language";

  const raw = settings["requests.preferred_languages"];
  const codes = Array.isArray(raw)
    ? raw.filter((c): c is string => typeof c === "string")
    : [];
  const names = codes.map(languageName).join(", ") || "no languages";
  if (mode === "only") return `only ${names}`;

  const minRating = num(settings, "requests.min_rating") ?? 7;
  const explicit = num(settings, "requests.min_rating_other");
  return `prefer ${names}, others need ${otherLanguageBar(minRating, explicit)}`;
}
