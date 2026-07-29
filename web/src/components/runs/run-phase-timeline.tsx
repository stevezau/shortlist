import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { STAGE_LABELS, TAIL_STAGES } from "@/lib/run-stages";
import type { RunLogEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

/** When each server-wide phase first appeared in the log, and how long until the next one did. */
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
  return ordered.map((stage, index) => {
    const start = firstSeen.get(stage)!;
    const next = ordered[index + 1];
    const end = next ? firstSeen.get(next)! : null;
    return {
      stage,
      seconds:
        end === null ? null : Math.max(0, Math.round((end - start) / 1000)),
    };
  });
}

/**
 * How long the run spent in each server-wide phase, after the last person finished.
 *
 * This is the part of a run nobody could see: converge, shelf ordering and the requests pass all ran
 * silently, so "why did that take nine minutes when everyone was done in four?" had no answer
 * anywhere in the UI.
 */
export function RunPhaseTimeline({ entries }: { entries: RunLogEntry[] }) {
  const phases = phaseTimings(entries);
  if (phases.length === 0) return null;
  const longest = Math.max(1, ...phases.map((p) => p.seconds ?? 0));

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Where the time went</CardTitle>
      </CardHeader>
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
    </Card>
  );
}
