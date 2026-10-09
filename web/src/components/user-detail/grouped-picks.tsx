import { useState } from "react";

import { TitlePoster } from "@/components/title-poster";
import { provenanceLabel } from "@/lib/pick-provenance";
import type { Pick } from "@/lib/types";

type PickGroup = { heading: string; picks: Pick[] };

/** The reason a pick is here, as a group heading. The API already says it: the title it was seeded from. */
function groupHeading(pick: Pick): string {
  if (pick.sources?.includes("history")) return "Worth another watch";
  if (pick.sources?.length === 1 && pick.sources[0] === "requests") return "They asked for these";
  if (pick.seed_title) return `Because you watched ${pick.seed_title}`;
  return "Also picked";
}

/** Groups in order of their best-ranked pick, so the strongest reason leads. */
function groupPicks(picks: Pick[]): PickGroup[] {
  const groups = new Map<string, PickGroup>();
  for (const pick of picks) {
    const heading = groupHeading(pick);
    const group = groups.get(heading) ?? { heading, picks: [] };
    group.picks.push(pick);
    groups.set(heading, group);
  }
  return [...groups.values()];
}

/**
 * A person's picks as posters, grouped under why they were chosen, instead of repeating the same
 * reason on every pick. Ranks stay on the poster corner, in overall order.
 *
 * `collapseAfter` caps the posters shown at first; a collapsed pick is not in the DOM, so its artwork
 * is not fetched until the list is expanded.
 */
export function GroupedPicks({ picks, collapseAfter }: { picks: Pick[]; collapseAfter: number }) {
  const [expanded, setExpanded] = useState(false);
  const ordered = [...picks].sort((a, b) => a.rank - b.rank);
  const collapses = ordered.length > collapseAfter;
  const shown = collapses && !expanded ? ordered.slice(0, collapseAfter) : ordered;
  const groups = groupPicks(shown);
  // One group needs no heading beyond its own; "Also picked" alone says nothing.
  const lone = groups.length === 1 && groups[0]?.heading === "Also picked";

  return (
    <div className="divide-y">
      {groups.map((group) => (
        <div key={group.heading} className="py-4 first:pt-0">
          {!lone && <h3 className="mb-3 text-sm font-semibold">{group.heading}</h3>}
          <ul className="flex flex-wrap gap-4">
            {group.picks.map((pick) => (
              <li key={pick.rank} className="w-[104px] shrink-0 sm:w-[120px]">
                <div className="relative">
                  <TitlePoster
                    ratingKey={pick.rating_key}
                    className="h-auto w-full aspect-[2/3] sm:h-auto sm:w-full"
                  />
                  <span className="absolute left-1 top-1 rounded bg-background/80 px-1.5 text-xs font-medium tabular-nums text-muted-foreground">
                    {pick.rank}
                  </span>
                </div>
                <div className="mt-1.5 truncate text-sm font-medium" title={pick.title}>
                  {pick.title}
                </div>
                <div className="line-clamp-2 text-xs text-muted-foreground" title={pick.reason}>
                  {provenanceLabel(pick)}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {collapses && (
        <div className="pt-3">
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
