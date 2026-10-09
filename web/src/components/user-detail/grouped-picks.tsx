import { useState } from "react";

import { TitlePoster } from "@/components/title-poster";
import { provenanceLabel } from "@/lib/pick-provenance";
import type { Pick } from "@/lib/types";

/** Why a pick is here, as one short line. The API already says it: the title it was seeded from. */
function pickReason(pick: Pick): string | null {
  if (pick.sources?.includes("history")) return "Worth another watch";
  if (pick.sources?.length === 1 && pick.sources[0] === "requests") return "They asked for these";
  if (pick.seed_title) return `Because you watched ${pick.seed_title}`;
  return null;
}

/**
 * A person's picks as one wrapping poster grid in rank order, the reason for each under its own
 * poster. Grouping by reason was tried: with a distinct seed per pick, every group held one or two
 * posters and the tab read as a tall sparse run log. Ranks stay on the poster corner.
 *
 * `collapseAfter` caps the posters shown at first; a collapsed pick is not in the DOM, so its artwork
 * is not fetched until the list is expanded.
 */
export function GroupedPicks({ picks, collapseAfter }: { picks: Pick[]; collapseAfter: number }) {
  const [expanded, setExpanded] = useState(false);
  const ordered = [...picks].sort((a, b) => a.rank - b.rank);
  const collapses = ordered.length > collapseAfter;
  const shown = collapses && !expanded ? ordered.slice(0, collapseAfter) : ordered;

  return (
    <div>
      <ul className="flex flex-wrap gap-4">
        {shown.map((pick) => {
          const reason = pickReason(pick);
          return (
            <li key={pick.rank} className="w-[104px] shrink-0 sm:w-[120px]">
              <div className="relative">
                <TitlePoster ratingKey={pick.rating_key} className="h-auto w-full aspect-[2/3] sm:h-auto sm:w-full" />
                <span className="absolute left-1 top-1 rounded bg-background/80 px-1.5 text-xs font-medium tabular-nums text-muted-foreground">
                  {pick.rank}
                </span>
              </div>
              <div className="mt-1.5 truncate text-sm font-medium" title={pick.title}>
                {pick.title}
              </div>
              {reason && (
                <div className="line-clamp-2 text-xs text-muted-foreground" title={reason}>
                  {reason}
                </div>
              )}
              <div className="line-clamp-2 text-xs text-muted-foreground" title={pick.reason}>
                {provenanceLabel(pick)}
              </div>
            </li>
          );
        })}
      </ul>
      {collapses && (
        <div className="pt-4">
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="text-sm font-medium text-muted-foreground hover:text-foreground focus-visible:underline focus-visible:outline-none"
            aria-expanded={expanded}
          >
            {expanded ? "Show fewer" : `Show all ${ordered.length} titles`}
          </button>
        </div>
      )}
    </div>
  );
}
