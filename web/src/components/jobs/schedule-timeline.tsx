import { Pencil, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { CronInput } from "@/components/cron-input";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { describeCron } from "@/lib/cron";
import { formatDate } from "@/lib/format";
import { useSaveSettings, useSchedule } from "@/lib/queries";
import type { ScheduleEntry } from "@/lib/types";

/** Sorts by when it next fires. Anything with no next run (unscheduled, or a bad expression the
 *  scheduler refused) sinks to the bottom rather than pretending to a place in the timeline. */
function byNextRun(a: ScheduleEntry, b: ScheduleEntry): number {
  if (!a.next_run) return b.next_run ? 1 : 0;
  if (!b.next_run) return -1;
  return a.next_run.localeCompare(b.next_run);
}

function ClockTime({ iso }: { iso: string | null }) {
  if (!iso) {
    return <span className="text-muted-foreground/60">—</span>;
  }
  return (
    <span className="tabular-nums">
      {new Date(iso).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })}
    </span>
  );
}

function EntryRow({
  entry,
  onSaveCron,
  saving,
}: {
  entry: ScheduleEntry;
  onSaveCron: (setting: string, cron: string) => void;
  saving: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const isRows = entry.type === "rows";
  const setting = entry.setting ?? "";

  return (
    <div className="border-b p-4 last:border-b-0">
      {/* No flex-wrap: with a long description the left block outgrew the row and shoved "Change"
          onto its own line, so the button sat in a different place on every row. The text wraps
          instead — min-w-0 is what lets it, and the button stays pinned right. */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <div className="w-14 shrink-0 pt-0.5 text-sm font-medium">
            <ClockTime iso={entry.next_run} />
          </div>
          <div className="min-w-0 space-y-0.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">
                {isRows ? "Build rows" : entry.label}
              </span>
              {entry.writes_plex && (
                <Badge
                  variant="outline"
                  className="gap-1 font-normal"
                  title="Writes to Plex or plex.tv, so it never runs alongside a run or another writer."
                >
                  <ShieldAlert className="h-3 w-3" aria-hidden />
                  writes to Plex
                </Badge>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              {describeCron(entry.cron) ||
                entry.cron ||
                "Off — runs only when you press it"}
              {/* Say where the cron came from. Without this an inherited default reads as a choice
                  the owner made, and they go looking for a setting they never set. */}
              {entry.using_default && entry.cron && (
                <span className="text-muted-foreground/70"> · built-in default</span>
              )}
            </p>
            {isRows && entry.rows && (
              <p className="text-sm text-muted-foreground">
                {entry.rows.map((row, i) => (
                  <span key={row.id}>
                    {i > 0 && ", "}
                    <Link
                      to="/rows"
                      className="underline underline-offset-2 hover:text-foreground"
                    >
                      {row.name}
                    </Link>
                  </span>
                ))}
              </p>
            )}
            {!isRows && entry.description && (
              <p className="text-sm text-muted-foreground">
                {entry.description}
              </p>
            )}
            {entry.next_run && (
              <p className="text-xs text-muted-foreground/70">
                Next {formatDate(entry.next_run)}
              </p>
            )}
          </div>
        </div>

        {/* Row crons are edited where a row is edited — one owner per setting, so a cron can never
            be validated two different ways. */}
        {isRows ? (
          <Button asChild variant="ghost" size="sm" className="shrink-0">
            <Link to="/rows">
              <Pencil aria-hidden="true" />
              Edit rows
            </Link>
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0"
            onClick={() => setEditing((v) => !v)}
            aria-expanded={editing}
          >
            <Pencil aria-hidden="true" />
            {entry.cron ? "Change" : "Add a schedule"}
          </Button>
        )}
      </div>

      {editing && !isRows && setting && (
        <div className="mt-3 pl-[4.25rem]">
          <CronInput
            value={entry.cron}
            onChange={(cron) => {
              onSaveCron(setting, cron);
              setEditing(false);
            }}
          />
          {entry.optional && entry.cron && (
            <Button
              variant="ghost"
              size="sm"
              className="mt-2 text-muted-foreground"
              disabled={saving}
              onClick={() => {
                onSaveCron(setting, "");
                setEditing(false);
              }}
            >
              Turn this schedule off
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Everything on a timer, in the order it happens — a tab of the Jobs page.
 *
 * Each cron was already editable, but a job's lived inside that job's expanded settings and a row's
 * inside the row editor, so "what happens overnight, and in what order?" could only be answered by
 * opening a dozen panels and doing the arithmetic yourself.
 *
 * It lives under Jobs rather than in its own nav entry because "what background work exists" and
 * "when does it run" are two views of ONE thing: a separate Schedule page meant every job appeared
 * in two places, and neither one could answer a whole question on its own.
 */
export function ScheduleTimeline() {
  const query = useSchedule();
  const saveSettings = useSaveSettings();

  return (
    <QueryBoundary query={query} skeleton={<Skeleton className="h-64 w-full" />}>
      {(data) => {
        const entries: ScheduleEntry[] = [...data.jobs, ...data.rows];
        const scheduled = entries.filter((e) => e.cron).sort(byNextRun);
        const off = entries.filter((e) => !e.cron);

        return (
          <div className="space-y-6">
            <p className="text-sm text-muted-foreground">
              Rows and background jobs together, in the order they fire. Times
              are your server&rsquo;s.
            </p>

            {scheduled.length === 0 ? (
              <EmptyState
                title="Nothing is scheduled"
                hint="Give a row a schedule, or turn on one of the background jobs below."
              />
            ) : (
              <div className="overflow-hidden rounded-xl border">
                {scheduled.map((entry) => (
                  <EntryRow
                    key={entry.setting || `rows-${entry.cron}`}
                    entry={entry}
                    saving={saveSettings.isPending}
                    onSaveCron={(setting, cron) =>
                      saveSettings.mutate({ [setting]: cron })
                    }
                  />
                ))}
              </div>
            )}

            {off.length > 0 && (
              <div className="space-y-2">
                <h2 className="text-sm font-medium text-muted-foreground">
                  Not scheduled
                </h2>
                <div className="overflow-hidden rounded-xl border">
                  {off.map((entry) => (
                    <EntryRow
                      key={entry.setting || `rows-${entry.cron}`}
                      entry={entry}
                      saving={saveSettings.isPending}
                      onSaveCron={(setting, cron) =>
                        saveSettings.mutate({ [setting]: cron })
                      }
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      }}
    </QueryBoundary>
  );
}
