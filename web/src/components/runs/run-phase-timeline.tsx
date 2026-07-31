import { useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { STAGE_LABELS, TAIL_STAGES } from "@/lib/run-stages";
import type { RunLogEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Stages that are MOMENTS, not phases: the boundaries of the tail rather than work inside it.
 *
 *  Each row's duration is the gap to the next stage, so a marker always measured ~0s and rendered an
 *  empty bar labelled "all users done · 0s" — a number that looks broken because it is meaningless.
 *  They bound the total instead. */
const BOUNDARIES = ["users_done", "finished"] as const;

/** When each server-wide phase first appeared, how long until the next did, and the tail's total. */
function phaseTimings(entries: RunLogEntry[]) {
  const firstSeen = new Map<string, number>();
  for (const entry of entries) {
    if (!TAIL_STAGES.includes(entry.stage as (typeof TAIL_STAGES)[number])) {
      continue;
    }
    if (!firstSeen.has(entry.stage) && entry.ts) {
      firstSeen.set(entry.stage, new Date(entry.ts).getTime());
    }
  }
  const ordered = TAIL_STAGES.filter((stage) => firstSeen.has(stage));

  const phases = ordered
    .map((stage, index) => {
      const start = firstSeen.get(stage)!;
      const next = ordered[index + 1];
      const end = next ? firstSeen.get(next)! : null;
      return {
        stage,
        seconds:
          end === null ? null : Math.max(0, Math.round((end - start) / 1000)),
      };
    })
    .filter(
      (p) => !BOUNDARIES.includes(p.stage as (typeof BOUNDARIES)[number]),
    );

  // The whole point of the card: "everyone was done in four minutes, so why did the run take nine?"
  const from = firstSeen.get("users_done") ?? null;
  const to = firstSeen.get("finished") ?? null;
  const total =
    from !== null && to !== null ? Math.max(0, Math.round((to - from) / 1000)) : null;

  return { phases, total };
}

function humanSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${mins}m ${rest}s` : `${mins}m`;
}

/**
 * How long the run spent in each server-wide phase, after the last person finished.
 *
 * This is the part of a run nobody could see: converge, shelf ordering and the requests pass all ran
 * silently, so "why did that take nine minutes when everyone was done in four?" had no answer
 * anywhere in the UI.
 */
export function RunPhaseTimeline({ entries }: { entries: RunLogEntry[] }) {
  const [open, setOpen] = useState(false);
  const { phases, total } = phaseTimings(entries);
  if (phases.length === 0) return null;
  const longest = Math.max(1, ...phases.map((p) => p.seconds ?? 0));

  return (
    <Card>
      {/* Collapsed by default. The HEADLINE answers the question on its own — the breakdown is for
          the one time in ten that the headline is surprising. */}
      <CardHeader className="pb-3">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex w-full items-center justify-between gap-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {/* The card keeps its own name as the heading. The number is SUPPORTING detail, on the
              right: leading with "After everyone finished: 1m 17s" put a second duration directly
              under the run's own "Duration 7m 37s" tile with nothing saying how the two relate. This
              says what its number measures — the tail — instead of competing for "the" total. */}
          <CardTitle className="text-base">Where the time went</CardTitle>
          <span className="shrink-0 text-xs text-muted-foreground">
            {total !== null && (
              <span className="tabular-nums text-foreground">
                {humanSeconds(total)}
              </span>
            )}
            {total !== null && " after the last person finished · "}
            {open ? "hide" : "show"}
          </span>
        </button>
      </CardHeader>
      {open && (
      <CardContent className="space-y-1.5">
        {phases.map(({ stage, seconds }) => (
          <div
            key={stage}
            className="flex items-center justify-between gap-3 text-sm"
          >
            <span className="w-56 shrink-0 truncate text-muted-foreground">
              {STAGE_LABELS[stage] ?? stage}
            </span>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
              <div
                className={cn(
                  "h-full rounded-full",
                  seconds === null ? "bg-muted" : "bg-primary/70",
                )}
                style={{
                  width: `${seconds === null ? 0 : (seconds / longest) * 100}%`,
                }}
              />
            </div>
            <span className="w-16 shrink-0 text-right tabular-nums text-muted-foreground">
              {seconds === null ? "—" : `${seconds}s`}
            </span>
          </div>
        ))}
      </CardContent>
      )}
    </Card>
  );
}
