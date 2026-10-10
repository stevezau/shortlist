import { useId, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MAX_AFTER_DAYS, MAX_EASTER_OFFSET, MAX_LEAD_DAYS, clampDays, daysOffered } from "@/lib/season-draft";
import { MONTH_NAMES, WEEKDAY_NAMES } from "@/lib/seasons";
import { selectedClass, unselectedClass } from "@/lib/selected";
import type { DateRule } from "@/lib/types";
import { cn } from "@/lib/utils";

import { SELECT_CLASS } from "./select-class";

const KINDS: readonly { kind: DateRule["kind"]; label: string }[] = [
  { kind: "fixed", label: "Same day every year" },
  { kind: "nth", label: "A weekday in a month" },
  { kind: "easter", label: "Days from Easter" },
  { kind: "month", label: "Full month" },
];

const WHICH: readonly { value: number; label: string }[] = [
  { value: 1, label: "1st" },
  { value: 2, label: "2nd" },
  { value: 3, label: "3rd" },
  { value: 4, label: "4th" },
  { value: -1, label: "Last" },
];

/** Days from Easter that people know by name, so "−21" reads as Mothering Sunday. */
const EASTER_DAYS: Readonly<Record<number, string>> = {
  [-47]: "Shrove Tuesday",
  [-46]: "Ash Wednesday",
  [-21]: "Mothering Sunday",
  [-7]: "Palm Sunday",
  [-3]: "Maundy Thursday",
  [-2]: "Good Friday",
  [-1]: "Holy Saturday",
  1: "Easter Monday",
  39: "Ascension Day",
  49: "Pentecost",
};

function easterDays(offset: number): string {
  const days = Math.abs(offset);
  return `${days} day${days === 1 ? "" : "s"} ${offset < 0 ? "before" : "after"}`;
}

/** "21 days before", or a day people know by its name alone — "Mothering Sunday", in its place in the
 *  list — so every option fits a phone's narrow select; the hint under it gives a named day's distance.
 *  "Easter" is in the field's label. */
function easterOption(offset: number): string {
  if (offset === 0) return "Easter Sunday";
  return EASTER_DAYS[offset] ?? easterDays(offset);
}

const EASTER_OFFSETS = Array.from({ length: MAX_EASTER_OFFSET * 2 + 1 }, (_, i) => i - MAX_EASTER_OFFSET);

/**
 * When a season falls (#137 D7) and how long it shows (D8): a fixed day, the nth (or last) weekday of
 * a month, days from Easter, or a complete calendar month, chosen without typing dates.
 */
export function SeasonWhenFields({
  rule,
  onRule,
  leadDays,
  afterDays,
  onTiming,
  ruleError,
  nextLine,
}: {
  rule: DateRule;
  onRule: (rule: DateRule) => void;
  leadDays: number;
  afterDays: number;
  onTiming: (patch: { lead_days?: number; after_days?: number }) => void;
  /** Why the date can't be used, worded by the server; null when it can. */
  ruleError: string | null;
  /** "Next: …", or what is happening while it is worked out. */
  nextLine: ReactNode;
}) {
  const headingId = useId();
  const errorId = useId();
  const ids = { day: useId(), month: useId(), which: useId(), weekday: useId(), easter: useId() };
  const describedBy = ruleError ? errorId : undefined;
  const set = (patch: Partial<DateRule>) => onRule({ ...rule, ...patch });

  const monthSelect = (
    <Field id={ids.month} label="Month">
      <select
        id={ids.month}
        aria-describedby={describedBy}
        className={cn(SELECT_CLASS, "sm:w-36")}
        value={rule.month}
        onChange={(event) => {
          const month = Number(event.target.value);
          set({ month, day: Math.min(rule.day, daysOffered(month)) });
        }}
      >
        {MONTH_NAMES.map((name, index) => (
          <option key={name} value={index + 1}>
            {name}
          </option>
        ))}
      </select>
    </Field>
  );

  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <h3 id={headingId} className="text-base font-semibold">
        When
      </h3>
      <div role="group" aria-label="How the date is set" className="flex flex-wrap gap-2">
        {KINDS.map(({ kind, label }) => {
          const active = rule.kind === kind;
          return (
            <Button
              key={kind}
              type="button"
              size="sm"
              variant="outline"
              aria-pressed={active}
              className={active ? selectedClass : unselectedClass}
              onClick={() => {
                set({ kind });
                if (kind === "month") onTiming({ lead_days: 0, after_days: 0 });
              }}
            >
              {label}
            </Button>
          );
        })}
      </div>

      <div className="flex flex-wrap items-end gap-3">
        {rule.kind === "month" && monthSelect}
        {rule.kind === "fixed" && (
          <>
            <Field id={ids.day} label="Day">
              <select
                id={ids.day}
                aria-describedby={describedBy}
                className={cn(SELECT_CLASS, "sm:w-20")}
                value={rule.day}
                onChange={(event) => set({ day: Number(event.target.value) })}
              >
                {Array.from({ length: daysOffered(rule.month) }, (_, i) => i + 1).map((day) => (
                  <option key={day} value={day}>
                    {day}
                  </option>
                ))}
              </select>
            </Field>
            {monthSelect}
          </>
        )}
        {rule.kind === "nth" && (
          <>
            <Field id={ids.which} label="Which">
              <select
                id={ids.which}
                aria-describedby={describedBy}
                className={cn(SELECT_CLASS, "sm:w-24")}
                value={rule.nth}
                onChange={(event) => set({ nth: Number(event.target.value) })}
              >
                {WHICH.map((which) => (
                  <option key={which.value} value={which.value}>
                    {which.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field id={ids.weekday} label="Weekday">
              <select
                id={ids.weekday}
                aria-describedby={describedBy}
                className={cn(SELECT_CLASS, "sm:w-36")}
                value={rule.weekday}
                onChange={(event) => set({ weekday: Number(event.target.value) })}
              >
                {WEEKDAY_NAMES.map((name, index) => (
                  <option key={name} value={index}>
                    {name}
                  </option>
                ))}
              </select>
            </Field>
            <span className="hidden pb-2 text-sm text-muted-foreground sm:inline">of</span>
            {monthSelect}
          </>
        )}
        {rule.kind === "easter" && (
          <Field id={ids.easter} label="Days from Easter" wide>
            <select
              id={ids.easter}
              aria-describedby={describedBy}
              className={cn(SELECT_CLASS, "sm:w-auto")}
              value={rule.offset}
              onChange={(event) => set({ offset: Number(event.target.value) })}
            >
              {EASTER_OFFSETS.map((offset) => (
                <option key={offset} value={offset}>
                  {easterOption(offset)}
                </option>
              ))}
            </select>
          </Field>
        )}
      </div>
      {rule.kind === "easter" && (
        <p className="text-sm text-muted-foreground">
          {EASTER_DAYS[rule.offset] && `${EASTER_DAYS[rule.offset]} is ${easterDays(rule.offset)} Easter Sunday. `}
          Easter moves each year; Shortlist works it out for you.
        </p>
      )}
      {rule.kind === "month" && (
        <p className="text-sm text-muted-foreground">
          Shows from the first to the last day of {MONTH_NAMES[rule.month - 1]} every year, including leap years.
        </p>
      )}
      {ruleError && (
        <p id={errorId} role="alert" className="text-sm text-destructive-text">
          {ruleError}
        </p>
      )}

      {rule.kind !== "month" && <p className="flex flex-wrap items-center gap-x-2 gap-y-2 text-sm">
        <label className="inline-flex flex-wrap items-center gap-2">
          Shows from{" "}
          <Input
            type="number"
            min={0}
            max={MAX_LEAD_DAYS}
            value={leadDays}
            onChange={(event) => onTiming({ lead_days: clampDays(event.target.value, MAX_LEAD_DAYS) })}
            className="w-20"
          />{" "}
          days before
        </label>{" "}
        <label className="inline-flex flex-wrap items-center gap-2">
          and stays{" "}
          <Input
            type="number"
            min={0}
            max={MAX_AFTER_DAYS}
            value={afterDays}
            onChange={(event) => onTiming({ after_days: clampDays(event.target.value, MAX_AFTER_DAYS) })}
            className="w-20"
          />{" "}
          days after.
        </label>
      </p>}
      {nextLine && <p className="rounded-md bg-muted/60 px-3 py-2 text-sm">{nextLine}</p>}
    </section>
  );
}

function Field({ id, label, wide = false, children }: { id: string; label: string; wide?: boolean; children: ReactNode }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", wide ? "w-full sm:w-auto" : "w-[calc(50%-0.375rem)] sm:w-auto")}>
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  );
}
