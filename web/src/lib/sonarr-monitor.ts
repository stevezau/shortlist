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
  all: "All Episodes",
  firstSeason: "First Season",
  lastSeason: "Last Season",
  pilot: "Pilot Episode",
  none: "None",
};

/** What actually lands on disk — the reason someone picks one of these. */
export const SONARR_MONITOR_HINTS: Record<SonarrMonitor, string> = {
  all: "Grabs every season, including the whole back catalogue of a long-running show.",
  firstSeason:
    "Season 1 only — a taster. Add the rest in Sonarr if it lands well.",
  lastSeason: "The most recent season only.",
  pilot: "The first episode, and nothing else.",
  none: "Files the show in Sonarr unmonitored — nothing downloads until you say so.",
};

export function asSonarrMonitor(value: unknown): SonarrMonitor {
  return SONARR_MONITOR_MODES.includes(value as SonarrMonitor)
    ? (value as SonarrMonitor)
    : "all";
}
