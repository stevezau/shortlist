import { CheckCircle2, ChevronRight, Clock, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

import { JobHistory } from "@/components/jobs/job-history";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { jobStatusLabel, timeAgo, timeUntil } from "@/lib/format";
import { jobDuration, jobStatusTone } from "@/lib/job-status";
import type { JobCatalogEntry } from "@/lib/types";

/** The last outcome, in one line: did it work, when, how long, and what it says it did. */
function LastRun({ entry }: { entry: JobCatalogEntry }) {
  const last = entry.last;
  if (!last) {
    return (
      <p className="text-sm text-muted-foreground">
        Hasn&rsquo;t run yet
        {entry.scheduled && entry.next_run
          ? ` — first run ${timeUntil(entry.next_run)}.`
          : "."}
      </p>
    );
  }
  const took = jobDuration(last);
  return (
    <div className="space-y-1">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        {last.status === "failed" ? (
          <TriangleAlert
            aria-hidden="true"
            className="size-4 shrink-0 text-destructive"
          />
        ) : last.status === "done" ? (
          <CheckCircle2
            aria-hidden="true"
            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-500"
          />
        ) : (
          <span
            aria-hidden="true"
            className="size-2 shrink-0 animate-pulse rounded-full bg-primary"
          />
        )}
        <span className={`font-medium ${jobStatusTone(last.status)}`}>
          {jobStatusLabel(last)}
        </span>
        <span className="text-muted-foreground">
          {last.created_at ? timeAgo(last.created_at) : ""}
          {took ? ` · took ${took}` : ""}
        </span>
      </p>
      {/* The error wins over the detail line: a failure's reason is the whole point of looking. */}
      {last.error ? (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-sm text-destructive">
          {last.error}
        </p>
      ) : last.detail ? (
        <p className="text-sm text-muted-foreground">{last.detail}</p>
      ) : null}
    </div>
  );
}

/**
 * One job, as a card: what it does, when it next runs, how it went last time, the controls to run
 * it, and its own history.
 *
 * Every job on the page renders through this — including the four with bespoke controls, which pass
 * them as `children`. That is the point of the restructure: previously "sync users" was a card with
 * a button in one place and anonymous rows in a shared table somewhere below, so "did that work?"
 * meant scanning a mixed-kind list for the right row.
 */
export function JobCard({
  entry,
  icon: Icon,
  children,
  statusUnknown = false,
}: {
  entry: JobCatalogEntry;
  icon: LucideIcon;
  /** Bespoke controls for this job — a cron picker, a progress bar, a dry-run preview. */
  children?: React.ReactNode;
  /** The catalogue hasn't answered yet, so this card knows nothing about past runs. Suppresses the
   *  status block rather than letting a placeholder claim the job has never run. */
  statusUnknown?: boolean;
}) {
  const [historyOpen, setHistoryOpen] = useState(false);
  const active = entry.running + entry.queued;

  return (
    <Card
      className={entry.failed > 0 ? "border-destructive/40" : undefined}
      data-testid={`job-${entry.kind}`}
    >
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Icon aria-hidden="true" className="size-5 text-muted-foreground" />
          {entry.label}
          {active > 0 && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
              <span className="size-1.5 animate-pulse rounded-full bg-primary" />
              {entry.running > 0 ? "running" : "queued"}
            </span>
          )}
          {!entry.manual && (
            <Badge variant="secondary" className="font-normal">
              automatic
            </Badge>
          )}
          {entry.failed > 0 && (
            <Badge variant="destructive" className="font-normal">
              {entry.failed} failed
            </Badge>
          )}
        </CardTitle>
        <CardDescription>{entry.description}</CardDescription>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1 text-xs text-muted-foreground">
          {entry.scheduled && entry.next_run && (
            <span className="flex items-center gap-1.5">
              <Clock className="size-3.5 shrink-0" aria-hidden="true" />
              Next: {timeUntil(entry.next_run)}
            </span>
          )}
          {/* A job with no button has to say what DOES start it, or the card reads as broken. */}
          {!entry.manual && entry.trigger && <span>{entry.trigger}</span>}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {children}
        {!statusUnknown && (
          <>
            <LastRun entry={entry} />
            <div>
              <button
                type="button"
                onClick={() => setHistoryOpen(!historyOpen)}
                aria-expanded={historyOpen}
                className="flex items-center gap-1.5 rounded text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ChevronRight
                  aria-hidden="true"
                  className={`size-4 transition-transform ${historyOpen ? "rotate-90" : ""}`}
                />
                Previous runs
                {entry.total > 0 && (
                  <span className="tabular-nums">({entry.total})</span>
                )}
              </button>
              {/* Fetched only when opened: a page of cards must not fire one history request each. */}
              {historyOpen && (
                <div className="pt-2">
                  <JobHistory kind={entry.kind} />
                </div>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
