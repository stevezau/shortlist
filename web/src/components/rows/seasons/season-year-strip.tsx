import { useState } from "react";

import {
  MONTH_NAMES,
  seasonDate,
  seasonOverlaps,
  seasonTiming,
  seasonWindows,
  yearFraction,
  type DateSpan,
} from "@/lib/seasons";
import type { Season } from "@/lib/types";

/** Theme colours only, cycled; each lane's label is what names it. */
const BAR_COLOURS = ["bg-primary/70", "bg-success/70", "bg-plex/70", "bg-warning/60", "bg-destructive/60"];

/** A lane's label column beside its bar, from 640px; below that the label sits above the bar. */
const LANE = "grid grid-cols-1 gap-x-2 gap-y-0.5 sm:grid-cols-[8rem_minmax(0,1fr)] sm:items-center";

function localIso(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** A window as stretches of one Jan-Dec year, in percent: one crossing New Year (Christmas with days
 *  after, Valentine's with a long lead) is drawn at both ends. */
function stretches({ start, end }: DateSpan): { left: number; width: number }[] {
  const from = yearFraction(start) * 100;
  const to = yearFraction(end, true) * 100;
  if (start.slice(0, 4) === end.slice(0, 4)) return [{ left: from, width: to - from }];
  return [
    { left: from, width: 100 - from },
    { left: 0, width: to },
  ];
}

function spanLabel({ start, end }: DateSpan): string {
  return start === end ? seasonDate(start) : `${seasonDate(start)} – ${seasonDate(end)}`;
}

/** The first of each month after January, in `year`, as a position across it. */
function monthStart(year: string, month: number): number {
  return yearFraction(`${year}-${String(month).padStart(2, "0")}-01`) * 100;
}

/**
 * The ticked seasons across one year, one lane each — named by emoji AND name, since two ready-made
 * seasons can share an emoji — with today marked, and a sentence for every overlap (#137).
 *
 * The drawing is one image to a screen reader, labelled with each season's window and today's date; the
 * overlap sentences under it are text. Each season is drawn at its NEXT window, from the server's
 * `next_dates` — no date rule is worked out here — with its own timing, or the row's for a built-in.
 */
export function SeasonYearStrip({
  seasons,
  leadDays,
  afterDays,
  today,
}: {
  /** The ticked seasons, in calendar order. */
  seasons: readonly Season[];
  /** The row's timing, which the built-ins follow. */
  leadDays: number;
  afterDays: number;
  /** ISO date for the marker; the browser's today when absent. */
  today?: string;
}) {
  const [now] = useState(() => today ?? localIso(new Date()));
  const year = now.slice(0, 4);
  const todayAt = yearFraction(now) * 100;
  const overlaps = seasonOverlaps(seasons, leadDays, afterDays);
  const lanes = seasons.map((season, index) => {
    const { lead, after } = seasonTiming(season, leadDays, afterDays);
    const next = seasonWindows(season, lead, after)[0];
    return { season, next, colour: BAR_COLOURS[index % BAR_COLOURS.length] };
  });
  const described = `When this row shows each season: ${lanes
    .map(({ season, next }) => `${season.name}, ${next ? spanLabel(next) : "no date"}`)
    .join("; ")}. Today is ${seasonDate(now)}.`;
  // Kept inside the strip at either end of the year.
  const captionShift = todayAt < 8 ? "translate-x-0" : todayAt > 92 ? "-translate-x-full" : "-translate-x-1/2";

  return (
    <div className="space-y-2">
      <div data-year-strip role="img" aria-label={described} className="space-y-1.5">
        <div className={LANE}>
          <span className="hidden sm:block" />
          <div className="relative h-4 text-[10px] font-medium">
            <span className={`absolute ${captionShift}`} style={{ left: `${todayAt}%` }}>
              Today
            </span>
          </div>
        </div>
        {lanes.map(({ season, next, colour }) => (
          <div key={season.slug} className={LANE}>
            <span className="truncate text-xs" title={`${season.emoji} ${season.name}`}>
              {season.emoji} {season.name}
            </span>
            <div className="relative h-4 overflow-hidden rounded-sm bg-muted/60">
              {Array.from({ length: 11 }, (_, i) => (
                <div
                  key={i}
                  className="absolute inset-y-0 border-l border-border/70"
                  style={{ left: `${monthStart(year, i + 2)}%` }}
                />
              ))}
              {next &&
                stretches(next).map((part) => (
                  <div
                    key={part.left}
                    title={`${season.name}: ${spanLabel(next)}`}
                    className={`absolute inset-y-0 rounded-sm ${colour}`}
                    style={{ left: `${part.left}%`, width: `${Math.max(part.width, 0.6)}%` }}
                  />
                ))}
              <div
                title={`Today, ${seasonDate(now)}`}
                className="absolute inset-y-0 w-0.5 bg-foreground"
                style={{ left: `${todayAt}%` }}
              />
            </div>
          </div>
        ))}
        <div className={LANE}>
          <span className="hidden sm:block" />
          <div className="relative h-4 text-[10px] text-muted-foreground">
            {MONTH_NAMES.map((month, index) => (
              <span key={month} className="absolute pl-0.5" style={{ left: `${monthStart(year, index + 1)}%` }}>
                <span className="sm:hidden">{month.slice(0, 1)}</span>
                <span className="hidden sm:inline">{month.slice(0, 3)}</span>
              </span>
            ))}
          </div>
        </div>
      </div>
      <p className="text-sm text-muted-foreground">
        {overlaps.length === 0
          ? "No seasons overlap. "
          : overlaps.map(({ first, second, ...span }) => (
              <span key={`${first.slug}-${second.slug}`}>
                {`${first.name} and ${second.name} overlap on ${spanLabel(span)}: the nearer date wins.`}{" "}
              </span>
            ))}
        Between seasons the row is hidden and keeps its place. Seasons change at midnight on the server.
      </p>
    </div>
  );
}
