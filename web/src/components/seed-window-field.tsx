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
 * Picker for how many recent watches a row cycles between ({@link SEED_WINDOW_MIN}..{@link SEED_WINDOW_MAX}).
 * Same self-buffering behaviour as {@link MaxSeedsField}: the field can be cleared and retyped, and the
 * clamped value is pushed up only on blur/Enter so autosave never fires mid-type.
 */
export function SeedWindowField({
  value,
  onChange,
  label = "Recent watches to choose from",
}: {
  value: number;
  onChange: (count: number) => void;
  /** Caption above the input. Pass "" when the surrounding block already renders one. */
  label?: string;
}) {
  const id = useId();
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
      {label ? <Label htmlFor={id}>{label}</Label> : null}
      <Input
        id={id}
        aria-label="Recent watches to choose from"
        type="number"
        inputMode="numeric"
        min={SEED_WINDOW_MIN}
        max={SEED_WINDOW_MAX}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          }
        }}
        className="w-28"
      />
      <p className="text-sm text-muted-foreground">{seedWindowHint(value)}</p>
    </div>
  );
}
