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
      <span className="flex items-center gap-1.5 text-sm font-medium text-destructive-text">
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
 * "Run" mixes a read-only history sweep with one job that writes corrections to Plex and can
 * delete a collection — and until this, the only way to tell them apart was to expand each row and
 * read a paragraph. The tag says it in words on the line; it is plain text, not a red badge, so the
 * rows that merely touch Shortlist's own data don't read as alarms.
 */
function EffectTag({
  tag,
}: {
  tag: { text: string; title: string; note?: string };
}) {
  return (
    <span
      title={tag.title}
      className="shrink-0 rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
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
  /** What pressing this changes, if it changes anything outside Shortlist — see {@link EffectTag}.
   *  `note` is the reassurance that has to stay VISIBLE beside a frightening tag; it renders on its
   *  own line under the row rather than living in the tag's `title`. */
  tag?: {
    text: string;
    title: string;
    note?: string;
  };
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

  // Next run earns a column only when there IS a schedule — an em-dash in eight rows is noise. But a
  // job that COULD be scheduled and isn't must say so: rendering nothing left "Sync check" looking
  // broken next to neighbours that all showed a time, with no way to tell an opt-in schedule from a
  // missing one.
  const nextRun =
    entry.scheduled && entry.next_run ? (
      <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground sm:w-32">
        <Clock className="size-3 shrink-0" aria-hidden="true" />
        {timeUntil(entry.next_run)}
      </span>
    ) : entry.schedule_optional ? (
      <span
        className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground sm:w-32"
        title="Off by choice, not broken — open this job to give it a schedule."
      >
        <Clock className="size-3 shrink-0" aria-hidden="true" />
        Not scheduled
      </span>
    ) : null;

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
      {/* Two layouts from one tree. A phone gets a fixed two-line grid — name and status on the first
          line, next run and tag under the name, the button at the right — so every row puts each
          piece in the same place; the old wrapping row put status, time and button wherever the
          name's length left room. From `sm` it is one line again: name, tag, status, next run,
          button, with `order` restoring that sequence around the phone-only meta wrapper. */}
      <div
        className={cn(
          "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 px-3 py-2.5 sm:flex sm:flex-wrap sm:gap-y-2",
          open && "border-b border-border/60",
        )}
      >
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          // `sm:min-w-[11rem]`, not `min-w-0`: with the row set to wrap, a floor on the name is what
          // makes the tag/status/time wrap to a second line. Without it the name absorbed every
          // pixel the others wanted and "Check and fix rows on Plex" rendered as "Check…".
          className="col-start-1 row-start-1 flex min-w-0 items-center gap-2.5 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:order-1 sm:min-w-[11rem] sm:flex-1"
        >
          <ChevronRight
            aria-hidden="true"
            className={`size-4 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
          />
          <Icon
            aria-hidden="true"
            className="size-4 shrink-0 text-muted-foreground"
          />
          {/* Wraps on a phone, truncates from `sm`. At 390 the longest job needs more than a line,
              and three of them ellipsed into nothing you could tell apart — "Put an un-paused
              person's ro…", "Remove a row's collections fr…". A name on two lines costs a few
              pixels of height and nothing else; from `sm` there is room for one line. */}
          <span className="text-sm font-medium sm:truncate">{entry.label}</span>
        </button>

        <div className="col-start-2 row-start-1 justify-self-end sm:order-3">
          <StatusChip entry={entry} queuedTitle={queuedTitle} />
        </div>

        {(tag || nextRun) && (
          // Indented to the name: chevron (1rem) + gap (0.625rem) + icon (1rem) + gap (0.625rem).
          <div className="col-start-1 row-start-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 pl-[3.25rem] sm:contents">
            {nextRun && <div className="sm:order-4">{nextRun}</div>}
            {/* Outside the expander button on purpose: it is information about the job, not part of
                the control's accessible name ("Sync check, Can delete" would be read as its name). */}
            {tag && (
              <div className="sm:order-2">
                <EffectTag tag={tag} />
              </div>
            )}
          </div>
        )}

        {action && (
          <Button
            size="sm"
            variant="outline"
            // One width for every job's "Run", so the status and next-run columns line up down the
            // list on a desktop.
            className="col-start-2 row-start-2 justify-self-end sm:order-5 sm:min-w-[6.5rem]"
            loading={action.pending}
            onClick={action.run}
            // The visible label is short because five of these stack up, but "Run" five times over
            // is useless to a screen reader — the accessible name says which job.
            aria-label={`${action.label}: ${entry.label}`}
          >
            {action.label}
          </Button>
        )}

        {/* A line of its own under everything else at every width, indented to the name it is about.
            A red "Can delete" whose only reassurance is a hover title is not reassurance at all — on
            a phone there is no hover, and the tag is all that is left. */}
        {tag?.note && (
          <p className="col-span-2 pl-[3.25rem] text-xs text-muted-foreground sm:order-6 sm:w-full">
            {tag.note}
          </p>
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
            <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-sm text-destructive-text">
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
