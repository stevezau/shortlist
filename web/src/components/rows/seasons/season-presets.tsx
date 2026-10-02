import { ChevronRight, Plus } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSeasonPresets } from "@/lib/queries";
import { ruleLabel, timingLabel } from "@/lib/seasons";
import type { SeasonPreset } from "@/lib/types";

import { SeasonFilmCount } from "./season-film-count";

/**
 * "Add more seasons" (#137 D9): the ready-made seasons not added yet, each with its date and how many
 * films it finds here, and Create your own. Add never saves anything: it opens the editor filled in,
 * so the owner sees the films before saving.
 *
 * Folded away unless `defaultOpen`; nothing is asked of the server until it is opened, since every
 * card counts its films.
 */
export function SeasonPresets({
  defaultOpen,
  rowSize,
  perPerson,
  onAdd,
  onCreate,
}: {
  defaultOpen: boolean;
  rowSize: number;
  perPerson: boolean;
  onAdd: (preset: SeasonPreset) => void;
  onCreate: () => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const presets = useSeasonPresets(open);

  return (
    <details
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      className="group rounded-md border"
    >
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-2 rounded-md px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight
          aria-hidden="true"
          className="h-4 w-4 shrink-0 transition-transform group-open:rotate-90 motion-reduce:transition-none"
        />
        <span className="font-medium">Add more seasons</span>
        <span className="text-muted-foreground">Ready-made holidays, or one you make yourself</span>
      </summary>
      {open && (
        <div className="space-y-3 border-t p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">
              Add opens the season filled in, so you can check its films before saving it.
            </p>
            <Button type="button" size="sm" onClick={onCreate}>
              <Plus aria-hidden="true" />
              Create your own
            </Button>
          </div>

          {presets.isPending ? (
            <div aria-hidden="true" className="grid grid-cols-[repeat(auto-fill,minmax(min(14rem,100%),1fr))] gap-2">
              <Skeleton className="h-28" />
              <Skeleton className="h-28" />
              <Skeleton className="h-28" />
            </div>
          ) : presets.isError ? (
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/40 p-3 text-sm">
              <p>Couldn’t load the ready-made seasons. Check Shortlist is running, then try again.</p>
              <Button type="button" variant="outline" size="sm" onClick={() => void presets.refetch()}>
                Retry
              </Button>
            </div>
          ) : presets.data.length === 0 ? (
            <p className="text-sm text-muted-foreground">You've added every ready-made season.</p>
          ) : (
            // Columns by the room this field has, not the window: the row editor's settings column is
            // ~370px wide on a 1024px screen, beside the summary.
            <ul aria-label="Ready-made seasons" className="grid grid-cols-[repeat(auto-fill,minmax(min(14rem,100%),1fr))] gap-2">
              {presets.data.map((preset) => (
                <PresetCard
                  key={preset.key}
                  preset={preset}
                  rowSize={rowSize}
                  perPerson={perPerson}
                  onAdd={() => onAdd(preset)}
                />
              ))}
            </ul>
          )}

          <p className="text-xs text-muted-foreground">
            Seasons you add or create are kept for the whole server, so any Seasonal row can tick them too.
          </p>
        </div>
      )}
    </details>
  );
}

function PresetCard({
  preset,
  rowSize,
  perPerson,
  onAdd,
}: {
  preset: SeasonPreset;
  rowSize: number;
  perPerson: boolean;
  onAdd: () => void;
}) {
  return (
    <li className="flex flex-col gap-2 rounded-md border bg-card p-3">
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="text-xl leading-none">
          {preset.emoji}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{preset.label}</p>
          <p className="text-xs text-muted-foreground">
            {ruleLabel(preset.rule)} · {timingLabel(preset.lead_days, preset.after_days)}
          </p>
        </div>
        <Button type="button" size="sm" variant="outline" aria-label={`Add ${preset.label}`} onClick={onAdd}>
          Add
        </Button>
      </div>
      <SeasonFilmCount
        source={preset}
        rowSize={rowSize}
        perPerson={perPerson}
        noun="films in your libraries"
        chipWhenOk
      />
      {preset.note && <p className="text-xs text-muted-foreground">{preset.note}</p>}
    </li>
  );
}
