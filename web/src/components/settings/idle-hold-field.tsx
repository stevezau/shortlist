import { useState } from "react";

import { MAX_REFRESH_DAYS, clampRefreshDays } from "@/lib/constants";
import { cn } from "@/lib/utils";

interface IdleHoldFieldProps {
  id?: string;
  value: number; // whole days; 0 = never hold, rebuild on the normal cadence
  onChange: (days: number) => void;
  /** The EFFECTIVE rebuild cadence this hold competes with, so the field can say when it cannot
   *  fire. Effective, not stored: the engine forces a row that follows a watch to nightly, which is
   *  what makes a hold work on it at all. */
  cadence?: number;
  /** Whether this control is editing one row or the server-wide default — the warning can only speak
   *  for every row in the `global` case, where some rows are forced nightly and unaffected. */
  scope?: "row" | "global";
  className?: string;
}

/** Off first, because off is the shipped default and the value an owner comes back to undo. */
const PRESETS: { label: string; days: number }[] = [
  { label: "Off", days: 0 },
  { label: "2 weeks", days: 14 },
  { label: "A month", days: 30 },
  { label: "2 months", days: 60 },
];

/** What the number means, in the terms the owner set it in. The ceiling is the half that has to be
 *  unmissable: a hold that never ended would be the opposite of what this is for. */
function description(days: number): string {
  if (days <= 0) {
    return "Off — rows rebuild on schedule whatever the person has been watching.";
  }
  return `A row due to rebuild is left alone while the person it belongs to hasn't watched anything since it was built — and rebuilds anyway after ${days} days, so a row never goes stale.`;
}

/**
 * Whether this hold can ever actually fire, given the cadence beside it.
 *
 * A row is rebuilt on its due night, so at its NEXT due night its age is exactly the cadence — and
 * the ceiling releases at that age. So a hold only bites when it is strictly GREATER than the
 * cadence. Both controls offer 14 and 30 as presets, which makes "monthly row, monthly hold" an
 * easy thing to set and a complete no-op; without this line nothing on screen would say so.
 */
function inertBecause(
  days: number,
  cadence: number | undefined,
  scope: "row" | "global",
): string | null {
  if (days <= 0 || cadence === undefined) return null;
  // The `{top_seed}` caveat belongs only on the server-wide control: it speaks for every row, and
  // some of them are forced nightly and unaffected by whatever the cadence here says.
  const caveat =
    scope === "global"
      ? " (a row named after a watch rebuilds nightly, so the hold still works there)"
      : "";
  // Cadence 0 is "Never" — a one-click preset on both controls. Such a row never comes due, so there
  // is no rebuild for the hold to postpone. Distinct from the cadence-beats-it case below, and not
  // the same advice: raising the hold cannot help here.
  if (cadence <= 0) {
    return scope === "row"
      ? "No effect: this row never rebuilds, so there is nothing to hold."
      : `No effect: rows on this cadence never rebuild, so there is nothing to hold${caveat}.`;
  }
  if (days > cadence) return null;
  const subject =
    scope === "row"
      ? "this row already rebuilds"
      : "rows on this cadence already rebuild";
  return `No effect: ${subject} every ${cadence} days, so ${scope === "row" ? "it" : "they"} never reach ${days} days old${caveat}. Set the hold above ${cadence} days to use it.`;
}

/**
 * How long a row may wait when its owner has watched nothing since it was last built.
 *
 * Shares `clampRefreshDays` and `MAX_REFRESH_DAYS` with the cadence field beside it — same units,
 * same bounds, same server-side validator — but its own presets and copy, because the question is a
 * different one ("how patient?") and the cadence field's presets ("Nightly", "Never") answer it
 * misleadingly. Same text-buffer trick as `refresh-days-field`, for the same reason.
 */
export function IdleHoldField({
  id,
  value,
  onChange,
  cadence,
  scope = "row",
  className,
}: IdleHoldFieldProps) {
  const [text, setText] = useState(String(value));
  const [syncedValue, setSyncedValue] = useState(value);
  if (syncedValue !== value) {
    setSyncedValue(value);
    setText(String(value));
  }

  const commit = (raw: string) => {
    const parsed = Number(raw);
    const next = raw.trim() === "" ? value : clampRefreshDays(parsed);
    setText(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="number"
          inputMode="numeric"
          min={0}
          max={MAX_REFRESH_DAYS}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={(e) => commit(e.target.value)}
          aria-label="How long to hold a row for an inactive viewer, in days"
          className="h-9 w-24 rounded-md border border-input bg-background px-3 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <span className="text-sm text-muted-foreground">
          {value === 1 ? "day" : "days"}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            onClick={() => onChange(preset.days)}
            aria-pressed={value === preset.days}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              value === preset.days
                ? "border-primary bg-primary text-primary-foreground"
                : "border-input text-muted-foreground hover:bg-accent hover:text-accent-foreground",
            )}
          >
            {preset.label}
          </button>
        ))}
      </div>
      <p className="text-sm text-muted-foreground">{description(value)}</p>
      {inertBecause(value, cadence, scope) && (
        <p className="text-sm text-warning">
          {inertBecause(value, cadence, scope)}
        </p>
      )}
    </div>
  );
}
