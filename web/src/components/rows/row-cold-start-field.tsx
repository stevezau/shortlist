import { Link } from "react-router";

import { InheritableField } from "@/components/rows/inheritable-field";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  asColdStart,
  COLD_START_HINTS,
  COLD_START_LABELS,
  COLD_STARTS,
} from "@/lib/cold-start";
import { TOP_SEED } from "@/lib/placeholders";
import { coldStartGlobal } from "@/lib/row-globals";
import type { RowSettingKey } from "@/lib/row-kinds";
import type { CollectionInput, Settings } from "@/lib/types";

/** The live `recommendations.min_history`; null while settings load, so no number is claimed. */
function minHistory(settings: Settings | undefined): number | null {
  const raw = settings?.["recommendations.min_history"];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/**
 * "When someone hasn't watched enough" and, directly under it, "Name for someone who's new" — the
 * name is only ever used in that case (design §5). Each renders only when the row's kind shows it.
 */
export function ColdStartFields({
  input,
  set,
  shown,
  settings,
  namesASeed,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  shown: ReadonlySet<RowSettingKey>;
  settings: Settings | undefined;
  /** Whether the row's name follows a watch with {top_seed}. */
  namesASeed: boolean;
}) {
  const count = minHistory(settings);
  return (
    <>
      {shown.has("cold_start") && (
        <InheritableField
          setting="cold_start"
          label="When someone hasn’t watched enough"
          labelFor="row-cold-start"
          description={
            // The threshold is per person in the engine, so one number serves every row — which is
            // why this points at Settings rather than offering a per-row box.
            <>
              {count === null
                ? "Someone counts as new until they’ve watched enough titles. That number applies to every row: "
                : `Someone counts as new until they’ve watched ${count} titles. The ${count} applies to every row: `}
              <Link
                to="/settings#min-history"
                className="underline underline-offset-2 hover:text-foreground"
              >
                change it in Settings
              </Link>
              .
            </>
          }
          inheriting={input.cold_start === null}
          globalValue={coldStartGlobal(settings)}
          // Turning the toggle OFF seeds "skip", not the global: the only reason to reach for this
          // control is to differ from the global, and the global is one flip away again.
          onToggle={(on) => set({ cold_start: on ? null : "skip" })}
          // A row named after one watch is the case this exists for — it has no favourite to
          // name itself after, so it silently renders as the plain default title instead.
          before={
            namesASeed &&
            input.cold_start !== "skip" && (
              <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
                This row is named after a title they watched. With too little
                history there is no such title, so the row falls back to a
                plain name — worth skipping it for those people instead.
              </p>
            )
          }
        >
          <select
            id="row-cold-start"
            value={input.cold_start ?? "popular"}
            onChange={(e) => set({ cold_start: asColdStart(e.target.value) })}
            className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          >
            {COLD_STARTS.map((choice) => (
              <option key={choice} value={choice}>
                {COLD_START_LABELS[choice]}
              </option>
            ))}
          </select>
          <p className="text-sm text-muted-foreground">
            {COLD_START_HINTS[asColdStart(input.cold_start)]}
          </p>
        </InheritableField>
      )}

      {/* Issue #84. `{top_seed}` needs a title the person has watched, and someone new to the server
          has none — so the row has no name for them. Shortlist will not invent one, which leaves
          exactly two honest answers, and this is where the choice belongs. */}
      {shown.has("fallback_name") && (
        <div
          data-setting="fallback_name"
          className="space-y-2 rounded-md border border-dashed p-3"
        >
          <Label htmlFor="row-fallback-name">
            Name for someone who&rsquo;s new
          </Label>
          <Input
            id="row-fallback-name"
            value={input.fallback_name}
            onChange={(e) => set({ fallback_name: e.target.value })}
            placeholder="e.g. ✨ Picked for {user}"
          />
          <p className="text-sm text-muted-foreground">
            {namesASeed ? (
              <>
                This name says the row follows a watch, and someone new to your
                server hasn&rsquo;t got one — so there is nothing to put in{" "}
                <code>{TOP_SEED}</code> for them.{" "}
                {input.fallback_name.trim() ? (
                  <>
                    They&rsquo;ll get this row under the name above, filled
                    with what rates highest on your server.
                  </>
                ) : (
                  <>
                    <strong className="text-foreground">
                      Leave this empty and they simply won&rsquo;t get this row
                    </strong>{" "}
                    — which is often the right answer, since a &ldquo;because
                    you watched&rdquo; row can&rsquo;t be true for them. It
                    appears on its own once they watch enough.
                  </>
                )}
              </>
            ) : (
              <>
                Only used while the row&rsquo;s name follows a watch with{" "}
                <code>{TOP_SEED}</code>: someone new has no watch to name it
                after.
              </>
            )}
          </p>
        </div>
      )}
    </>
  );
}
