import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const RECENT_COUNT_MIN = 1;
const RECENT_COUNT_MAX = 25;

/** The one name this setting goes by, everywhere it appears (Settings → Finding titles and the row
 *  editor). It names the single source it affects, because it is only a slice of the list
 *  {@link MaxSeedsField} governs — `candidates.py` searches `seeds[:recent_count]` — and a name that
 *  said "watches" alone was indistinguishable from the row-wide one. Exported so every screen
 *  imports the name rather than retyping it; the row editor and Settings had already drifted apart. */
export const RECENT_COUNT_LABEL = "Watches the AI web search looks up";

/** Clamp any number to the valid recent-watches range (matches the API's 1..25 bound). */
function clampRecentCount(n: number): number {
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
  label = RECENT_COUNT_LABEL,
}: {
  value: number;
  onChange: (count: number) => void;
  /** Caption above the input. Pass "" when the surrounding block already renders one — an
   *  `InheritableField` does, and rendering both printed the same heading twice with the toggle
   *  sandwiched between them. Suppressing it falls back to an `aria-label`, so the field never
   *  loses its accessible name. */
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
    const next = text.trim() === "" ? value : clampRecentCount(Number(text));
    setText(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <div className="space-y-1.5">
      {label ? <Label htmlFor={id}>{label}</Label> : null}
      <div className="flex items-center gap-2">
        <Input
          id={id}
          // Only when the visible caption is suppressed: an aria-label WINS over a <label>, so
          // setting it unconditionally overrode the per-person caption the user-row card passes.
          aria-label={label ? undefined : RECENT_COUNT_LABEL}
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
          className="w-24"
        />
        <span className="text-sm text-muted-foreground">watches</span>
      </div>
    </div>
  );
}
