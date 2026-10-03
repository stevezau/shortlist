import { useState } from "react";

import {
  MAX_REFRESH_DAYS,
  clampRefreshDays,
  refreshDaysDescription,
} from "@/lib/constants";
import { selectedClass } from "@/lib/selected";
import { cn } from "@/lib/utils";

interface RefreshDaysFieldProps {
  id?: string;
  value: number; // whole days; 0 = never refresh once built
  onChange: (days: number) => void;
  className?: string;
}

/** The cadences people actually want, so the common cases are one click rather than arithmetic. */
const PRESETS: { label: string; days: number }[] = [
  { label: "Nightly", days: 1 },
  { label: "Weekly", days: 7 },
  { label: "Fortnightly", days: 14 },
  { label: "Monthly", days: 30 },
  { label: "Never", days: 0 },
];

/**
 * How often a row re-picks its titles, in days. 0 = frozen once built, 1 = nightly, N = every N days.
 *
 * A number field with presets rather than the percent slider this replaces. The slider had to be
 * translated for the reader — it showed "55%" and the helper text underneath explained that meant
 * about every 7 days — and a slider is the wrong control now the range runs to a year: a pixel would
 * be worth several days, so the value people want could not reliably be hit. Typing "30" can.
 *
 * Keeps its own text buffer so the box can be cleared and retyped without the value snapping back to
 * a clamped number mid-keystroke; the committed value is only ever a clamped whole number.
 */
export function RefreshDaysField({
  id,
  value,
  onChange,
  className,
}: RefreshDaysFieldProps) {
  const [text, setText] = useState(String(value));
  // Re-sync the buffer when the value changes from elsewhere — a preset button here, or the row
  // editor's inherit toggle seeding the global. Adjusted during render rather than in an effect, the
  // same way `row-size-field` does it: React re-runs the component immediately without committing
  // the discarded render, so the input never paints the stale text. An effect paints stale first,
  // then corrects on the next frame — and `react-hooks/set-state-in-effect` rejects it outright.
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

  const custom = !PRESETS.some((preset) => preset.days === value);

  return (
    <div className={cn("space-y-2", className)}>
      {/* One control: the presets and the custom number are one strip, so exactly one of them
          reads as chosen — never a typed "30" beside a separately highlighted "Monthly". */}
      <div
        role="group"
        aria-label="Titles refresh every"
        className="inline-flex max-w-full flex-wrap items-stretch gap-0.5 rounded-md border border-border-strong bg-elevated p-0.5"
      >
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            onClick={() => onChange(preset.days)}
            aria-pressed={value === preset.days}
            className={cn(
              "rounded border px-3 py-1.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              value === preset.days
                ? cn(selectedClass, "font-medium")
                : "border-transparent text-muted-foreground hover:bg-raised hover:text-foreground",
            )}
          >
            {preset.label}
          </button>
        ))}
        <label
          className={cn(
            "flex items-center gap-1.5 rounded border px-2 text-xs transition-colors",
            custom ? cn(selectedClass, "font-medium") : "border-transparent text-muted-foreground",
          )}
        >
          <input
            id={id}
            type="number"
            inputMode="numeric"
            min={0}
            max={MAX_REFRESH_DAYS}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onBlur={(e) => commit(e.target.value)}
            aria-label="Titles refresh every, in days"
            className="h-7 w-14 rounded border border-input bg-background px-2 text-xs tabular-nums text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {value === 1 ? "day" : "days"}
        </label>
      </div>
      <p className="text-sm text-muted-foreground">
        {refreshDaysDescription(value)}
      </p>
    </div>
  );
}
