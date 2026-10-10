import { NumberField } from "@/components/number-field";

const RECENT_COUNT_MIN = 1;
const RECENT_COUNT_MAX = 25;

/** The one name this setting goes by, everywhere it appears (Settings → Finding titles and the row
 *  editor). It names the single source it affects, because it is only a slice of the list
 *  {@link MaxSeedsField} governs — `candidates.py` searches `seeds[:recent_count]` — and a name that
 *  said "watches" alone was indistinguishable from the row-wide one. Exported so every screen
 *  imports the name rather than retyping it; the row editor and Settings had already drifted apart. */
export const RECENT_COUNT_LABEL = "Watches the AI web search looks up";

/**
 * Picker for how many recent watches the AI web-search source searches ({@link RECENT_COUNT_MIN}..
 * {@link RECENT_COUNT_MAX}). Used by both the row editor (per-row default) and a person's row card
 * (their per-row override).
 */
export function RecentCountField({
  value,
  onChange,
  label = RECENT_COUNT_LABEL,
}: {
  value: number;
  onChange: (count: number) => void;
  /** Caption above the input. Pass "" when the surrounding block already renders one. */
  label?: string;
}) {
  return (
    <NumberField
      value={value}
      onChange={onChange}
      min={RECENT_COUNT_MIN}
      max={RECENT_COUNT_MAX}
      unit="watches"
      label={label}
      fallbackLabel={RECENT_COUNT_LABEL}
    />
  );
}
