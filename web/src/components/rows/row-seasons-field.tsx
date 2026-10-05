import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { SeasonEditorDialog, type SeasonEditorTarget } from "@/components/rows/seasons/season-editor-dialog";
import { SeasonListItem } from "@/components/rows/seasons/season-list-item";
import { SeasonPresets } from "@/components/rows/seasons/season-presets";
import { SeasonYearStrip } from "@/components/rows/seasons/season-year-strip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { queryKeys, useCreateSeason, useSeasons } from "@/lib/queries";
import { usesSeason } from "@/lib/placeholders";
import { MAX_AFTER_DAYS, MAX_LEAD_DAYS, clampDays, draftFrom, seasonBody } from "@/lib/season-draft";
import { titleNoun, type SeasonRow } from "@/lib/season-verdict";
import { isNightly, seasonStatusLine } from "@/lib/seasons";
import type { Season, SeasonStatus } from "@/lib/types";

type SeasonsValue = {
  seasons: string[];
  season_lead_days: number;
  season_after_days: number;
};

/**
 * Which seasons a Seasonal row follows (discussion #124), and the owner's own seasons (#137).
 *
 * The row holds only the season it is in — Halloween films and horror in October, Christmas films in
 * December — and is hidden between seasons, keeping its collection so it comes straight back. Where it
 * is TODAY comes from the server (`status`), on the clock Plex follows.
 *
 * Every season on the server is listed to tick. Ready-made seasons can be added with their defaults
 * or customised in the editor; either saves for the whole server and ticks it here. The row's
 * own timing applies to the built-ins only: a season of the owner's carries its own (#137 D8).
 *
 * Only rendered for a Seasonal row: the editor's kind picker is what makes a row seasonal or not
 * (`row-kinds.ts`), so this has no on/off switch of its own.
 *
 * Controlled; `onChange` receives only the fields that changed.
 */
export function RowSeasonsField({
  value,
  onChange,
  schedule,
  name,
  status,
  row,
  savedRow,
}: {
  value: SeasonsValue;
  onChange: (patch: Partial<SeasonsValue>) => void;
  /** The row's run schedule, to warn when it would not change nightly or switch seasons on time. */
  schedule: string;
  /** The row's name template, to suggest `{season}` when it does not follow the season. */
  name: string;
  /** Where the SAVED row is in its calendar; null for a new row or one not yet seasonal. */
  status: SeasonStatus | null;
  /** The row's size, mode, media and libraries: what a season's count is made for and judged by. */
  row: SeasonRow;
  /** The row as saved: its id, so the editor names only OTHER rows that use a season, and its seasons,
   *  so a delete the server would refuse for this row is explained first. Null for a new row. */
  savedRow: { id: number; seasons: readonly string[] } | null;
}) {
  const catalogue = useSeasons();
  const create = useCreateSeason();
  const queryClient = useQueryClient();
  const [editor, setEditor] = useState<SeasonEditorTarget | null>(null);
  const [keptLast, setKeptLast] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const addedDirectly = useRef<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  // What had focus when the editor opened, and where focus goes when it closes: a saved season's
  // checkbox (slug), the Seasons heading after a delete (""), or back to the opener (null).
  const opener = useRef<HTMLElement | null>(null);
  const focusAfterClose = useRef<string | null>(null);

  const seasons = catalogue.data ?? [];
  const ticked = seasons.filter((season) => value.seasons.includes(season.slug));
  const builtinTicked = catalogue.isSuccess ? ticked.some((season) => season.builtin) : catalogue.isError;
  // Seasons the form ticks that the server no longer has — deleted in another tab, say. They have no
  // checkbox, and saving the row with them would be refused.
  const gone = catalogue.isSuccess ? value.seasons.filter((slug) => !seasons.some((s) => s.slug === slug)) : [];
  const allGone = gone.length > 0 && gone.length === value.seasons.length;

  useEffect(() => {
    if (!catalogue.isSuccess) return;
    const kept = value.seasons.filter((slug) => catalogue.data.some((s) => s.slug === slug));
    // Never down to none: that would make it a row that follows nothing. `allGone` says what to do.
    if (kept.length > 0 && kept.length < value.seasons.length) onChange({ seasons: kept });
  }, [catalogue.isSuccess, catalogue.data, value.seasons, onChange]);

  useEffect(() => {
    if (!addedDirectly.current) return;
    const checkbox = [...(root.current?.querySelectorAll<HTMLElement>("[data-season]") ?? [])]
      .find((item) => item.dataset.season === addedDirectly.current)
      ?.querySelector<HTMLInputElement>("input[type=checkbox]");
    if (checkbox) {
      checkbox.focus();
      addedDirectly.current = null;
    }
  }, [saved, catalogue.data]);

  /** The row's seasons in calendar order — the server's catalogue order — and only ones it has. */
  const ordered = (chosen: readonly string[], catalogueNow: readonly Season[] = seasons) =>
    catalogueNow.map((season) => season.slug).filter((slug) => chosen.includes(slug));
  const setSeasons = (chosen: string[]) => onChange({ seasons: ordered(chosen) });

  const openEditor = (target: SeasonEditorTarget) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    focusAfterClose.current = null;
    setEditor(target);
  };

  /** Radix gives focus back to a trigger, and this dialog has none; and after a save from a ready-made
   *  season, or a delete, the button that opened it is gone. */
  const placeFocusAfterClose = (event: Event) => {
    event.preventDefault();
    const slug = focusAfterClose.current;
    focusAfterClose.current = null;
    const box = slug
      ? [...(root.current?.querySelectorAll<HTMLElement>("[data-season]") ?? [])]
          .find((item) => item.dataset.season === slug)
          ?.querySelector<HTMLInputElement>("input[type=checkbox]")
      : null;
    const back = slug === null && opener.current?.isConnected ? opener.current : null;
    (box ?? back ?? heading.current)?.focus();
  };

  const toggleSeason = (slug: string) => {
    const chosen = value.seasons.includes(slug)
      ? value.seasons.filter((s) => s !== slug)
      : [...value.seasons, slug];
    // The last one cannot be unticked: no seasons means "not seasonal", which is the kind picker's call.
    setKeptLast(chosen.length === 0);
    if (chosen.length === 0) return;
    setSaved(null);
    setSeasons(chosen);
  };

  const onSaved = (slug: string) => {
    // The save awaited the catalogue's refetch, so the cache already has the new season in its place.
    const catalogueNow = queryClient.getQueryData<Season[]>(queryKeys.seasons) ?? seasons;
    const season = catalogueNow.find((s) => s.slug === slug);
    const label = season ? `“${season.emoji} ${season.name}”` : "The season";
    if (value.seasons.includes(slug)) {
      setSaved(`Saved ${label}.`);
    } else {
      const next = ordered([...value.seasons, slug], catalogueNow);
      onChange({ seasons: next.includes(slug) ? next : [...next, slug] });
      setSaved(`Saved ${label} and ticked it here. Save this row to keep it ticked.`);
    }
    setKeptLast(false);
    focusAfterClose.current = slug;
    setEditor(null);
  };

  const onDeleted = (slug: string) => {
    // The server took it out of every saved row; the form here follows.
    const ticked = value.seasons.includes(slug);
    if (ticked) onChange({ seasons: value.seasons.filter((s) => s !== slug) });
    const season = editor?.kind === "edit" ? editor.season : null;
    const label = season ? `“${season.emoji} ${season.name}”` : "the season";
    setSaved(`Deleted ${label}${ticked ? " and unticked it here" : ""}.`);
    focusAfterClose.current = "";
    setEditor(null);
  };

  const statusLine = seasonStatusLine(status);

  return (
    <div ref={root} className="space-y-4">
      <div className="space-y-1">
        <h3
          ref={heading}
          tabIndex={-1}
          className="rounded-sm text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Seasons
        </h3>
        <p className="text-sm text-muted-foreground">
          Ticked seasons show in this row on their dates. Only {titleNoun(row.media, 2)} in your libraries are used.
        </p>
      </div>

      {catalogue.isPending ? (
        <div className="space-y-2" aria-hidden="true">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : catalogue.isError ? (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/40 p-3 text-sm">
          <p>Couldn’t load the seasons. Check Shortlist is running, then try again.</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void catalogue.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <fieldset disabled={create.isPending}>
          <legend className="sr-only">Seasons</legend>
          <ul className="space-y-2">
            {seasons.map((season) => (
              <SeasonListItem
                key={season.slug}
                season={season}
                checked={value.seasons.includes(season.slug)}
                onToggle={() => toggleSeason(season.slug)}
                rowLeadDays={value.season_lead_days}
                rowAfterDays={value.season_after_days}
                row={row}
                onEdit={() => openEditor({ kind: "edit", season })}
              />
            ))}
          </ul>
        </fieldset>
      )}
      {allGone && (
        <p role="status" className="rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
          The season this row followed has been deleted. Tick another season for it before saving.
        </p>
      )}
      {keptLast && (
        <p role="status" className="text-sm text-muted-foreground">
          This row needs at least one season, so the last one stays ticked. Tick another season first.
        </p>
      )}
      {saved && (
        <p role="status" className="text-sm">
          {saved}
        </p>
      )}

      {catalogue.isSuccess && (
        <SeasonPresets
          defaultOpen={ticked.every((season) => season.builtin)}
          row={row}
          onAdd={async (preset) => {
            const season = await create.mutateAsync(seasonBody(draftFrom(preset), preset.key));
            addedDirectly.current = season.slug;
            onSaved(season.slug);
          }}
          onCustomise={(preset) => openEditor({ kind: "preset", preset })}
          onCreate={() => openEditor({ kind: "create" })}
        />
      )}

      <div className="space-y-3">
        <h4 className="text-sm font-medium">When this row shows</h4>
        {builtinTicked && (
          <p className="flex flex-wrap items-center gap-x-2 gap-y-2 text-sm">
            <label className="inline-flex flex-wrap items-center gap-2">
              Built-in seasons show from{" "}
              <Input
                type="number"
                min={0}
                max={MAX_LEAD_DAYS}
                value={value.season_lead_days}
                onChange={(e) => onChange({ season_lead_days: clampDays(e.target.value, MAX_LEAD_DAYS) })}
                className="w-20"
              />{" "}
              days before
            </label>{" "}
            <label className="inline-flex flex-wrap items-center gap-2">
              and stay{" "}
              <Input
                type="number"
                min={0}
                max={MAX_AFTER_DAYS}
                value={value.season_after_days}
                onChange={(e) => onChange({ season_after_days: clampDays(e.target.value, MAX_AFTER_DAYS) })}
                className="w-20"
              />{" "}
              days after.
            </label>
          </p>
        )}
        {ticked.length > 0 && (
          <SeasonYearStrip seasons={ticked} leadDays={value.season_lead_days} afterDays={value.season_after_days} />
        )}
      </div>

      {statusLine && <p className="text-sm font-medium">{statusLine}</p>}

      {!usesSeason(name) && (
        <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
          Put <code>{"{season_emoji} {season}"}</code> in the name and it follows the season too —
          “🎃 Halloween picks” in October, “🎄 Christmas picks” in December.
        </p>
      )}

      {!isNightly(schedule) && (
        <p role="status" className="rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
          This row doesn’t run every night, so it won’t change daily, and a new season can appear a
          few days late. Set its schedule to <strong>Nightly</strong> under “When it updates”.
        </p>
      )}

      {editor && (
        <SeasonEditorDialog
          target={editor}
          row={row}
          savedRow={savedRow}
          tickedHere={value.seasons}
          onClose={() => setEditor(null)}
          onCloseAutoFocus={placeFocusAfterClose}
          onSaved={onSaved}
          onDeleted={onDeleted}
        />
      )}
    </div>
  );
}
