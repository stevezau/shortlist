import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { selectedClass } from "@/lib/selected";
import { cn } from "@/lib/utils";

/**
 * A whole-number field with its own text buffer, shared by the row-size, seed-budget and
 * recent-count pickers. The buffer lets a value be cleared and retyped without the field fighting
 * the user; the clamped whole number is pushed up only on blur/Enter (and on the browser spinner),
 * so autosave never fires mid-type with an out-of-range value.
 */
export function NumberField({
  value,
  onChange,
  min,
  max,
  unit,
  label,
  fallbackLabel,
  fallback = min,
  hint,
  presets,
  presetLabel,
}: {
  value: number;
  onChange: (next: number) => void;
  min: number;
  max: number;
  /** Word after the input ("titles", "watches"). */
  unit: string;
  /** Caption above the input. Pass "" when the surrounding block already renders one — an
   *  `InheritableField` does, and rendering both printed the same heading twice. */
  label: string;
  /** Accessible name used only when `label` is suppressed: an aria-label WINS over a <label>, so
   *  setting it unconditionally would override a caller's own caption. */
  fallbackLabel: string;
  /** What an unparseable entry (e.g. "e") becomes. */
  fallback?: number;
  hint?: string;
  /** Optional quick choices; the free number field always remains available. */
  presets?: readonly number[];
  presetLabel?: (preset: number) => string;
}) {
  const id = useId();
  const [text, setText] = useState(String(value));
  // Re-sync the buffer when the saved value changes from elsewhere (reset, another tab).
  // Adjusted during render rather than in an effect: React re-runs this component immediately
  // without committing the discarded render, so the input never paints the stale text. An effect
  // would paint stale, then correct it on the next frame.
  const [syncedValue, setSyncedValue] = useState(value);
  if (syncedValue !== value) {
    setSyncedValue(value);
    setText(String(value));
  }

  const clamp = (n: number) => (Number.isFinite(n) ? Math.max(min, Math.min(max, Math.round(n))) : fallback);

  const commit = () => {
    const next = text.trim() === "" ? value : clamp(Number(text));
    setText(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <div className="space-y-1.5">
      {label ? <Label htmlFor={id}>{label}</Label> : null}
      <div className="flex flex-wrap items-center gap-2">
        {presets?.map((preset) => (
          <Button
            key={preset}
            type="button"
            variant="outline"
            aria-label={presetLabel?.(preset)}
            aria-pressed={value === preset}
            className={cn("min-w-12", value === preset && selectedClass)}
            onClick={() => onChange(preset)}
          >
            {preset}
          </Button>
        ))}
        <Input
          id={id}
          aria-label={label ? undefined : fallbackLabel}
          type="number"
          inputMode="numeric"
          min={min}
          max={max}
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
        <span className="text-sm text-muted-foreground">{unit}</span>
      </div>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
