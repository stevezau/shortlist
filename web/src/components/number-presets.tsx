import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** One preset chip: the stored number and the label shown on it (e.g. 0 → "All", 45 → "45s"). */
export interface NumberPreset {
  value: number;
  label: string;
}

/**
 * A row of preset chips with a "Custom…" escape hatch — the same segmented look as {@link Segmented},
 * but any value outside the presets is allowed via a number input. The chip for the current value is
 * active; if the stored value matches no preset (or the owner picks "Custom…"), the input shows and
 * carries it. Used for the power-user knobs (Plex timeout, Runs kept, Run concurrency) where the
 * presets cover the common cases but an operator may need an exact figure the boxes don't offer.
 */
export function NumberPresets({
  value,
  presets,
  onChange,
  min,
  max,
  step = 1,
  unit,
  ariaLabel,
}: {
  value: number;
  presets: NumberPreset[];
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  /** Appended after the input (e.g. "seconds") so the custom field reads in the same unit as a chip. */
  unit?: string;
  ariaLabel: string;
}) {
  const matchesPreset = presets.some((p) => p.value === value);
  // "Custom" is sticky: once opened it stays open even while the typed value happens to equal a
  // preset, so a half-typed number doesn't yank the input away. A value that arrives already
  // off-preset (loaded from the server) opens it too.
  const [custom, setCustom] = useState(!matchesPreset);
  // Local text mirror so an empty/partial field is editable without forcing it through Number()
  // every keystroke (which would turn "" into 0 and clamp mid-typing).
  const [draft, setDraft] = useState(String(value));

  useEffect(() => {
    if (!matchesPreset) setCustom(true);
  }, [matchesPreset]);

  const commitDraft = (raw: string) => {
    setDraft(raw);
    const n = Number(raw);
    if (raw.trim() === "" || Number.isNaN(n)) return; // wait for a real number
    const clamped = Math.min(max, Math.max(min, Math.round(n / step) * step));
    if (clamped !== value) onChange(clamped);
  };

  return (
    <div role="group" aria-label={ariaLabel} className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {presets.map((preset) => (
          <Button
            key={preset.value}
            type="button"
            size="sm"
            variant={!custom && value === preset.value ? "default" : "outline"}
            aria-pressed={!custom && value === preset.value}
            onClick={() => {
              setCustom(false);
              onChange(preset.value);
            }}
          >
            {preset.label}
          </Button>
        ))}
        <Button
          type="button"
          size="sm"
          variant={custom ? "default" : "outline"}
          aria-pressed={custom}
          onClick={() => {
            setDraft(String(value));
            setCustom(true);
          }}
        >
          Custom…
        </Button>
      </div>
      {custom && (
        <div className="flex items-center gap-2">
          <Input
            type="number"
            inputMode="numeric"
            min={min}
            max={max}
            step={step}
            value={draft}
            aria-label={`${ariaLabel} (custom value)`}
            className="w-28"
            onChange={(e) => commitDraft(e.target.value)}
            onBlur={() => setDraft(String(value))}
          />
          {unit && (
            <span className="text-sm text-muted-foreground">{unit}</span>
          )}
        </div>
      )}
    </div>
  );
}
