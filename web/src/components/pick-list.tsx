import type { Pick } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The ranked "#1 Title — why we picked it · inspired by Seed" list, shared by the per-user row card
 * and the run-detail results. Sorts by rank so callers can pass picks in any order.
 */
export function PickList({
  picks,
  className,
}: {
  picks: Pick[];
  className?: string;
}) {
  const ordered = [...picks].sort((a, b) => a.rank - b.rank);
  return (
    <ol className={cn("space-y-1.5", className)}>
      {ordered.map((pick) => (
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
          </span>
        </li>
      ))}
    </ol>
  );
}
