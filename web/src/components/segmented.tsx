import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { selectedClass, unselectedClass } from "@/lib/selected";
import { cn } from "@/lib/utils";

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
  joined = false,
}: {
  value: T;
  options: {
    value: T;
    label: ReactNode;
    disabled?: boolean;
    reason?: string;
  }[];
  onChange: (value: T) => void;
  /** Visible caption; when set, the buttons are wrapped in a labelled fieldset. */
  legend?: string;
  /** Screen-reader label when there is no visible legend. */
  ariaLabel?: string;
  /** One bordered bar with hairlines between the options, rather than a row of separate chips — for
   *  a filter or a window that sits in a header row beside other controls. */
  joined?: boolean;
}) {
  const buttons = joined ? (
    // Scrolls inside itself rather than wrapping: a joined bar broken over two lines reads as two
    // controls, and at 320px it must never push the page sideways. The focus ring is inset because
    // the scroller clips anything drawn outside it.
    <div className="inline-flex max-w-full divide-x divide-border overflow-x-auto rounded-lg border border-border-strong bg-elevated">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          disabled={option.disabled}
          title={option.disabled ? option.reason : undefined}
          onClick={() => onChange(option.value)}
          className={cn(
            "inline-flex h-8 shrink-0 items-center whitespace-nowrap px-2 text-[13px] sm:px-3 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none",
            value === option.value ? selectedClass : "text-muted-foreground hover:bg-raised hover:text-foreground",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  ) : (
    <div className="flex flex-wrap gap-2">
      {options.map((option) => {
        const button = (
          <Button
            key={option.value}
            type="button"
            size="sm"
            variant="outline"
            className={cn(value === option.value ? selectedClass : unselectedClass)}
            aria-pressed={value === option.value}
            disabled={option.disabled}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </Button>
        );
        if (!option.disabled || !option.reason) return button;
        // A wrapper that DOES take pointer events, so hovering a dead option answers "why can't I
        // pick this?" in place. `aria-describedby` is not usable here (the reason has no element of
        // its own), so the text is also exposed to assistive tech via `aria-label`, and a caller
        // that wants it visible renders it as a hint as well — a disabled button takes no focus, so
        // hover alone would leave keyboard users with nothing.
        return (
          <span
            key={option.value}
            title={option.reason}
            aria-label={option.reason}
            className="inline-flex cursor-not-allowed"
          >
            {button}
          </span>
        );
      })}
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
    // `min-w-0` lets a joined bar inside a flex row shrink to the row and scroll, rather than
    // holding the row open at its full width (a flex item's minimum is its content's, 384px at 320).
    <div role="group" aria-label={ariaLabel} className={joined ? "min-w-0 max-w-full" : undefined}>
      {buttons}
    </div>
  );
}
