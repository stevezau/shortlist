import { CalendarClock, Clock } from "lucide-react";
import { Link } from "react-router";

import { Skeleton } from "@/components/ui/skeleton";
import { describeCron } from "@/lib/cron";
import { timeUntil } from "@/lib/format";
import { useSchedule } from "@/lib/queries";
import { resolveRowName } from "@/lib/run-rows";

/**
 * The rows that build on a timer, listed alongside the jobs that do.
 *
 * Row schedules were the only thing the separate Timeline page showed that the Jobs list didn't —
 * every job already carries its own next-run — so keeping them apart meant two pages each holding
 * half the answer to "what runs overnight". Rows are grouped by shared cron exactly as the scheduler
 * groups them: one trigger builds all of them, so listing them per row would imply N timers where
 * there is one.
 *
 * The SCHEDULE leads, because the schedule is what a group is. Row names comma-joined into one
 * truncating line would put the group's identity in the small print and make the rows themselves
 * unreadable and unclickable, so each row is its own link into its own editor.
 *
 * Read-only on purpose. A row's schedule is edited in the row editor, so the cron has exactly one
 * owner and can never be validated two different ways.
 */
export function RowSchedules() {
  const query = useSchedule();
  const groups = (query.data?.rows ?? []).filter((entry) => entry.cron);

  // Loading, a failed fetch and "genuinely nothing on its own schedule" all produced an empty list
  // and rendered nothing at all — three different situations collapsed into one blank space, so a
  // broken schedule endpoint looked exactly like a server with no per-row schedules. Absent is a
  // legitimate answer here (most installs have none), so it stays silent; the other two do not.
  if (query.isPending) {
    return (
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Rows</h2>
        <Skeleton className="h-16 w-full" />
      </section>
    );
  }
  if (query.isError) {
    return (
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Rows</h2>
        <p className="text-sm text-destructive-text" role="alert">
          Couldn&rsquo;t load row schedules.{" "}
          <button
            type="button"
            className="underline underline-offset-2"
            onClick={() => query.refetch()}
          >
            Try again
          </button>
        </p>
      </section>
    );
  }
  if (groups.length === 0) return null;

  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-2 px-1">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Rows</h2>
        <p className="text-xs text-muted-foreground">
          · built on their own schedule
        </p>
      </div>

      <div className="overflow-hidden rounded-md border">
        {groups.map((entry, index) => {
          const rows = entry.rows ?? [];
          return (
            // The icon sits in the job rows' icon column and the text on their name line, so this
            // list lines up with the two around it: the leading inset is the width of a job row's
            // chevron and its gap, which a schedule (nothing to expand) has no use for.
            <div
              key={entry.cron}
              className={`flex items-start gap-2.5 py-2.5 pl-[2.375rem] pr-3 ${index > 0 ? "border-t" : ""}`}
            >
              <CalendarClock
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-muted-foreground"
              />
              <div className="min-w-0 flex-1 space-y-2">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <p className="text-sm font-medium">
                    {describeCron(entry.cron) || entry.cron}
                  </p>
                  {entry.next_run && (
                    <span className="flex items-center gap-1.5 self-center text-xs text-muted-foreground">
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
                  {rows.map((row) => {
                    const label = row.name || row.slug;
                    return (
                      <Link
                        key={row.id}
                        to={`/rows/${row.id}`}
                        title={`Edit ${label}`}
                        className="inline-flex max-w-full items-center rounded-full border bg-muted/40 px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <span className="min-w-0 break-words">{resolveRowName(label, { user: "each person" }) || row.slug}</span>
                      </Link>
                    );
                  })}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
