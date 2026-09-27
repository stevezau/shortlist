import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const SEED_WINDOW_MIN = 1;
const SEED_WINDOW_MAX = 20;

/** Clamp to the valid cycle window (matches the API's 1..20 bound). */
function clampSeedWindow(n: number): number {
  if (Number.isNaN(n)) return SEED_WINDOW_MIN;
  return Math.max(SEED_WINDOW_MIN, Math.min(SEED_WINDOW_MAX, Math.round(n)));
}

/** What this row will actually do, in a sentence, for the number currently in the box.
 *
 * The number alone does not say it: "3" could as easily mean "build from 3 watches at once" (which
 * is the neighbouring setting, and the wrong one — see the row editor). Spelling out the behaviour
 * next to the input is what keeps the two apart.
 */
function seedWindowHint(value: number): string {
  return value <= 1
    ? "Always the last thing they finished. The row renames itself when they finish something new."
    : `Cycles through their last ${value} watches — a different one each day, then back to the first.`;
}

/**
 * "Take turns between their last [N] watches": how many recent watches a row cycles between
 * ({@link SEED_WINDOW_MIN}..{@link SEED_WINDOW_MAX}), written as one sentence with the number inline.
 *
 * Self-buffering like {@link MaxSeedsField}: the field can be cleared and retyped, and the clamped
 * value is pushed up only on blur/Enter so a half-typed number never reaches the row.
 */
export function SeedWindowField({
  value,
  onChange,
  disabled = false,
}: {
  value: number;
  onChange: (count: number) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const suffixId = useId();
  const labelId = useId();
  const [text, setText] = useState(String(value));
  // Re-sync the buffer when the value changes from elsewhere (reset, another tab).
  // Adjusted during render rather than in an effect — see the note in row-size-field.tsx.
  const [syncedValue, setSyncedValue] = useState(value);
  if (syncedValue !== value) {
    setSyncedValue(value);
    setText(String(value));
  }

  const commit = () => {
    const next = text.trim() === "" ? value : clampSeedWindow(Number(text));
    setText(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <Label id={labelId} htmlFor={id}>
          Take turns between their last
        </Label>
        <Input
          id={id}
          aria-labelledby={`${labelId} ${suffixId}`}
          type="number"
          inputMode="numeric"
          min={SEED_WINDOW_MIN}
          max={SEED_WINDOW_MAX}
          value={text}
          disabled={disabled}
          onChange={(event) => setText(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            }
          }}
          className="w-20"
        />
        <span id={suffixId} className="text-sm font-medium">
          watches
        </span>
      </div>
      {/* Disabled, the caller says why instead: what the number would do is beside the point then. */}
      {!disabled && (
        <p className="text-sm text-muted-foreground">{seedWindowHint(value)}</p>
      )}
    </div>
  );
}
