import { CheckCircle2, ChevronRight, Clock, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

import { JobHistory } from "@/components/jobs/job-history";
import { Button } from "@/components/ui/button";
import { timeAgo, timeUntil } from "@/lib/format";
import { cn } from "@/lib/utils";
import { jobDuration } from "@/lib/job-status";
import type { JobCatalogEntry } from "@/lib/types";

/**
 * The last outcome as ONE scannable token — the thing you sweep your eye down the list for.
 *
 * Deliberately not a sentence: nine of these stack up, and "Done · Synced 7 people from plex.tv ·
 * 2h ago" on every line is what made the old card list unreadable. The full detail is one click away.
 */
function StatusChip({
  entry,
  queuedTitle,
}: {
  entry: JobCatalogEntry;
  /** Why it's queued, when it is — the fuller explanation the terse chip text has no room for. */
  queuedTitle?: string;
}) {
  if (entry.running + entry.queued > 0) {
    const queued = entry.running === 0;
    return (
      <span
        className="flex items-center gap-1.5 text-sm font-medium text-primary"
        title={queued ? queuedTitle : undefined}
      >
        <span
          aria-hidden="true"
          className="size-1.5 animate-pulse rounded-full bg-primary"
        />
        {queued ? "Queued" : "Running"}
      </span>
    );
  }
  const last = entry.last;
  if (!last) {
    return <span className="text-sm text-muted-foreground">never run</span>;
  }
  if (last.status === "failed") {
    return (
      <span className="flex items-center gap-1.5 text-sm font-medium text-destructive">
        <TriangleAlert aria-hidden="true" className="size-3.5 shrink-0" />
        Failed
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
      <CheckCircle2
        aria-hidden="true"
        className="size-3.5 shrink-0 text-success"
      />
      {last.created_at ? timeAgo(last.created_at) : "done"}
    </span>
  );
}

/**
 * What a job CHANGES, said on the line rather than three clicks in.
 *
 * "Run now" mixes a read-only history sweep with one job that writes corrections to Plex and can
 * delete a collection — and until this, the only way to tell them apart was to expand each row and
 * read a paragraph. A destructive button that looks exactly like a harmless one is the problem;
 * `destructive` is what makes that one look different at a glance.
 */
function EffectTag({
  tag,
}: {
  tag: { text: string; title: string; destructive?: boolean };
}) {
  return (
    <span
      title={tag.title}
      className={cn(
        "shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium",
        tag.destructive
          ? "border-destructive/40 bg-destructive/10 text-destructive"
          : "border-border bg-muted text-muted-foreground",
      )}
    >
      {tag.text}
    </span>
  );
}

/**
 * One job as a single line, expanding to everything about it.
 *
 * The list is NAVIGATION, not content: name, health and next run on the line, and the description,
 * settings and history only once you ask. The previous design gave every job a full card with its
 * paragraph and its controls permanently on screen — nine of those is ~1800px of scroll, and four of
 * them are jobs you can never start.
 */
export function JobRow({
  entry,
  icon: Icon,
  action,
  live,
  panel,
  first,
  queuedTitle,
  tag,
}: {
  entry: JobCatalogEntry;
  icon: LucideIcon;
  /** The row's primary action. Absent for automatic jobs — nothing here may start those. */
  action?: { label: string; run: () => void; pending: boolean };
  /** What pressing this changes, if it changes anything outside Shortlist — see {@link EffectTag}. */
  tag?: { text: string; title: string; destructive?: boolean };
  /** Live progress, shown under the line WITHOUT expanding — a running job must be visible while
   *  the row is collapsed, or pressing Run looks like it did nothing. */
  live?: React.ReactNode;
  /** Settings and results for this job, revealed on expand. */
  panel?: React.ReactNode;
  first?: boolean;
  /** Why this job's kind is queued right now, when it is — see {@link StatusChip}. */
  queuedTitle?: string;
}) {
  const [open, setOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const last = entry.last;

  return (
    // An OPEN row is tinted end to end — header included — so the panel reads as belonging to the job
    // above it. Without that the body just ran into the next row and "Back up the database" looked
    // like part of Privacy sync's settings.
    <div
      className={cn(
        first ? "" : "border-t",
        open && "bg-muted/30 ring-1 ring-inset ring-border",
      )}
      data-testid={`job-${entry.kind}`}
    >
      <div
        className={cn(
          "flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5",
          open && "border-b border-border/60",
        )}
      >
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          // `min-w-[11rem]`, not `min-w-0`: with the row set to wrap, a floor on the name is what
          // makes the tag/status/time wrap to a second line. Without it the name absorbed every
          // pixel the others wanted and "Check and fix rows on Plex" rendered as "Check…".
          className="flex min-w-[11rem] flex-1 items-center gap-2.5 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronRight
            aria-hidden="true"
            className={`size-4 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
          />
          <Icon
            aria-hidden="true"
            className="size-4 shrink-0 text-muted-foreground"
          />
          <span className="truncate text-sm font-medium">{entry.label}</span>
        </button>

        {/* Outside the expander button on purpose: it is information about the job, not part of the
            control's accessible name ("Sync check, Can delete" would be read as the button's name). */}
        {tag && <EffectTag tag={tag} />}

        <StatusChip entry={entry} queuedTitle={queuedTitle} />

        {/* Next run earns a column only when there IS a schedule — an em-dash in eight rows is noise.
            But a job that COULD be scheduled and isn't must say so: rendering nothing left "Sync
            check" looking broken next to neighbours that all showed a time, with no way to tell an
            opt-in schedule from a missing one. */}
        {entry.scheduled && entry.next_run ? (
          <span className="hidden w-32 shrink-0 items-center gap-1.5 text-xs text-muted-foreground sm:flex">
            <Clock className="size-3 shrink-0" aria-hidden="true" />
            {timeUntil(entry.next_run)}
          </span>
        ) : entry.schedule_optional ? (
          <span
            className="hidden w-32 shrink-0 items-center gap-1.5 text-xs text-muted-foreground/60 sm:flex"
            title="Off by choice, not broken — open this job to give it a schedule."
          >
            <Clock className="size-3 shrink-0" aria-hidden="true" />
            Not scheduled
          </span>
        ) : null}

        {action && (
          <Button
            size="sm"
            variant="outline"
            loading={action.pending}
            onClick={action.run}
            // The visible label is short because five of these stack up, but "Run" five times over
            // is useless to a screen reader — the accessible name says which job.
            aria-label={`${action.label}: ${entry.label}`}
          >
            {action.label}
          </Button>
        )}
      </div>

      {/* Callers must pass null, not an element whose children are all conditional — this wrapper
          carries padding, so an "empty" live slot leaves dead space under the row for ever. */}
      {live && (
        <div data-testid="job-live" className="px-3 pb-3">
          {live}
        </div>
      )}

      {open && (
        <div className="space-y-4 border-l-2 border-primary/40 px-3 py-3">
          {/* Paragraph breaks are meaningful in these — the server writes "\n\n" between "what it
              does" and "when it runs", and rendering them as one block ran the two together. */}
          <div className="space-y-2">
            {entry.description
              .split("\n\n")
              .filter(Boolean)
              .map((paragraph) => (
                <p key={paragraph} className="text-sm text-muted-foreground">
                  {paragraph}
                </p>
              ))}
          </div>
          {/* A job with no button has to say what DOES start it, or the row reads as broken — and a
              job that has one still needs it whenever something ELSE also queues it. "Why did this
              run at 3am when I never pressed it?" was unanswerable for the manual kinds, because
              their trigger text was written and then never rendered. */}
          {entry.trigger && (
            <p className="text-sm text-muted-foreground">{entry.trigger}</p>
          )}

          {panel}

          {/* The error wins over the detail line: a failure's reason is the point of looking. */}
          {last?.error ? (
            <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-sm text-destructive">
              {last.error}
            </p>
          ) : last?.detail ? (
            <p className="text-sm">
              <span className="text-muted-foreground">Last run: </span>
              {last.detail}
              {jobDuration(last) ? ` · ${jobDuration(last)}` : ""}
            </p>
          ) : null}

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
            {/* Fetched only when opened: a page of rows must not fire one history request each. */}
            {historyOpen && (
              <div className="pt-2">
                <JobHistory kind={entry.kind} />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
