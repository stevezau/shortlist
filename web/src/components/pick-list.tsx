import { useState } from "react";

import { provenanceLabel } from "@/lib/pick-provenance";
import type { Pick } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The ranked "#1 Title — why we picked it · inspired by Seed" list, shared by the per-user row card
 * and the run-detail results. Sorts by rank so callers can pass picks in any order.
 *
 * `collapseAfter` caps how many rows show at first, with a "+N more" toggle — a person's row can hold
 * 40 titles, and a page of several rows is a wall without it. Omit it to always show every pick.
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
          <li key={pick.rank} className="flex items-baseline gap-3 text-sm">
            <span className="w-5 shrink-0 font-semibold text-primary">
              #{pick.rank}
            </span>
            <span>
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
