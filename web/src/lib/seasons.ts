import { isPresetCron, timeFromCron } from "@/lib/format";
import type { DateRule, Season, SeasonStatus } from "@/lib/types";

/**
 * Seasonal rows (discussion #124, custom seasons #137): a row that follows the calendar and is hidden
 * between seasons.
 *
 * No date rule is worked out here. Which day a season falls on comes from the server's `next_dates`
 * (Thanksgiving's 4th Thursday, Easter's computus), and which season a row is in TODAY from its
 * `season_status`, both on the SERVER's clock — the one the runs, the midnight job and Plex follow.
 * What this file does is arithmetic on those dates and the words for them.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
] as const;
/** Monday first, as the server numbers them (Python's `weekday()`: Monday is 0). */
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;
const ORDINALS: Record<number, string> = { 1: "1st", 2: "2nd", 3: "3rd", 4: "4th", [-1]: "Last" };

export const MONTH_NAMES: readonly string[] = MONTHS;
export const WEEKDAY_NAMES: readonly string[] = WEEKDAYS;

function utc(iso: string): Date {
  return new Date(`${iso}T00:00:00Z`);
}

/** "31 Oct" (in the reader's own date order) for an ISO date, read as a calendar date, not an instant. */
export function seasonDate(iso: string): string {
  return utc(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" });
}

/** "Thu 26 Nov" (in the reader's own date order). */
export function weekdayDate(iso: string): string {
  return utc(iso).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
}

/** "Thu 26 Nov 2026" (in the reader's own date order). */
export function longDate(iso: string): string {
  return utc(iso).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** The ISO date `days` after (or, negative, before) `iso`. */
export function addDays(iso: string, days: number): string {
  return new Date(utc(iso).getTime() + days * DAY_MS).toISOString().slice(0, 10);
}

/** A date rule in plain English, worded as the server's `DateRule.label()`: "17 March", "4th Thursday of
 *  November", "21 days before Easter". For a preset, which comes without the server's label. */
export function ruleLabel(rule: DateRule): string {
  const month = MONTHS[rule.month - 1] ?? "";
  if (rule.kind === "fixed") return `${rule.day} ${month}`;
  if (rule.kind === "nth") return `${ORDINALS[rule.nth] ?? ""} ${WEEKDAYS[rule.weekday] ?? ""} of ${month}`;
  if (rule.offset === 0) return "Easter Sunday";
  const days = Math.abs(rule.offset);
  return `${days} day${days === 1 ? "" : "s"} ${rule.offset < 0 ? "before" : "after"} Easter`;
}

/** How many days before and after its day a season shows on a row: a season of the owner's carries its
 *  own (#137 D8); a built-in follows the row's "Built-in seasons show from…". */
export function seasonTiming(
  season: Pick<Season, "lead_days" | "after_days">,
  rowLeadDays: number,
  rowAfterDays: number,
): { lead: number; after: number } {
  return { lead: season.lead_days ?? rowLeadDays, after: season.after_days ?? rowAfterDays };
}

function daysWord(n: number): string {
  return `${n} day${n === 1 ? "" : "s"}`;
}

/** "from 14 days before", "from 7 days before to 1 day after", "on the day only". */
export function timingLabel(lead: number, after: number): string {
  if (lead === 0 && after === 0) return "on the day only";
  if (lead === 0) return `the day and ${daysWord(after)} after`;
  if (after === 0) return `from ${daysWord(lead)} before`;
  return `from ${daysWord(lead)} before to ${daysWord(after)} after`;
}

/** When a season next shows on a row with these days before and after, e.g. "1 Oct – 31 Oct". Empty
 *  when the server gave no date. */
export function seasonWindowLabel(season: Pick<Season, "next_dates">, leadDays: number, afterDays: number): string {
  const next = season.next_dates[0];
  if (!next) return "";
  return `${seasonDate(addDays(next, -leadDays))} – ${seasonDate(addDays(next, afterDays))}`;
}

/** ISO start and end, both shown. */
export type DateSpan = { start: string; end: string };

/** A season's windows around each of its next dates — this year's or next, and the one after. */
export function seasonWindows(season: Pick<Season, "next_dates">, lead: number, after: number): DateSpan[] {
  return season.next_dates.map((day) => ({ start: addDays(day, -lead), end: addDays(day, after) }));
}

export type SeasonOverlap<S> = { first: S; second: S } & DateSpan;

/**
 * Every pair of these seasons whose windows share days, with the first days they share, in the order
 * given. Both of each season's next dates are compared, not just the next: on 20 December, Christmas's
 * window is this year's and Thanksgiving's next year's, yet the two still overlap every year.
 */
export function seasonOverlaps<S extends Pick<Season, "next_dates" | "lead_days" | "after_days">>(
  seasons: readonly S[],
  rowLeadDays: number,
  rowAfterDays: number,
): SeasonOverlap<S>[] {
  const windows = seasons.map((season) => {
    const { lead, after } = seasonTiming(season, rowLeadDays, rowAfterDays);
    return seasonWindows(season, lead, after);
  });
  const found: SeasonOverlap<S>[] = [];
  seasons.forEach((first, i) => {
    seasons.slice(i + 1).forEach((second, offset) => {
      const shared = (windows[i] ?? [])
        .flatMap((a) =>
          (windows[i + 1 + offset] ?? []).map((b) => ({
            start: a.start > b.start ? a.start : b.start,
            end: a.end < b.end ? a.end : b.end,
          })),
        )
        .filter((span) => span.start <= span.end)
        .sort((a, b) => a.start.localeCompare(b.start))[0];
      if (shared) found.push({ first, second, ...shared });
    });
  });
  return found;
}

/** Where a date falls in its own year, from 0 (1 January) to 1 (the end of 31 December). */
export function yearFraction(iso: string, endOfDay = false): number {
  const year = Number(iso.slice(0, 4));
  const start = Date.UTC(year, 0, 1);
  const length = Date.UTC(year + 1, 0, 1) - start;
  return (utc(iso).getTime() - start + (endOfDay ? DAY_MS : 0)) / length;
}

/** The one line the Rows card and the editor say about where a seasonal row is today. */
export function seasonStatusLine(status: SeasonStatus | null | undefined): string {
  if (!status) return "";
  if (status.showing) {
    return `Showing ${status.showing.emoji} ${status.showing.name} until ${seasonDate(status.showing.ends)}`;
  }
  if (status.next) {
    return `Hidden until ${status.next.emoji} ${status.next.name} starts on ${seasonDate(status.next.starts)}`;
  }
  return "";
}

/** Whether a row's schedule runs it every day — what a seasonal row needs to change nightly and to switch
 *  seasons on the day they start. */
export function isNightly(cron: string): boolean {
  const trimmed = cron.trim();
  return trimmed !== "" && isPresetCron(trimmed) && !timeFromCron(trimmed).weekly;
}
