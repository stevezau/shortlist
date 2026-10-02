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

/** Theme colours only, cycled; the emoji at the start of each lane is what names it. */
const BAR_COLOURS = ["bg-primary/70", "bg-success/70", "bg-plex/70", "bg-warning/60", "bg-destructive/60"];

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

/**
 * The ticked seasons across one year, each on its own lane, with a marker for today and a sentence for
 * every overlap (#137). The drawing is for sighted readers only; the sentences carry what it shows.
 *
 * Each season is drawn at its NEXT window, from the server's `next_dates` — no date rule is worked out
 * here — with its own timing, or the row's for a built-in.
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
  const overlaps = seasonOverlaps(seasons, leadDays, afterDays);
  const lanes = seasons.map((season, index) => {
    const { lead, after } = seasonTiming(season, leadDays, afterDays);
    const next = seasonWindows(season, lead, after)[0];
    return { season, next, colour: BAR_COLOURS[index % BAR_COLOURS.length] };
  });

  return (
    <div className="space-y-2">
      <div data-year-strip aria-hidden="true" className="flex gap-2">
        <div className="flex w-6 shrink-0 flex-col gap-1.5">
          {lanes.map(({ season }) => (
            <span key={season.slug} className="flex h-4 items-center justify-center text-xs leading-none">
              {season.emoji}
            </span>
          ))}
        </div>
        <div className="min-w-0 flex-1">
          <div className="relative">
            <div className="flex flex-col gap-1.5">
              {lanes.map(({ season, next, colour }) => (
                <div key={season.slug} className="relative h-4 overflow-hidden rounded-sm bg-muted/60">
                  {next &&
                    stretches(next).map((part) => (
                      <div
                        key={part.left}
                        title={`${season.name}: ${spanLabel(next)}`}
                        className={`absolute inset-y-0 rounded-sm ${colour}`}
                        style={{ left: `${part.left}%`, width: `${Math.max(part.width, 0.6)}%` }}
                      />
                    ))}
                </div>
              ))}
            </div>
            {MONTH_NAMES.slice(1).map((month, index) => (
              <div
                key={month}
                className="absolute inset-y-0 border-l border-border/70"
                style={{ left: `${yearFraction(`${now.slice(0, 4)}-${String(index + 2).padStart(2, "0")}-01`) * 100}%` }}
              />
            ))}
            <div
              className="absolute -inset-y-1 w-0.5 rounded-full bg-foreground"
              style={{ left: `${yearFraction(now) * 100}%` }}
            />
          </div>
          <div className="relative mt-1 h-4 text-[10px] text-muted-foreground">
            {MONTH_NAMES.map((month, index) => (
              <span
                key={month}
                className="absolute pl-0.5"
                style={{ left: `${yearFraction(`${now.slice(0, 4)}-${String(index + 1).padStart(2, "0")}-01`) * 100}%` }}
              >
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
