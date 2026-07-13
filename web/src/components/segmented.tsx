import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";

/**
 * A single-select segmented control — a row of chip buttons where exactly one is active. Used for
 * every "pick one" choice in the app (cadence, row size, media, tone, built-how) so they all look
 * and behave identically. Pass `legend` to wrap the group in a `<fieldset>` with an accessible
 * caption; omit it when the control already sits under its own label.
 */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  legend,
  ariaLabel,
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  /** Visible caption; when set, the buttons are wrapped in a labelled fieldset. */
  legend?: string;
  /** Screen-reader label when there is no visible legend. */
  ariaLabel?: string;
}) {
  const buttons = (
    <div className="flex flex-wrap gap-2">
      {options.map((option) => (
        <Button
          key={option.value}
          type="button"
          size="sm"
          variant={value === option.value ? "default" : "outline"}
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </Button>
      ))}
    </div>
  );

  if (legend) {
    return (
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">{legend}</legend>
        {buttons}
      </fieldset>
    );
  }
  return (
    <div role="group" aria-label={ariaLabel}>
      {buttons}
    </div>
  );
}
