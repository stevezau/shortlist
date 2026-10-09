import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { selectedClass } from "@/lib/selected";
import { cn } from "@/lib/utils";
import { ROW_SIZE_MAX, ROW_SIZE_MIN, clampRowSize } from "@/lib/constants";

/**
 * A free row-size picker: any whole number of titles from {@link ROW_SIZE_MIN} to
 * {@link ROW_SIZE_MAX}. Keeps its own text buffer so a value can be cleared and retyped without the
 * field fighting the user; the clamped whole number is only pushed up on blur/Enter (and on the
 * browser spinner), so autosave never fires with an out-of-range value.
 */
export function RowSizeField({
  value,
  onChange,
  label = "How many titles",
  hint = `Any number of titles from ${ROW_SIZE_MIN} to ${ROW_SIZE_MAX}.`,
  presets,
}: {
  value: number;
  onChange: (size: number) => void;
  label?: string;
  hint?: string;
  /** Optional quick choices; the free number field always remains available. */
  presets?: readonly number[];
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

  const commit = () => {
    const next = text.trim() === "" ? value : clampRowSize(Number(text));
    setText(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <div className="flex flex-wrap items-center gap-2">
        {presets?.map((size) => (
          <Button key={size} type="button" variant="outline"
            aria-label={`${size} titles`} aria-pressed={value === size}
            className={cn("min-w-12", value === size && selectedClass)} onClick={() => onChange(size)}>{size}</Button>
        ))}
        <Input
          id={id}
          type="number"
          inputMode="numeric"
          min={ROW_SIZE_MIN}
          max={ROW_SIZE_MAX}
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
        <span className="text-sm text-muted-foreground">titles</span>
      </div>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}
