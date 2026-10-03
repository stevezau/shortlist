import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { CollectionInput } from "@/lib/types";

/** One optional number. Buffers what is typed and commits on blur/Enter, so a save never fires
 *  mid-type; blank commits null, which the API reads as "no limit". */
function LimitInput({
  id,
  label,
  value,
  onChange,
  min,
  max,
  step,
  className = "w-28",
}: {
  id?: string;
  label: string;
  value: number | null;
  onChange: (next: number | null) => void;
  min: number;
  max: number;
  step?: number;
  className?: string;
}) {
  const [text, setText] = useState(value === null ? "" : String(value));
  // Re-sync when the value changes from elsewhere (reset, another tab); adjusted during render
  // rather than in an effect — see the note in row-size-field.tsx.
  const [syncedValue, setSyncedValue] = useState(value);
  if (syncedValue !== value) {
    setSyncedValue(value);
    setText(value === null ? "" : String(value));
  }

  const commit = () => {
    const parsed = text.trim() === "" ? null : Number(text);
    const next = parsed !== null && Number.isNaN(parsed) ? null : parsed;
    if (next !== value) onChange(next);
  };

  return (
    <Input
      id={id}
      aria-label={label}
      type="number"
      inputMode="decimal"
      min={min}
      max={max}
      step={step}
      value={text}
      placeholder="No limit"
      onChange={(event) => setText(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          commit();
        }
      }}
      className={className}
    />
  );
}

/**
 * Length, release-year and rating limits for a row (`max_runtime`, `min_year`/`max_year`,
 * `min_rating`). They have no server-wide default, so each is simply off while blank.
 */
export function RowLimitsFields({
  input,
  set,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
}) {
  const runtimeId = useId();
  const yearsId = useId();
  const ratingId = useId();
  const yearsBackwards =
    input.min_year !== null &&
    input.max_year !== null &&
    input.min_year > input.max_year;

  return (
    <div data-setting="limits" className="space-y-4 border-t pt-4">
      <p className="text-sm text-muted-foreground">
        Optional limits on what can be in this row. Leaving a box blank means no
        limit.
      </p>
      <div className="space-y-1.5">
        <Label htmlFor={runtimeId}>Longest it can run (minutes)</Label>
        <LimitInput
          id={runtimeId}
          label="Longest it can run (minutes)"
          value={input.max_runtime}
          onChange={(max_runtime) => set({ max_runtime })}
          min={1}
          max={600}
        />
        <p className="text-sm text-muted-foreground">
          Leaves out films longer than this. Blank means no limit.
        </p>
      </div>
      <div className="space-y-1.5" role="group" aria-labelledby={yearsId}>
        <Label id={yearsId}>Released between</Label>
        <div className="flex items-center gap-2">
          <LimitInput
            label="Released from year"
            value={input.min_year}
            onChange={(min_year) => set({ min_year })}
            min={1870}
            max={2100}
          />
          <span className="text-sm text-muted-foreground">and</span>
          <LimitInput
            label="Released up to year"
            value={input.max_year}
            onChange={(max_year) => set({ max_year })}
            min={1870}
            max={2100}
          />
        </div>
        {yearsBackwards ? (
          <p role="alert" className="text-sm text-destructive">
            The first year can&rsquo;t be later than the last year.
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">
            Fill in one end or both. Blank means no limit.
          </p>
        )}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={ratingId}>Lowest rating (out of 10)</Label>
        <LimitInput
          id={ratingId}
          label="Lowest rating (out of 10)"
          value={input.min_rating}
          onChange={(min_rating) => set({ min_rating })}
          min={0}
          max={10}
          step={0.1}
        />
        <p className="text-sm text-muted-foreground">
          Leaves out titles rated below this. Blank means no limit.
        </p>
      </div>
    </div>
  );
}
