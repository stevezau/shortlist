import { SeedWindowField } from "@/components/seed-window-field";
import { Button } from "@/components/ui/button";
import type { RowSettingKey } from "@/lib/row-kinds";
import type { CollectionInput } from "@/lib/types";

/** Why "Take turns" is shown but disabled on a Because you watched blend (design §5). */
const TAKE_TURNS_BLEND_REASON =
  "Doesn't work with a blend of 3 or more. Choose one of the other Based on options to use it.";

/**
 * "Take turns between their last [N] watches" (`seed_window`).
 *
 * Rendered wherever the row's kind shows it — enabled only while the row is built from 1 or 2 watches
 * — and ALSO wherever the kind hides it but the row still holds a rotation the engine applies
 * (`hiddenButRead`). Then it says so and offers Reset: a hidden setting must never quietly change
 * what a row does.
 */
export function TakeTurnsSetting({
  input,
  set,
  shown,
  hidden,
  enabled,
  hiddenWhy,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  shown: ReadonlySet<RowSettingKey>;
  hidden: readonly RowSettingKey[];
  /** `takeTurnsEnabled` for this row. */
  enabled: boolean;
  /** Why this row doesn't use rotation, for the note when it's still set: "Picked for You rows don't
   *  take turns". */
  hiddenWhy: string;
}) {
  const usable = shown.has("seed_window");
  const stale = hidden.includes("seed_window");
  if (!usable && !stale) return null;
  const editable = usable && enabled;
  const watches = `${input.seed_window} watches`;

  return (
    <div data-setting="seed_window" className="space-y-2 border-t pt-4">
      <SeedWindowField
        value={input.seed_window}
        onChange={(seed_window) => set({ seed_window })}
        disabled={!editable}
      />
      {usable && !enabled && (
        <p className="text-sm text-muted-foreground">{TAKE_TURNS_BLEND_REASON}</p>
      )}
      {editable && input.seed_window > 1 && (
        <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
          Cycling rebuilds this row whenever the watch changes, so it writes to
          Plex most nights. Set it back to 1 if you&rsquo;d rather the row only
          changed when they actually watch something new.
        </p>
      )}
      {stale && (
        <div className="flex flex-wrap items-start gap-3 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
          <p className="min-w-0 flex-1">
            {usable
              ? `This row is still set to take turns between their last ${watches}, and it still does, even as a blend. That rebuilds it every night.`
              : `${hiddenWhy}, but this one is still set to take turns between their last ${watches}, and it still does. That rebuilds it every night.`}{" "}
            Reset puts it back to 1.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => set({ seed_window: 1 })}
          >
            Reset
          </Button>
        </div>
      )}
    </div>
  );
}
