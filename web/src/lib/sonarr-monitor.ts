/**
 * How much of a show Sonarr monitors — and so downloads — when Shortlist asks for it.
 *
 * These are Sonarr's own Add Series "Monitor" choices, passed through untouched, so the labels are
 * Sonarr's too: someone comparing the two screens must not have to work out which of our words means
 * which of theirs. The hints are ours, because a label names the setting and a hint names what
 * actually lands on disk.
 *
 * Shared by the global setting (Settings → Requests) and the per-row override (Row editor), so the
 * two can never drift into naming the same choice differently.
 *
 * Mirrors `SONARR_MONITOR_MODES` in `shortlist/engine/models.py` — a deliberate SUBSET of Sonarr's
 * dropdown, and that file records which modes were measured out of it and why.
 */
export const SONARR_MONITOR_MODES = [
  "all",
  "firstSeason",
  "lastSeason",
  "pilot",
  "none",
] as const;

export type SonarrMonitor = (typeof SONARR_MONITOR_MODES)[number];

/** A row stores `null` to mean "inherit the global". */
export type RowSonarrMonitor = SonarrMonitor | null;

export const SONARR_MONITOR_LABELS: Record<SonarrMonitor, string> = {
  all: "Every season",
  firstSeason: "First seasons",
  lastSeason: "Last seasons",
  pilot: "Pilot episode",
  none: "Add unmonitored",
};

/** What actually lands on disk — the reason someone picks one of these. First/last depend on the
 *  count, so they are spelled out by `sonarrMonitorHint`; these are the count-free fallbacks. Whether
 *  seasons that air LATER are picked up depends on the route, so the control says that separately. */
export const SONARR_MONITOR_HINTS: Record<SonarrMonitor, string> = {
  all: "Grabs every season, including the whole back catalogue of a long-running show.",
  firstSeason: "Season 1 only — a taster. Add the rest if it lands well.",
  lastSeason: "The newest season only.",
  pilot: "The first episode, and nothing else.",
  none: "Files the show in Sonarr unmonitored — nothing downloads until you say so.",
};

/** The first/last modes are the two that take a season count; every other mode ignores it. */
export function takesSeasonCount(mode: SonarrMonitor): boolean {
  return mode === "firstSeason" || mode === "lastSeason";
}

export const MIN_SEASON_COUNT = 1;
export const MAX_SEASON_COUNT = 99;

/** A stored count, or 1 — the count first/last always meant before it existed. */
export function asSeasonCount(value: unknown): number {
  const n = Number(value);
  return Number.isInteger(n) && n >= MIN_SEASON_COUNT && n <= MAX_SEASON_COUNT
    ? n
    : MIN_SEASON_COUNT;
}

/** The modes a request route can carry. Overseerr takes a season list; "pilot" and "none" only
 *  exist as Sonarr's own Add Series options, so they work when requests go straight to Sonarr. */
export function monitorModesFor(
  target: "arr" | "overseerr" | undefined,
): readonly SonarrMonitor[] {
  return target === "overseerr"
    ? ["all", "firstSeason", "lastSeason"]
    : SONARR_MONITOR_MODES;
}

/** One line on what the choice downloads, with the count spelled out for first/last. */
export function sonarrMonitorHint(mode: SonarrMonitor, count: number): string {
  if (count > 1 && mode === "firstSeason") {
    return `Seasons 1 to ${count}. Add the rest if it lands well.`;
  }
  if (count > 1 && mode === "lastSeason") {
    return `The newest ${count} seasons.`;
  }
  return SONARR_MONITOR_HINTS[mode];
}

/** A short summary of the choice for a "global default" line — "First seasons · 2". */
export function sonarrMonitorSummary(mode: SonarrMonitor, count: number): string {
  return takesSeasonCount(mode)
    ? `${SONARR_MONITOR_LABELS[mode]} · ${count}`
    : SONARR_MONITOR_LABELS[mode];
}

export function asSonarrMonitor(value: unknown): SonarrMonitor {
  return SONARR_MONITOR_MODES.includes(value as SonarrMonitor)
    ? (value as SonarrMonitor)
    : "all";
}
