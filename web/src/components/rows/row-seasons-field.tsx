import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSeasons } from "@/lib/queries";
import { usesSeason } from "@/lib/placeholders";
import { isNightly, seasonStatusLine, seasonWindowLabel } from "@/lib/seasons";
import type { SeasonStatus } from "@/lib/types";

/** The API's bounds (`seasons.MAX_LEAD_DAYS` / `MAX_AFTER_DAYS`), so the field never offers a value
 *  the save would refuse. */
const MAX_LEAD_DAYS = 90;
const MAX_AFTER_DAYS = 30;

type SeasonsValue = {
  seasons: string[];
  season_lead_days: number;
  season_after_days: number;
};

function clampDays(raw: string, max: number): number {
  return Math.min(max, Math.max(0, Math.round(Number(raw) || 0)));
}

/**
 * Which seasons a Seasonal row follows, and how early and late it shows each one (discussion #124).
 *
 * The row holds only the season it is in — Halloween films and horror in October, Christmas films in
 * December — and is hidden between seasons, keeping its collection so it comes straight back. Where it
 * is TODAY comes from the server (`status`), on the clock Plex follows.
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
}: {
  value: SeasonsValue;
  onChange: (patch: Partial<SeasonsValue>) => void;
  /** The row's run schedule, to warn when it would not change nightly or switch seasons on time. */
  schedule: string;
  /** The row's name template, to suggest `{season}` when it does not follow the season. */
  name: string;
  /** Where the SAVED row is in its calendar; null for a new row or one not yet seasonal. */
  status: SeasonStatus | null;
}) {
  const catalogue = useSeasons();

  const toggleSeason = (slug: string) => {
    const chosen = value.seasons.includes(slug)
      ? value.seasons.filter((s) => s !== slug)
      : [...value.seasons, slug];
    // The last one cannot be unticked: no seasons means "not seasonal", which is the kind picker's call.
    if (chosen.length === 0) return;
    const order = (catalogue.data ?? []).map((s) => s.slug);
    onChange({ seasons: order.filter((s) => chosen.includes(s)) });
  };

  const statusLine = seasonStatusLine(status);

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <p className="text-sm font-medium">Which seasons</p>
        <p className="text-sm text-muted-foreground">
          This row holds only the season it’s in, and is hidden between seasons. It keeps its
          collection, so it comes straight back next time.
        </p>
      </div>

      {catalogue.isLoading ? (
        <div className="space-y-2" aria-hidden="true">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : catalogue.isError ? (
        <div className="flex items-center gap-3 rounded-md border border-destructive/40 p-3 text-sm">
          <p>Couldn’t load the seasons. Check Shortlist is running, then try again.</p>
          <Button type="button" variant="outline" size="sm" onClick={() => catalogue.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <fieldset className="space-y-2">
          <legend className="sr-only">Which seasons</legend>
          {(catalogue.data ?? []).map((season) => {
            const checked = value.seasons.includes(season.slug);
            return (
              <label
                key={season.slug}
                className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2 text-sm transition-colors hover:bg-muted/50"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggleSeason(season.slug)}
                  className="mt-0.5 h-4 w-4 accent-primary"
                />
                <span className="min-w-0 flex-1">
                  <span className="font-medium">
                    <span aria-hidden="true">{season.emoji}</span> {season.name}
                  </span>
                  <span className="block text-muted-foreground">
                    {season.description} · shows{" "}
                    {seasonWindowLabel(season, value.season_lead_days, value.season_after_days)}
                  </span>
                </span>
              </label>
            );
          })}
        </fieldset>
      )}

      <div className="flex flex-wrap gap-6">
        <div className="space-y-2">
          <Label htmlFor="row-season-lead">Start showing (days before)</Label>
          <Input
            id="row-season-lead"
            type="number"
            min={0}
            max={MAX_LEAD_DAYS}
            value={value.season_lead_days}
            onChange={(e) => onChange({ season_lead_days: clampDays(e.target.value, MAX_LEAD_DAYS) })}
            className="w-24"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="row-season-after">Keep it up (days after)</Label>
          <Input
            id="row-season-after"
            type="number"
            min={0}
            max={MAX_AFTER_DAYS}
            value={value.season_after_days}
            onChange={(e) => onChange({ season_after_days: clampDays(e.target.value, MAX_AFTER_DAYS) })}
            className="w-24"
          />
        </div>
      </div>
      <p className="text-sm text-muted-foreground">
        When two seasons overlap, the one coming up next wins. Seasons change at midnight on the
        server.
      </p>

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
    </div>
  );
}
