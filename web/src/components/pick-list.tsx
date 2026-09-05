import { useState } from "react";

import { TitlePoster } from "@/components/title-poster";
import { provenanceLabel } from "@/lib/pick-provenance";
import type { Pick } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The ranked "#1 Title — why we picked it · inspired by Seed" list, shared by the per-user row card
 * and the run-detail results. Sorts by rank so callers can pass picks in any order.
 *
 * `collapseAfter` caps how many rows show at first, with a "+N more" toggle — a person's row can hold
 * 40 titles, and a page of several rows is a wall without it. Omit it to always show every pick.
 * It also bounds the poster requests: a collapsed pick is not in the DOM at all, so its artwork is
 * never fetched until the owner expands the list.
 */
export function PickList({
  picks,
  className,
  collapseAfter,
}: {
  picks: Pick[];
  className?: string;
  collapseAfter?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const ordered = [...picks].sort((a, b) => a.rank - b.rank);
  const collapses =
    collapseAfter !== undefined && ordered.length > collapseAfter;
  const shown =
    collapses && !expanded ? ordered.slice(0, collapseAfter) : ordered;
  const hidden = ordered.length - shown.length;

  return (
    <div className="space-y-1.5">
      <ol className={cn("space-y-1.5", className)}>
        {shown.map((pick) => (
          // `items-start`, not `items-baseline`: a poster and a text baseline do not align.
          <li key={pick.rank} className="flex items-start gap-3 text-sm">
            {/* Below `sm` the rank rides the poster's corner instead of taking its own 20px column,
                which is what buys the text enough width to read at 320px. */}
            <div className="relative shrink-0">
              <TitlePoster ratingKey={pick.rating_key} />
              <span className="absolute left-0 top-0 rounded-br rounded-tl bg-background/90 px-1 text-xs font-semibold text-primary sm:hidden">
                #{pick.rank}
              </span>
            </div>
            <span className="hidden w-5 shrink-0 pt-0.5 font-semibold text-primary sm:inline">
              #{pick.rank}
            </span>
            <span className="min-w-0">
              <span className="font-medium">{pick.title}</span>
              <span className="text-muted-foreground">
                {" "}
                — {pick.reason}
                {pick.seed_title ? ` · inspired by ${pick.seed_title}` : ""}
              </span>
              {/* Where it came from, on its own line: "why is this here?" was previously
                  unanswerable without reading the logs. */}
              {provenanceLabel(pick) ? (
                <span className="block text-xs text-muted-foreground/80">
                  {provenanceLabel(pick)}
                </span>
              ) : null}
            </span>
          </li>
        ))}
      </ol>
      {collapses && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none"
          aria-expanded={expanded}
        >
          {expanded ? "Show fewer" : `Show all ${ordered.length} (+${hidden})`}
        </button>
      )}
    </div>
  );
}
