import { useId } from "react";

import { badgeVariants } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { longDate, seasonTiming, seasonWindowLabel, timingLabel } from "@/lib/seasons";
import type { Season } from "@/lib/types";
import { cn } from "@/lib/utils";

import { SeasonFilmCount } from "./season-film-count";

/**
 * One season in the row editor's list (#137): tick it to show it in this row.
 *
 * A season of the owner's ("Yours") shows its film count against this row and an Edit button; a
 * built-in shows neither — its films are known to be plenty, and it can't be edited.
 */
export function SeasonListItem({
  season,
  checked,
  onToggle,
  rowLeadDays,
  rowAfterDays,
  rowSize,
  perPerson,
  onEdit,
}: {
  season: Season;
  checked: boolean;
  onToggle: () => void;
  /** The row's timing, which a built-in follows. */
  rowLeadDays: number;
  rowAfterDays: number;
  rowSize: number;
  perPerson: boolean;
  onEdit: () => void;
}) {
  const detailsId = useId();
  const { lead, after } = seasonTiming(season, rowLeadDays, rowAfterDays);
  const next = season.next_dates[0];

  return (
    <li data-season={season.slug} className="rounded-md border px-3 py-2 transition-colors hover:bg-muted/40">
      <div className="flex items-start gap-3">
        <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3 text-sm">
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggle}
            aria-label={`Show ${season.name} in this row`}
            aria-describedby={detailsId}
            className="mt-1 h-4 w-4 shrink-0 accent-primary"
          />
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1 font-medium">
              <span>
                <span aria-hidden="true">{season.emoji}</span> {season.name}
              </span>
              <span className={cn(badgeVariants({ variant: season.builtin ? "secondary" : "outline" }), !season.builtin && "text-primary")}>
                {season.builtin ? "Built in" : "Yours"}
              </span>
            </span>
            <span id={detailsId} className="block text-muted-foreground">
              <span className="block">
                {season.rule_label}
                {next && ` · next ${longDate(next)}`}
              </span>{" "}
              <span className="block">
                Shows {seasonWindowLabel(season, lead, after)}, {timingLabel(lead, after)}
              </span>
              {season.builtin && season.description && (
                <>
                  {" "}
                  <span className="block">{season.description}</span>
                </>
              )}
            </span>
          </span>
        </label>
        {!season.builtin && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="shrink-0"
            aria-label={`Edit ${season.name}`}
            onClick={onEdit}
          >
            Edit
          </Button>
        )}
      </div>
      {!season.builtin && (
        <div className="mt-1 pl-7">
          <SeasonFilmCount source={season} rowSize={rowSize} perPerson={perPerson} />
        </div>
      )}
    </li>
  );
}
