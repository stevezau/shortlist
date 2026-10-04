import { useId, useState } from "react";

import { SELECT_CLASS } from "@/components/rows/seasons/select-class";
import { QueryBoundary } from "@/components/query-boundary";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { usesTheme } from "@/lib/placeholders";
import { useCollections } from "@/lib/queries";
import type { Collection, CollectionInput } from "@/lib/types";

/** What each choice means; null is the usual third, and what a row stores until someone changes it. */
const SHARE_OPTIONS: { label: string; value: number | null }[] = [
  { label: "A little (about a fifth)", value: 0.2 },
  { label: "A third (usual)", value: null },
  { label: "Half", value: 0.5 },
  { label: "Almost everything", value: 0.8 },
];

/** The API's limit on the cooldown, and where switching it on starts. */
const MIN_COOLDOWN = 1;
const MAX_COOLDOWN = 365;
const DEFAULT_COOLDOWN = 30;

/**
 * How an AI row changes between runs (#138): how much of the list is swapped each time, how long a title
 * stays out once shown, and which of the person's other rows it keeps out of. Only an AI row has these.
 * Every control starts at today's behaviour.
 */
export function OverTimeFields({
  input,
  ownSlug,
  onChange,
}: {
  input: Pick<CollectionInput, "refresh_share" | "repeat_cooldown_days" | "avoid_rows">;
  /** The saved row's slug, so it is never offered as a row to keep out of itself; null for a new row. */
  ownSlug: string | null;
  onChange: (patch: Partial<CollectionInput>) => void;
}) {
  const shareId = useId();
  const cooldownId = useId();
  const daysId = useId();
  const share = input.refresh_share;
  const known = SHARE_OPTIONS.some((option) => option.value === share);
  const cooldownOn = input.repeat_cooldown_days !== null;

  return (
    <section aria-label="How the row changes over time" className="space-y-5 border-t pt-4">
      <h3 className="text-sm font-semibold">How the row changes over time</h3>

      <div className="space-y-2">
        <Label htmlFor={shareId}>How much changes each time</Label>
        <select
          id={shareId}
          className={SELECT_CLASS}
          value={share === null ? "" : String(share)}
          onChange={(event) => onChange({ refresh_share: event.target.value === "" ? null : Number(event.target.value) })}
        >
          {SHARE_OPTIONS.map((option) => (
            <option key={option.label} value={option.value === null ? "" : String(option.value)}>
              {option.label}
            </option>
          ))}
          {/* A value set through the API that the list doesn't offer is shown, not silently replaced. */}
          {!known && share !== null && <option value={String(share)}>{`About ${Math.round(share * 100)}%`}</option>}
        </select>
        <p className="text-sm text-muted-foreground">
          On each refresh, this much of what a person sees is swapped for new titles.
        </p>
      </div>

      <div className="space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <Label htmlFor={cooldownId}>Don’t repeat a title for a while</Label>
            <p className="text-sm text-muted-foreground">
              Once a title has been in a person’s row, it stays out for this long. Off by default.
            </p>
          </div>
          <Switch
            id={cooldownId}
            checked={cooldownOn}
            onCheckedChange={(on) => onChange({ repeat_cooldown_days: on ? DEFAULT_COOLDOWN : null })}
          />
        </div>
        {cooldownOn && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Label htmlFor={daysId} className="font-normal">
              Don’t repeat a title for
            </Label>
            <DaysInput
              id={daysId}
              label="Days before a title can repeat"
              value={input.repeat_cooldown_days ?? DEFAULT_COOLDOWN}
              min={MIN_COOLDOWN}
              max={MAX_COOLDOWN}
              onCommit={(days) => onChange({ repeat_cooldown_days: days })}
            />
            <span>days</span>
          </div>
        )}
      </div>

      <AvoidRows ownSlug={ownSlug} chosen={input.avoid_rows ?? []} onChange={(rows) => onChange({ avoid_rows: rows })} />
    </section>
  );
}

/**
 * A whole-number box that keeps what is being typed. A controlled number box that refuses an in-between
 * value (empty, or out of range) snaps back and eats the next keystroke, so the text is held here and
 * only a valid number is handed up. Leaving the box shows the last valid number again.
 */
export function DaysInput({
  id,
  label,
  value,
  min,
  max,
  onCommit,
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  onCommit: (days: number) => void;
}) {
  const [text, setText] = useState(String(value));
  // The value can change from outside (Discard puts the saved one back); typing that already says it is left be.
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    if (text === "" || Number(text) !== value) setText(String(value));
  }
  return (
    <Input
      id={id}
      aria-label={label}
      type="number"
      inputMode="numeric"
      min={min}
      max={max}
      className="w-24"
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        const days = Number(event.target.value);
        if (event.target.value !== "" && Number.isInteger(days) && days >= min && days <= max) onCommit(days);
      }}
      onBlur={() => setText(String(value))}
    />
  );
}

/** An AI row is named from its theme (`{theme_emoji} {theme}`), which means nothing until a person has one. */
function friendlyName(row: Collection): string {
  return usesTheme(row.name) ? `AI row (${row.slug})` : row.name;
}

function AvoidRows({
  ownSlug,
  chosen,
  onChange,
}: {
  ownSlug: string | null;
  chosen: string[];
  onChange: (rows: string[] | null) => void;
}) {
  const rows = useCollections();
  const toggle = (slug: string) => {
    const next = chosen.includes(slug) ? chosen.filter((s) => s !== slug) : [...chosen, slug];
    onChange(next.length === 0 ? null : next);
  };
  return (
    <fieldset className="space-y-2">
      <legend className="text-sm font-medium">Keep out titles already in:</legend>
      {rows.isError ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm">
          <p>Couldn’t read your other rows.</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void rows.refetch()}>
            Read them again
          </Button>
        </div>
      ) : (
      <QueryBoundary
        query={rows}
        skeleton={
          <div role="status" aria-label="Checking the other rows" className="space-y-2">
            <Skeleton className="h-5 w-1/2" />
            <Skeleton className="h-5 w-1/3" />
          </div>
        }
        isEmpty={(all) => !all.some((row) => row.build === "per_person" && row.slug !== ownSlug)}
        empty={<p className="text-sm text-muted-foreground">No other rows to compare with.</p>}
      >
        {(all) => (
          <>
            <ul className="space-y-1">
              {all
                .filter((row) => row.build === "per_person" && row.slug !== ownSlug)
                .map((row) => (
                  <li key={row.slug}>
                    <label className="flex cursor-pointer items-start gap-2.5 rounded-md px-2 py-1.5 text-sm hover:bg-muted/50 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
                      <input
                        type="checkbox"
                        checked={chosen.includes(row.slug)}
                        onChange={() => toggle(row.slug)}
                        className="mt-0.5 h-4 w-4 shrink-0 accent-primary focus-visible:outline-none"
                      />
                      {friendlyName(row)}
                    </label>
                  </li>
                ))}
            </ul>
            <p className="text-sm text-muted-foreground">
              A title in any of these rows for the same person is left out of this one. Rows are built one after
              another, so a row built later keeps out what an earlier one picked.
            </p>
          </>
        )}
      </QueryBoundary>
      )}
    </fieldset>
  );
}
