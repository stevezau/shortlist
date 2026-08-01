import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const MAX_SEEDS_MIN = 1;
const MAX_SEEDS_MAX = 100;

/** Clamp any number to the valid seed-budget range (matches the API's 1..100 bound). */
function clampMaxSeeds(n: number): number {
  if (Number.isNaN(n)) return MAX_SEEDS_MIN;
  return Math.max(MAX_SEEDS_MIN, Math.min(MAX_SEEDS_MAX, Math.round(n)));
}

/**
 * Picker for how many watched titles a row is built from ({@link MAX_SEEDS_MIN}..{@link MAX_SEEDS_MAX}).
 * Same self-buffering behaviour as {@link RecentCountField}: the field can be cleared and retyped, and
 * the clamped value is pushed up only on blur/Enter so autosave never fires mid-type.
 */
export function MaxSeedsField({
  value,
  onChange,
  label = "Watches to build from",
}: {
  value: number;
  onChange: (count: number) => void;
  /** Caption above the input. Pass "" when the surrounding block already renders one — an
   *  `InheritableField` does, and rendering both printed the same heading twice with the toggle
   *  sandwiched between them. The input keeps an `aria-label` either way, so suppressing the
   *  visible caption never costs the field its accessible name. */
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
    const next = text.trim() === "" ? value : clampMaxSeeds(Number(text));
    setText(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <div className="space-y-1.5">
      {label ? <Label htmlFor={id}>{label}</Label> : null}
      <Input
        id={id}
        aria-label="Watches to build from"
        type="number"
        inputMode="numeric"
        min={MAX_SEEDS_MIN}
        max={MAX_SEEDS_MAX}
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
    </div>
  );
}
