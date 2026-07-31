import { LayoutGrid, Pencil } from "lucide-react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { describeCron } from "@/lib/cron";
import { timeUntil } from "@/lib/format";
import { useSchedule } from "@/lib/queries";

/**
 * The rows that build on a timer, listed alongside the jobs that do.
 *
 * Row schedules were the only thing the separate Timeline page showed that the Jobs list didn't —
 * every job already carries its own next-run — so keeping them apart meant two pages each holding
 * half the answer to "what runs overnight". Rows are grouped by shared cron exactly as the scheduler
 * groups them: one trigger builds all of them, so listing them per row would imply N timers where
 * there is one.
 *
 * Read-only on purpose. A row's schedule is edited in the row editor, so the cron has exactly one
 * owner and can never be validated two different ways.
 */
export function RowSchedules() {
  const query = useSchedule();
  const groups = (query.data?.rows ?? []).filter((entry) => entry.cron);

  if (groups.length === 0) return null;

  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2">
        <h2 className="text-sm font-medium">Rows</h2>
        <p className="text-xs text-muted-foreground">
          built on their own schedule
        </p>
      </div>

      <div className="overflow-hidden rounded-md border">
        {groups.map((entry, index) => (
          <div
            key={entry.cron}
            className={`flex items-center gap-3 px-3 py-2.5 ${index > 0 ? "border-t" : ""}`}
          >
            <LayoutGrid
              aria-hidden="true"
              className="size-4 shrink-0 text-muted-foreground"
            />

            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">
                {(entry.rows ?? []).map((row) => row.name).join(", ")}
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {describeCron(entry.cron) || entry.cron}
              </p>
            </div>

            {entry.next_run && (
              <span className="hidden w-32 shrink-0 text-xs text-muted-foreground sm:block">
                {timeUntil(entry.next_run)}
              </span>
            )}

            {/* Edited where the row is edited — one owner per setting. */}
            <Button asChild size="sm" variant="outline" className="shrink-0">
              <Link to="/rows">
                <Pencil aria-hidden="true" />
                Edit
              </Link>
            </Button>
          </div>
        ))}
      </div>
    </section>
  );
}
