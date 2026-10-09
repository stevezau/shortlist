import { NumberField } from "@/components/number-field";

const MAX_SEEDS_MIN = 1;
const MAX_SEEDS_MAX = 100;

/** The one name this setting goes by, everywhere it appears (Settings → Finding titles and the row
 *  editor). It names its SCOPE — every source — because its neighbour, {@link RecentCountField},
 *  slices the front of this same list for a single source and the two were previously told apart
 *  only by wording. Exported so Settings, the row editor and the rename page import the name rather
 *  than retype it — the last rename shipped to some of those screens and not others. */
export const MAX_SEEDS_LABEL = "How many recent watches to match";

/**
 * Picker for how many watched titles a row is built from ({@link MAX_SEEDS_MIN}..{@link MAX_SEEDS_MAX}).
 * Same self-buffering behaviour as {@link RecentCountField}.
 */
export function MaxSeedsField({
  value,
  onChange,
  label = MAX_SEEDS_LABEL,
  min = MAX_SEEDS_MIN,
}: {
  value: number;
  onChange: (count: number) => void;
  /** The lowest count this caller accepts. A Picked for You row passes 3: 1 or 2 would make it a
   *  Because you watched row (`row-kinds.ts`), a change only the kind picker should make. */
  min?: number;
  /** Caption above the input. Pass "" when the surrounding block already renders one. */
  label?: string;
}) {
  return (
    <NumberField
      value={value}
      onChange={onChange}
      min={min}
      max={MAX_SEEDS_MAX}
      unit="watches"
      label={label}
      fallbackLabel={MAX_SEEDS_LABEL}
    />
  );
}
