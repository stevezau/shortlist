import { CalendarClock, Clock } from "lucide-react";
import { Link } from "react-router";

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
 * The SCHEDULE leads, because the schedule is what a group is. This block used to lead with the row
 * names, comma-joined into one truncating line — three rows on the same nightly cron rendered as
 * "✨ Picked for You, 🎯 Because you watched {top_seed}, 👥 Popular {library_name} on SFLIX" with
 * the cron as its subtitle. That put the group's identity in the small print and made the rows
 * themselves unreadable and unclickable. Each row is now its own link into its own editor.
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
        {groups.map((entry, index) => {
          const rows = entry.rows ?? [];
          return (
            <div
              key={entry.cron}
              className={`space-y-2 px-3 py-2.5 ${index > 0 ? "border-t" : ""}`}
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <CalendarClock
                  aria-hidden="true"
                  className="size-4 shrink-0 text-muted-foreground"
                />
                <p className="text-sm font-medium">
                  {describeCron(entry.cron) || entry.cron}
                </p>
                {entry.next_run && (
                  <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Clock className="size-3 shrink-0" aria-hidden="true" />
                    {timeUntil(entry.next_run)}
                  </span>
                )}
                {/* The count is what makes the chips below read as a list rather than as tags on
                    the schedule — and it is the number that matters when one cron drives twelve. */}
                <span className="text-xs text-muted-foreground/80">
                  · builds {rows.length} {rows.length === 1 ? "row" : "rows"}
                </span>
              </div>

              {/* One link per row, to that row's own editor. The old single "Edit" button pointed
                  at /rows — the list — because with N names on one line there was no single row it
                  could mean. It read as "edit this schedule" and could not be. */}
              <div className="flex flex-wrap gap-1.5">
                {rows.map((row) => (
                  <Link
                    key={row.id}
                    to={`/rows/${row.id}`}
                    title={`Edit ${row.name}`}
                    className="inline-flex max-w-full items-center rounded-full border bg-muted/40 px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {/* `truncate` needs `min-w-0` on the flex child to shrink; a template row name
                        ("👥 Popular {library_name} on SFLIX") is long enough to matter on a phone. */}
                    <span className="min-w-0 truncate">{row.name}</span>
                  </Link>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
