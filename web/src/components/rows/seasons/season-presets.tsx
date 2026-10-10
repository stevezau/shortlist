import { ChevronRight, Plus, Search } from "lucide-react";
import { useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { useSeasonPresets, useSeasonPreview } from "@/lib/queries";
import { previewInput } from "@/lib/season-draft";
import { selectedClass, unselectedClass } from "@/lib/selected";
import { ruleLabel, timingLabel } from "@/lib/seasons";
import type { SeasonRow } from "@/lib/season-verdict";
import type { SeasonPreset } from "@/lib/types";

import { SeasonFilmCount } from "./season-film-count";

const CATEGORIES = [
  { key: "all", label: "All" },
  { key: "holidays", label: "Holidays" },
  { key: "film_days", label: "Film days" },
  { key: "spotlights", label: "Spotlights" },
] as const;
type Category = (typeof CATEGORIES)[number]["key"];
const INITIAL_VISIBLE = 6;

/** Preview only the visible presets: a library count can involve reading every library. */
export function SeasonPresets({
  defaultOpen,
  row,
  onAdd,
  onCustomise,
  onCreate,
}: {
  defaultOpen: boolean;
  row: SeasonRow;
  onAdd: (preset: SeasonPreset) => Promise<void>;
  onCustomise: (preset: SeasonPreset) => void;
  onCreate: () => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<Category>("all");
  const [showAll, setShowAll] = useState(false);
  const [adding, setAdding] = useState<string | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  // Guard before the next render as well as disabling the buttons, so a double click saves once.
  const pending = useRef(false);
  const searchId = useId();
  const presets = useSeasonPresets(open);
  const query = search.trim().toLocaleLowerCase();
  const filtered = (presets.data ?? []).filter((preset) =>
    (category === "all" || (preset.category ?? "holidays") === category) &&
    (!query || [preset.label, preset.name, preset.description, preset.note, ruleLabel(preset.rule)]
      .filter(Boolean).join(" ").toLocaleLowerCase().includes(query)),
  );
  const visible = showAll ? filtered : filtered.slice(0, INITIAL_VISIBLE);

  const add = async (preset: SeasonPreset) => {
    if (pending.current) return;
    pending.current = true;
    setAdding(preset.key);
    setError(null);
    try {
      await onAdd(preset);
    } catch (cause) {
      setError({
        key: preset.key,
        message: apiErrorMessage(cause, "Couldn’t add this season. Check Shortlist is running, then try again."),
      });
    } finally {
      pending.current = false;
      setAdding(null);
    }
  };

  return (
    <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="group rounded-md border">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-2 rounded-md px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden="true" className="h-4 w-4 shrink-0 transition-transform group-open:rotate-90 motion-reduce:transition-none" />
        <span className="font-medium">Add more seasons</span>
        <span className="text-muted-foreground">Holidays, film days and spotlights</span>
      </summary>
      {open && (
        <div className="space-y-4 border-t p-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <p className="max-w-prose text-sm text-muted-foreground">
              Ready-made dates and film selections. Add uses the defaults; Customise lets you change them.
            </p>
            <Button type="button" size="sm" variant="outline" disabled={adding !== null} onClick={onCreate}>
              <Plus aria-hidden="true" />
              Create your own
            </Button>
          </div>

          {presets.isPending ? (
            <div aria-hidden="true" className="space-y-2">
              <Skeleton className="h-28 w-full" />
              <Skeleton className="h-28 w-full" />
              <Skeleton className="h-28 w-full" />
            </div>
          ) : presets.isError ? (
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <p>Couldn’t load the ready-made seasons. Check Shortlist is running, then try again.</p>
              <Button type="button" variant="outline" size="sm" onClick={() => void presets.refetch()}>Retry</Button>
            </div>
          ) : presets.data.length === 0 ? (
            <p className="text-sm text-muted-foreground">You've added every ready-made season.</p>
          ) : (
            <>
              <div className="space-y-3">
                <div className="space-y-1">
                  <Label htmlFor={searchId}>Find a season</Label>
                  <div className="relative">
                    <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                    <Input
                      id={searchId}
                      type="search"
                      value={search}
                      placeholder="Search by occasion or theme"
                      className="pl-9"
                      onChange={(event) => { setSearch(event.target.value); setShowAll(false); }}
                    />
                  </div>
                </div>
                <div role="group" aria-label="Season categories" className="flex flex-wrap gap-2">
                  {CATEGORIES.map(({ key, label }) => (
                    <Button
                      key={key}
                      type="button"
                      size="sm"
                      variant="outline"
                      aria-pressed={category === key}
                      className={category === key ? selectedClass : unselectedClass}
                      onClick={() => { setCategory(key); setShowAll(false); }}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground" role="status">
                  {`${filtered.length} ${filtered.length === 1 ? "season" : "seasons"} to explore`}
                </p>
              </div>

              {filtered.length === 0 ? (
                <div className="space-y-2 py-2 text-sm">
                  <p>No seasons match your search and category.</p>
                  <Button type="button" size="sm" variant="outline" onClick={() => { setSearch(""); setCategory("all"); }}>
                    Clear filters
                  </Button>
                </div>
              ) : (
                <ul aria-label="Ready-made seasons" className="divide-y border-y">
                  {visible.map((preset) => (
                    <PresetItem
                      key={preset.key}
                      preset={preset}
                      row={row}
                      busy={adding !== null}
                      adding={adding === preset.key}
                      error={error?.key === preset.key ? error.message : null}
                      onAdd={() => void add(preset)}
                      onCustomise={() => onCustomise(preset)}
                    />
                  ))}
                </ul>
              )}
              {!showAll && filtered.length > INITIAL_VISIBLE && (
                <Button type="button" size="sm" variant="outline" onClick={() => setShowAll(true)}>
                  {`Show all ${filtered.length} seasons`}
                </Button>
              )}
            </>
          )}

          <p className="text-xs text-muted-foreground">
            Matches are counted before the row’s filters and watch requirements. Starter selections contain films;
            Customise lets you add more picks or a Plex collection. Seasons are saved for the whole server.
          </p>
        </div>
      )}
    </details>
  );
}

function PresetItem({ preset, row, busy, adding, error, onAdd, onCustomise }: {
  preset: SeasonPreset;
  row: SeasonRow;
  busy: boolean;
  adding: boolean;
  error: string | null;
  onAdd: () => void;
  onCustomise: () => void;
}) {
  const preview = useSeasonPreview(previewInput(preset, row));
  const sample = preview.data?.sample.slice(0, 3) ?? [];
  const filmsOnly = (preset.picks ?? []).length > 0 &&
    (preset.picks ?? []).every((pick) => pick.media_type === "movie") &&
    (preset.tags ?? []).length === 0 && (preset.collections ?? []).length === 0 && preset.genre == null;
  const needsShows = filmsOnly && row.media === "show";

  return (
    <li className="space-y-2 py-4 first:pt-3 last:pb-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 basis-44 space-y-1">
          <p className="text-sm font-semibold"><span aria-hidden="true">{preset.emoji}</span> {preset.label}</p>
          <p className="text-xs text-muted-foreground">
            {ruleLabel(preset.rule)}
            {preset.rule.kind !== "month" && ` · ${timingLabel(preset.lead_days, preset.after_days)}`}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button type="button" size="sm" variant="ghost" disabled={busy} aria-label={`Customise ${preset.label}`} onClick={onCustomise}>
            Customise
          </Button>
          <Button type="button" size="sm" variant="outline" disabled={busy || needsShows} loading={adding} aria-label={`Add ${preset.label}`} onClick={onAdd}>
            Add
          </Button>
        </div>
      </div>
      {preset.description && <p className="max-w-prose text-sm">{preset.description}</p>}
      <SeasonFilmCount source={preset} row={row} inLibraries />
      {sample.length > 0 && <p className="text-xs text-muted-foreground">In your library: {sample.join(" · ")}</p>}
      {needsShows && <p className="text-sm text-muted-foreground">Films only. Customise this selection to add shows for your TV row.</p>}
      {preset.note && <p className="max-w-prose text-xs text-muted-foreground">{preset.note}</p>}
      {error && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <p role="alert" className="text-destructive-text">{error} Customise to change the name or selection.</p>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={onAdd}>Retry</Button>
        </div>
      )}
    </li>
  );
}
