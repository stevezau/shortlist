import { MAX_SEEDS_LABEL, MaxSeedsField } from "@/components/max-seeds-field";
import { InheritableField } from "@/components/rows/inheritable-field";
import { maxSeedsGlobal, maxSeedsSeed } from "@/lib/row-globals";
import type { CollectionInput, Settings } from "@/lib/types";

/**
 * "How many recent watches to match" (`max_seeds`) as a number, for Picked for You and for the
 * fill-up of a Watch it again row. A Because you watched row asks the same question as "Based on".
 */
export function RowMaxSeedsSetting({
  input,
  set,
  settings,
  min = 1,
  inheritBlockedReason = null,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  settings: Settings | undefined;
  /** The lowest count the field accepts (see `MaxSeedsField`). */
  min?: number;
  /** Why following the global would change what kind of row this is; null when it wouldn't. */
  inheritBlockedReason?: string | null;
}) {
  const inheriting = input.max_seeds === null;
  return (
    <InheritableField
      setting="max_seeds"
      label={MAX_SEEDS_LABEL}
      description="More gives a broader mix of everything they like; fewer stays close to what they’ve watched lately."
      ariaLabel="Use the global default for how many recent watches to match"
      inheriting={inheriting}
      globalValue={maxSeedsGlobal(settings)}
      // Only while the row has its own count: a row already following the global can always stop.
      toggleDisabledReason={inheriting ? null : inheritBlockedReason}
      // Stops at the global's own value, like every other dial here: turning the switch off stops
      // tracking the global, it doesn't change what the row does.
      onToggle={(on) =>
        set({ max_seeds: on ? null : Math.max(min, maxSeedsSeed(settings)) })
      }
    >
      <MaxSeedsField
        label=""
        min={min}
        value={input.max_seeds ?? 0}
        onChange={(next) => set({ max_seeds: next })}
      />
    </InheritableField>
  );
}
