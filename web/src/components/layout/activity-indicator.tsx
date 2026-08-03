import { Activity, Check, CircleAlert, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useDismissable } from "@/lib/use-dismissable";
import { Link } from "react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { timeAgo } from "@/lib/format";
import {
  isInFlight,
  jobTransitions,
  queuedReason,
  statusMap,
  useJobActivity,
  useJobLabels,
  useRunActive,
  useWritesPlex,
} from "@/lib/job-activity";
import type { Job } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Job kinds whose SUCCESS is already announced by the page that triggers them, naming the person
 *  and the decision rather than the machinery. Failures are never suppressed. */
const ANNOUNCED_BY_THE_PAGE = new Set([
  "user.cleanup",
  "user.hide",
  "user.restore",
]);

/** Who or what a job is acting on, when its payload says. Undefined rather than a filler string:
 *  sonner omits the second line entirely, which reads better than a line that says nothing. */
function jobTarget(job: Job): string | undefined {
  const payload = job.payload ?? {};
  const slug = payload.slug;
  if (typeof slug === "string" && slug) return slug;
  const reason = payload.reason;
  if (typeof reason === "string" && reason) return reason;
  return undefined;
}

function JobLine({
  job,
  label,
  queued,
}: {
  job: Job;
  label: string;
  /** Why it's waiting, when `job.status === "queued"` — ignored otherwise. */
  queued?: { text: string; title: string };
}) {
  return (
    <li className="flex items-center justify-between gap-3 py-1 text-sm">
      <span className="flex min-w-0 items-center gap-2">
        {job.status === "running" ? (
          <Loader2
            className="h-3 w-3 shrink-0 animate-spin text-primary"
            aria-hidden
          />
        ) : job.status === "queued" ? (
          <span
            className="h-2 w-2 shrink-0 rounded-full border border-muted-foreground/60"
            aria-hidden
          />
        ) : job.status === "failed" ? (
          <CircleAlert
            className="h-3 w-3 shrink-0 text-destructive-text"
            aria-hidden
          />
        ) : (
          <Check className="h-3 w-3 shrink-0 text-success" aria-hidden />
        )}
        <span className="truncate">{label}</span>
      </span>
      <span
        className="shrink-0 text-xs text-muted-foreground"
        title={job.status === "queued" ? queued?.title : undefined}
      >
        {job.status === "queued"
          ? (queued?.text ?? "waiting its turn")
          : job.finished_at
            ? timeAgo(job.finished_at)
            : job.started_at
              ? timeAgo(job.started_at)
              : ""}
      </span>
    </li>
  );
}

/**
 * Live background-work indicator: a count in the header, a popover of what's happening, and a toast
 * when something starts or finishes.
 *
 * The gap this closes: the automatic job kinds — the cleanup queued when you disable someone, the
 * hide when you pause them, the reconcile when you change a row — had NO enqueue-time feedback at
 * all. You did a thing, and nothing visibly happened until you thought to open the Jobs page.
 */
/** `align` is which EDGE of the panel is pinned to the button — the same convention as
 *  {@link NotificationBell}. In the w-60 sidebar a w-80 panel pinned right runs off the left of the
 *  screen and its labels are cut in half, so the rail passes "left" to open it rightward into
 *  <main>; the mobile header, where the button sits at the right edge, passes "right". */
export function ActivityIndicator({
  align = "left",
}: {
  align?: "left" | "right";
}) {
  const query = useJobActivity();
  const labelFor = useJobLabels();
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  // What we announced last poll. A ref, not state: it must not itself trigger a render, and the
  // very first poll seeds it silently — announcing a server's entire recent history on page load
  // would be a wall of toasts for work that finished yesterday.
  const seen = useRef<Map<number, Job["status"]> | null>(null);

  // Same behaviour as the notification bell beside it, which had this and this one did not.
  useDismissable(open, panelRef, () => setOpen(false));

  const jobs = query.data ?? [];
  const writesPlexFor = useWritesPlex();

  useEffect(() => {
    if (!query.data) return;
    const previous = seen.current;
    seen.current = statusMap(query.data);
    if (previous === null) return; // first poll: seed only
    const { started, finished, failed } = jobTransitions(previous, query.data);
    for (const job of started) {
      // No "Running in the background" — the spinner already says that, and repeating it on every
      // toast stacked identical second lines that carried no information. The TARGET does: a
      // cleanup for one person reads very differently from one for forty.
      toast.loading(`${labelFor(job.kind)}…`, {
        id: `job-${job.id}`,
        description: jobTarget(job),
      });
    }
    for (const job of finished) {
      // The page that caused these already said what was happening, by name — "Turning off sarah…"
      // then "sarah is off". Announcing the machinery behind it as well ("Remove a disabled person's
      // rows · Removed 0 row(s) for s_flix") is a second, vaguer toast for one decision. Failures
      // still speak: nothing else would say the cleanup didn't work.
      if (ANNOUNCED_BY_THE_PAGE.has(job.kind)) continue;
      toast.success(labelFor(job.kind), {
        id: `job-${job.id}`,
        description: job.detail || undefined,
      });
    }
    for (const job of failed) {
      toast.error(labelFor(job.kind), {
        id: `job-${job.id}`,
        description: job.error || "Open Jobs for the details.",
      });
    }
  }, [query.data, labelFor]);

  const inFlight = jobs.filter(isInFlight);
  // Split rather than one "Running" heading over both: a queued job is NOT running, and heading it
  // that way is exactly what left an operator unable to tell "working" from "stuck".
  const running = jobs.filter((job) => job.status === "running");
  const queued = jobs.filter((job) => job.status === "queued");
  const recent = jobs.filter((job) => !isInFlight(job)).slice(0, 5);
  const failing = jobs.filter((job) => job.status === "failed").length;
  const runActive = useRunActive(queued.length > 0);

  return (
    <div className="relative" ref={panelRef}>
      <Button
        variant="ghost"
        size="icon"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={
          inFlight.length
            ? `Background work: ${inFlight.length} in progress`
            : "Background work"
        }
        title="Background work"
      >
        <span className="relative inline-flex">
          {inFlight.length > 0 ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          ) : (
            <Activity className="h-4 w-4" aria-hidden />
          )}
          {inFlight.length > 0 && (
            <span
              className={cn(
                "absolute -right-2 -top-2 rounded-full px-1 text-[10px] font-semibold leading-4",
                failing ? "bg-destructive text-white" : "bg-primary text-white",
              )}
            >
              {inFlight.length}
            </span>
          )}
        </span>
      </Button>

      {open && (
        <div
          className={cn(
            "absolute z-50 mt-2 w-80 rounded-lg border bg-elevated p-3 shadow-lg",
            align === "left" ? "left-0" : "right-0",
          )}
          role="dialog"
          aria-label="Background work"
        >
          {inFlight.length === 0 && recent.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nothing running. Background work shows up here as it happens.
            </p>
          ) : (
            <div className="space-y-3">
              {running.length > 0 && (
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Running
                  </p>
                  <ul>
                    {running.map((job) => (
                      <JobLine
                        key={job.id}
                        job={job}
                        label={labelFor(job.kind)}
                      />
                    ))}
                  </ul>
                </div>
              )}
              {queued.length > 0 && (
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Queued
                  </p>
                  <ul>
                    {queued.map((job) => (
                      <JobLine
                        key={job.id}
                        job={job}
                        label={labelFor(job.kind)}
                        queued={queuedReason(
                          writesPlexFor(job.kind),
                          runActive,
                        )}
                      />
                    ))}
                  </ul>
                </div>
              )}
              {recent.length > 0 && (
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Recent
                  </p>
                  <ul>
                    {recent.map((job) => (
                      <JobLine
                        key={job.id}
                        job={job}
                        label={labelFor(job.kind)}
                      />
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
          <Link
            to="/jobs"
            onClick={() => setOpen(false)}
            className="mt-3 block text-xs text-primary underline-offset-4 hover:underline"
          >
            Open Jobs →
          </Link>
        </div>
      )}
    </div>
  );
}
