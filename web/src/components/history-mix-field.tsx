import { useState } from "react";

import { NumberField } from "@/components/number-field";
import { Button } from "@/components/ui/button";
import { MIX_MAX, MIX_PRESETS, mixName, presetFor } from "@/lib/history-mix";
import { selectedClass, unselectedClass } from "@/lib/selected";
import { cn } from "@/lib/utils";

export const HISTORY_MIX_LABEL = "How much of their history it searches from";
export const HISTORY_MIX_HELP =
  "Recent watches are always included. The presets add long-time favourites and older watches from across the years.";

export interface HistoryMix {
  favourites: number;
  older: number;
}

/**
 * The two history-mix counts as presets (Recent only … Deep) plus a Custom pair of number inputs.
 * The selected preset is derived from the counts, so it can never disagree with them.
 *
 * Pass `inherited` for a row or person that can follow a wider default: a first option, "<prefix>
 * (<preset>)", selects `null`. `value` is then `null` while inheriting.
 */
export function HistoryMixField({
  value,
  onChange,
  inherited,
  modeLine,
  label = HISTORY_MIX_LABEL,
}: {
  value: HistoryMix | null;
  onChange: (next: HistoryMix | null) => void;
  inherited?: { prefix: string; mix: HistoryMix };
  /** The cost of one more group of searches in the configured setup; null while settings load. */
  modeLine?: string | null;
  /** Caption above the presets. Pass "" when the surrounding block already renders one. */
  label?: string;
}) {
  const [customOpen, setCustomOpen] = useState(false);
  const current = value ?? inherited?.mix ?? { favourites: 0, older: 0 };
  const preset = presetFor(current.favourites, current.older);
  const inheriting = value === null;
  const showCustom = !inheriting && (customOpen || preset === null);

  const options: { id: string; label: string; selected: boolean; pick: () => void }[] = [];
  if (inherited) {
    options.push({
      id: "inherit",
      label: `${inherited.prefix} (${mixName(inherited.mix.favourites, inherited.mix.older)})`,
      selected: inheriting,
      pick: () => {
        setCustomOpen(false);
        onChange(null);
      },
    });
  }
  for (const p of MIX_PRESETS) {
    options.push({
      id: p.id,
      label: p.label,
      selected: !inheriting && !showCustom && preset?.id === p.id,
      pick: () => {
        setCustomOpen(false);
        onChange({ favourites: p.favourites, older: p.older });
      },
    });
  }
  options.push({
    id: "custom",
    label: "Custom",
    selected: showCustom,
    pick: () => {
      setCustomOpen(true);
      if (inheriting) onChange(current);
    },
  });

  return (
    <div className="space-y-2">
      {label ? <p className="text-sm font-medium">{label}</p> : null}
      <div role="radiogroup" aria-label={HISTORY_MIX_LABEL} className="flex flex-wrap gap-2">
        {options.map((option) => (
          <Button
            key={option.id}
            type="button"
            role="radio"
            size="sm"
            variant="outline"
            aria-checked={option.selected}
            className={cn(option.selected ? selectedClass : unselectedClass)}
            onClick={option.pick}
          >
            {option.label}
          </Button>
        ))}
      </div>
      {showCustom && (
        <div className="space-y-3">
          <NumberField
            value={current.favourites}
            onChange={(favourites) => onChange({ ...current, favourites })}
            min={0}
            max={MIX_MAX}
            unit="titles"
            label="long-time favourites (films watched twice+, shows finished or 20+ episodes, rated 4 stars+)"
            fallbackLabel="long-time favourites"
          />
          <NumberField
            value={current.older}
            onChange={(older) => onChange({ ...current, older })}
            min={0}
            max={MIX_MAX}
            unit="titles"
            label="older watches (one from each part of their history, a new set each week)"
            fallbackLabel="older watches"
          />
        </div>
      )}
      {modeLine ? <p className="text-xs text-muted-foreground">{modeLine}</p> : null}
    </div>
  );
}
