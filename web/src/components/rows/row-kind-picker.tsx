import type { ReactNode } from "react";
import { useId } from "react";

import {
  FILL_META,
  KIND_GROUP,
  KIND_META,
  ROW_KINDS,
  SEASONAL_FILLS,
  type KindMeta,
  type RowFill,
  type RowKind,
} from "@/lib/row-kinds";
import { cn } from "@/lib/utils";
import { selectedVerticalClass } from "@/lib/selected";

/**
 * A radio list where each choice is a title plus one line on what it means. Native radios, so the
 * arrow keys, grouping and "1 of 5" announcements come from the browser rather than from us.
 *
 * Each radio is named by its title alone and described by its line, so assistive tech reads
 * "Watch it again, radio" and then the sentence, instead of both run together as one name.
 */
function RadioCards<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  /** The group's accessible name. */
  label: string;
  value: T;
  /** `disabledReason` null means the choice can be picked; anything else is why it can't. */
  options: (KindMeta & { value: T; disabledReason: ReactNode | null })[];
  onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <div role="radiogroup" aria-label={label} className="space-y-2">
      {options.map((option) => {
        const id = `${name}-${option.value}`;
        const checked = option.value === value;
        const disabled = option.disabledReason !== null;
        return (
          <label
            key={option.value}
            htmlFor={id}
            className={cn(
              "flex items-start gap-3 rounded-md border px-3 py-2 text-sm transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring",
              checked && selectedVerticalClass,
              disabled
                ? "cursor-not-allowed opacity-60"
                : "cursor-pointer hover:bg-muted/50",
            )}
          >
            <input
              id={id}
              type="radio"
              name={name}
              value={option.value}
              checked={checked}
              disabled={disabled}
              onChange={() => onChange(option.value)}
              aria-labelledby={`${id}-title`}
              aria-describedby={`${id}-description`}
              className="mt-0.5 h-4 w-4 shrink-0 accent-primary focus-visible:outline-none"
            />
            <span className="min-w-0 flex-1">
              <span id={`${id}-title`} className="block font-medium">
                {option.title}
              </span>
              <span
                id={`${id}-description`}
                className="block text-muted-foreground"
              >
                {option.description}
                {option.disabledReason && (
                  <span className="block italic">{option.disabledReason}</span>
                )}
              </span>
            </span>
          </label>
        );
      })}
    </div>
  );
}

/** "What kind of row is this?" — the six kinds, in the picker's order, with the design's copy. */
export function RowKindPicker({
  value,
  onChange,
  disabledReason,
}: {
  value: RowKind;
  onChange: (kind: RowKind) => void;
  /** Why a kind can't be picked right now, or null when it can. */
  disabledReason: (kind: RowKind) => ReactNode | null;
}) {
  return (
    <RadioCards
      label={KIND_GROUP.title}
      value={value}
      onChange={onChange}
      options={ROW_KINDS.map((kind) => ({
        value: kind,
        ...KIND_META[kind],
        disabledReason: disabledReason(kind),
      }))}
    />
  );
}

/** A seasonal row's "How it's filled": every kind but Seasonal and Your requests (a request lands
 *  when it lands, so no season decides whether it shows). */
export function RowFillPicker({
  value,
  onChange,
}: {
  value: RowFill;
  onChange: (fill: RowFill) => void;
}) {
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">
        How it&rsquo;s filled
      </p>
      <p className="text-sm text-muted-foreground">
        While a season is on, the row is filled in one of these ways. Its
        settings follow below.
      </p>
      <RadioCards
        label="How it's filled"
        value={value}
        onChange={onChange}
        options={SEASONAL_FILLS.map((fill) => ({
          value: fill,
          ...FILL_META[fill],
          disabledReason: null,
        }))}
      />
    </div>
  );
}
