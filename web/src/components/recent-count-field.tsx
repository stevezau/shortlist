import { useEffect, useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export const RECENT_COUNT_MIN = 1;
export const RECENT_COUNT_MAX = 25;

/** Clamp any number to the valid recent-watches range (matches the API's 1..25 bound). */
export function clampRecentCount(n: number): number {
  if (Number.isNaN(n)) return RECENT_COUNT_MIN;
  return Math.max(RECENT_COUNT_MIN, Math.min(RECENT_COUNT_MAX, Math.round(n)));
}

/**
 * Picker for how many recent watches the AI web-search source searches ({@link RECENT_COUNT_MIN}..
 * {@link RECENT_COUNT_MAX}). Like {@link RowSizeField}, it keeps its own text buffer so the field can
 * be cleared and retyped without fighting the user; the clamped value is pushed up only on
 * blur/Enter, so autosave never fires mid-type with an out-of-range number. Used by both the row
 * editor (per-row default) and a person's row card (their per-row override).
 */
export function RecentCountField({
  value,
  onChange,
  label = "Recent watches to search",
}: {
  value: number;
  onChange: (count: number) => void;
  label?: string;
}) {
  const id = useId();
  const [text, setText] = useState(String(value));
  // Re-sync the buffer when the value changes from elsewhere (reset, another tab).
  useEffect(() => setText(String(value)), [value]);

  const commit = () => {
    const next = text.trim() === "" ? value : clampRecentCount(Number(text));
    setText(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="number"
        inputMode="numeric"
        min={RECENT_COUNT_MIN}
        max={RECENT_COUNT_MAX}
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
