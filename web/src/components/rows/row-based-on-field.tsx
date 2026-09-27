import type { ReactNode } from "react";
import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { TOP_SEED } from "@/lib/placeholders";
import { maxSeedsGlobal } from "@/lib/row-globals";
import {
  effectiveMaxSeeds,
  namesASeed,
  type RowKindContext,
} from "@/lib/row-kinds";
import type { CollectionInput, Settings } from "@/lib/types";
import { cn } from "@/lib/utils";

const BLEND_MAX = 100;

function RadioRow({
  id,
  name,
  checked,
  onSelect,
  labelledBy,
  describedBy,
  disabled = false,
  children,
}: {
  id: string;
  name: string;
  checked: boolean;
  onSelect: () => void;
  labelledBy?: string;
  describedBy?: string;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border px-3 py-2 text-sm has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring",
        checked && "border-primary bg-primary/5",
        disabled && "opacity-60",
      )}
    >
      <input
        id={id}
        type="radio"
        name={name}
        checked={checked}
        disabled={disabled}
        onChange={onSelect}
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        className="h-4 w-4 shrink-0 accent-primary focus-visible:outline-none"
      />
      {children}
    </div>
  );
}

/**
 * "Based on": which of their watches a Because you watched row follows (design §5). It writes the
 * watch count (`max_seeds`), offered as the choices that make sense for the row's media instead of
 * a bare number.
 *
 * Choosing a blend wider than 2 also stops taking turns — the engine can't rotate a blend, and a
 * rotation left behind would keep rebuilding the row nightly with nothing on screen to say why.
 *
 * A row named after a watch can also blend the global default's count (`max_seeds: null`). Only that
 * row: without a {top_seed} name, following a global of 3 or more reads as Picked for You.
 */
export function BasedOnField({
  input,
  set,
  ctx,
  settings,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  ctx: RowKindContext;
  settings: Settings | undefined;
}) {
  const group = useId();
  const both = input.media === "both";
  const noun = input.media === "movie" ? "film" : "show";
  const effective = effectiveMaxSeeds(input, ctx);
  const blendMin = both ? 3 : 2;
  const blendSelected = effective >= blendMin;
  // Only a name that follows a watch keeps a blend of 3 or more a Because you watched row; without
  // one it reads as Picked for You (`rowKindOf`), a change that has to go through the kind picker and
  // its confirm dialog rather than happen under the cursor here.
  const named = namesASeed(input, ctx);
  const blendMax = named ? BLEND_MAX : 2;
  const blendAvailable = blendMax >= blendMin;
  const inheriting = input.max_seeds === null;
  // Only where following the global is a blend; a global of 1 or 2 is one of the single choices.
  const canInherit = named && ctx.globalMaxSeeds >= blendMin;
  const globalCount = settings?.["recommendations.max_seeds"];
  const inheritLabel = `Use the global default${typeof globalCount === "number" ? ` (${globalCount})` : ""}`;

  const [text, setText] = useState(String(Math.max(blendMin, effective)));
  // Re-sync when the count changes from elsewhere (another choice, a kind switch).
  const [synced, setSynced] = useState(effective);
  if (synced !== effective) {
    setSynced(effective);
    if (effective >= blendMin) setText(String(effective));
  }

  // Only a number someone actually typed selects the blend: tabbing through the box on the way
  // past must not quietly change what the row is based on.
  const [typing, setTyping] = useState(false);

  const chooseBlend = () => {
    const typed = Math.round(Number(text));
    const count =
      text.trim() !== "" && Number.isFinite(typed)
        ? Math.min(blendMax, Math.max(blendMin, typed))
        : Math.max(blendMin, effective);
    setText(String(count));
    if (!blendSelected || count !== effective) {
      set({ max_seeds: count, ...(count > 2 ? { seed_window: 1 } : {}) });
    }
  };
  const commitTyped = () => {
    if (!typing) return;
    setTyping(false);
    chooseBlend();
  };

  const inherit = (on: boolean) =>
    set(
      on
        ? { max_seeds: null, ...(ctx.globalMaxSeeds > 2 ? { seed_window: 1 } : {}) }
        : // Stops at the global's own value: turning the switch off doesn't change what the row does.
          { max_seeds: Math.max(blendMin, effective) },
    );

  const single: { value: number; label: string }[] = both
    ? [
        { value: 2, label: "Their latest film and their latest show" },
        { value: 1, label: "Only the very last thing they watched" },
      ]
    : [{ value: 1, label: `Their latest ${noun}` }];
  const globalLabel = maxSeedsGlobal(settings);

  return (
    <div data-setting="based_on" className="space-y-2">
      <p id={`${group}-legend`} className="text-sm font-medium">
        Based on
      </p>
      <div
        role="radiogroup"
        aria-labelledby={`${group}-legend`}
        className="space-y-2"
      >
        {single.map((option) => (
          <RadioRow
            key={option.value}
            id={`${group}-${option.value}`}
            name={group}
            checked={!blendSelected && effective === option.value}
            onSelect={() => set({ max_seeds: option.value })}
          >
            <label htmlFor={`${group}-${option.value}`} className="min-w-0 flex-1 cursor-pointer">
              {option.label}
            </label>
          </RadioRow>
        ))}
        <RadioRow
          id={`${group}-blend`}
          name={group}
          checked={blendSelected}
          onSelect={chooseBlend}
          labelledBy={`${group}-blend-before ${group}-blend-after`}
          describedBy={blendAvailable ? undefined : `${group}-blend-reason`}
          disabled={!blendAvailable}
        >
          <label
            id={`${group}-blend-before`}
            htmlFor={`${group}-blend`}
            className={blendAvailable ? "cursor-pointer" : "cursor-not-allowed"}
          >
            A blend of their last
          </label>
          <Input
            aria-label={`How many ${both ? "watches" : `${noun}s`} to blend`}
            type="number"
            inputMode="numeric"
            min={blendMin}
            max={blendMax}
            disabled={!blendAvailable || (canInherit && inheriting)}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setTyping(true);
            }}
            onBlur={commitTyped}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                commitTyped();
              }
            }}
            className="w-20"
          />
          <span id={`${group}-blend-after`}>{both ? "watches" : `${noun}s`}</span>
          {canInherit && (
            <span className="flex basis-full items-center justify-between gap-4">
              <Label htmlFor={`${group}-inherit`} className="font-normal">
                {inheritLabel}
              </Label>
              <Switch
                id={`${group}-inherit`}
                checked={inheriting}
                onCheckedChange={inherit}
              />
            </span>
          )}
          {!blendAvailable && (
            <span
              id={`${group}-blend-reason`}
              className="basis-full text-muted-foreground"
            >
              A blend of 3 or more is a Picked for You row unless its name
              follows a watch with {TOP_SEED}. To blend more, switch the kind to
              Picked for You.
            </span>
          )}
        </RadioRow>
      </div>
      {inheriting && !canInherit && (
        <p className="text-xs text-muted-foreground">
          Following the global default
          {globalLabel ? ` of ${globalLabel}` : ""}. Pick one of these to set
          this row&rsquo;s own.
        </p>
      )}
      {named && blendSelected && (
        // `{top_seed}` names the best-matching pick's watch, one of the blend; and rotation needs a
        // row built from 1 or 2 watches (`takeTurnsEnabled`).
        <p className="text-sm text-muted-foreground">
          Picks mix all of these watches, but the name only mentions the latest
          one{effective > 2 ? ", and it can't take turns while blending" : ""}.
        </p>
      )}
      {both && !blendSelected && effective === 1 && (
        // Seeds are balanced across the media types present, so one watch seeds only its own type
        // and the other library's collection never builds (`namedRowSeeds`).
        <p className="text-sm text-muted-foreground">
          A watch is either a film or a show, never both, so one of your two
          libraries would get nothing to build from.
        </p>
      )}
    </div>
  );
}
