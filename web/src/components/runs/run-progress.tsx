import { Check, ChevronRight, Clock3, Info } from "lucide-react";
import { Link } from "react-router";

import { RowName } from "@/components/rows/row-name";
import { rowDisplayName } from "@/lib/run-rows";
import { formatDuration } from "@/lib/format";
import { currentPhase, inFlight, peopleProgress } from "@/lib/run-format";
import { describeCounts, STAGE_LABELS } from "@/lib/run-stages";
import type { RunDetail, RunLogEntry } from "@/lib/types";
import { useLiveClock } from "@/lib/use-live-clock";
import { clockTime } from "@/lib/when";

/** Live roster and row results share the same people, states, titles and detail destinations. */
export function RunProgress({ run, entries }: { run: RunDetail; entries: RunLogEntry[] }) {
  const now = useLiveClock(!run.finished_at);
  const began = run.began_at ? Date.parse(run.began_at) : NaN;
  const elapsed = Number.isFinite(began) ? Math.max(0, now - began) : null;
  const phase = currentPhase(run, entries);
  const progress = peopleProgress(run, entries);
  const latest = new Map(entries.map((entry) => [entry.user, entry]));
  const isWriting = (slug: string) => latest.get(slug)?.stage === "delivering" && Boolean(latest.get(slug)?.counts?.library);
  const working = inFlight(run, entries).sort((a, b) => Number(isWriting(b.slug)) - Number(isWriting(a.slug)));
  const processed = run.users.filter((person) => person.status !== "pending");
  const last = entries.at(-1);
  const percent = progress ? Math.min(100, Math.round(progress.done / progress.total * 100)) : null;
  if (run.finished_at) return null;

  return (
    <section aria-label="Live run progress" className="overflow-hidden rounded-xl border border-primary/15 bg-gradient-to-br from-primary/[0.035] via-card to-card">
      <div className="space-y-3 px-4 py-4 sm:px-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              {run.status === "queued" ? "Waiting to start" : phase?.tail ? "Finishing up" : "In progress"}
            </p>
            <h2 className="mt-1 text-lg font-medium tracking-tight sm:text-xl">
              {run.status === "queued" ? "Queued — waiting to start" : progress && !phase?.tail ? "Building your rows" : phase?.label ?? (run.began_at ? "Waiting for progress updates" : "Queued")}
            </h2>
          </div>
          {progress && (
            <p className="shrink-0 text-3xl font-semibold leading-none tracking-tight tabular-nums">
              {progress.done}<span className="ml-1.5 text-base font-normal text-muted-foreground">/ {progress.total}</span>
              <span className="mt-0.5 block text-right text-xs font-normal leading-4 tracking-normal text-muted-foreground">people processed</span>
            </p>
          )}
        </div>
        {progress && (
          <div role="progressbar" aria-label="People processed" aria-valuenow={progress.done} aria-valuemax={progress.total} aria-valuemin={0} className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary transition-[width] motion-reduce:transition-none" style={{ width: `${percent}%` }} />
          </div>
        )}
        <div className="flex flex-wrap justify-between gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>{percent !== null ? `${percent}% processed · ` : ""}{working.length} {working.length === 1 ? "person" : "people"} in progress</span>
          <span className="flex flex-wrap gap-x-3">
            {elapsed !== null && <span>{formatDuration(elapsed)} elapsed</span>}
            {last?.ts && <time dateTime={last.ts}>Updated {clockTime(last.ts)}</time>}
          </span>
        </div>
        {/* Preserve the precise server phase for assistive technology, including shared-row work. */}
        {progress && !phase?.tail && <p className="sr-only">{phase?.label}</p>}
      </div>

      {working.length > 0 && (
        <div className="border-t border-border/60 px-4 sm:px-6">
          <p className="pb-1 pt-3 text-xs font-semibold uppercase tracking-widest text-muted-foreground">Working on now</p>
          <ul aria-label="In progress" className="divide-y divide-border/60">
            {working.map((person) => {
              const entry = latest.get(person.slug);
              const counts = entry?.counts ?? {};
              const row = typeof counts.row === "string" ? counts.row : null;
              const library = typeof counts.library === "string" ? counts.library : undefined;
              const waiting = entry?.stage === "delivering" && !library;
              const writing = entry?.stage === "delivering" && Boolean(library);
              const initials = person.name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
              const destination = `?user=${encodeURIComponent(person.slug)}#run-rows`;
              return (
                <li key={person.slug}>
                  <details className="group" open={writing || undefined}>
                    <summary className="grid cursor-pointer list-none grid-cols-[1.75rem_minmax(0,1fr)_auto_0.75rem] items-center gap-2 py-2 marker:content-none sm:grid-cols-[2rem_minmax(0,1fr)_9rem_0.75rem] sm:gap-3 sm:py-1.5 [&::-webkit-details-marker]:hidden">
                      <span aria-hidden="true" className="grid size-7 place-items-center rounded-full bg-primary/10 text-xs font-medium text-primary/80 sm:size-8">{initials}</span>
                      <span className="min-w-0">
                        <span className="block text-xs font-medium leading-4">{person.name}</span>
                        <span className="mt-0.5 block break-words text-xs leading-4 text-muted-foreground">
                          {row ? <RowName name={library ? row : rowDisplayName(row)} libraryName={library} className="font-normal" /> : library ?? "Preparing recommendations"}
                        </span>
                      </span>
                      <span className={`flex items-center justify-end gap-1.5 text-xs sm:justify-start sm:text-xs ${writing ? "text-primary" : "text-muted-foreground"}`}>
                        {waiting ? <Clock3 aria-hidden="true" className="size-3 shrink-0" /> : writing ? <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-primary" /> : null}
                        {waiting ? "Waiting for Plex" : writing ? "Writing to Plex" : STAGE_LABELS[entry?.stage ?? ""] ?? entry?.stage}
                      </span>
                      <ChevronRight aria-hidden="true" className="size-3 text-muted-foreground transition-transform group-open:rotate-90 motion-reduce:transition-none" />
                    </summary>
                    <div className="mb-2 ml-9 space-y-1 border-l-2 border-primary/35 pl-3 text-xs sm:ml-11">
                      <p className="text-xs leading-relaxed text-muted-foreground">{waiting ? "Titles are ready. Waiting for the current Plex write to finish." : `${library ? `${library} · ` : ""}${describeCounts(Object.fromEntries(Object.entries(counts).filter(([key]) => key !== "row" && key !== "library"))) || (STAGE_LABELS[entry?.stage ?? ""] ?? entry?.stage)}`}</p>
                      <Link to={destination} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
                        Open {person.name}’s row and library details <ChevronRight aria-hidden="true" className="size-3" />
                      </Link>
                    </div>
                  </details>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {processed.length > 0 && (
        <div className="mx-4 border-t border-border/60 py-3 sm:mx-6">
          <p className="mb-2.5 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
            <Check aria-hidden="true" className="size-3.5 text-success" />Processed <span className="ml-1 font-normal">{processed.length} people</span>
          </p>
          <ul aria-label="Processed people" className="flex flex-wrap gap-1.5">
            {processed.map((person) => (
              <li key={person.slug}>
                <Link to={`?user=${encodeURIComponent(person.slug)}#run-rows`} className={`inline-flex rounded-md border px-2 py-1 text-xs transition-colors hover:border-primary/40 hover:text-primary ${person.error ? "border-destructive/25 text-destructive-text" : person.status === "skipped" ? "border-border text-muted-foreground" : "border-success/15 bg-success/[0.035] text-foreground/80"}`}>
                  {person.error ? "! " : person.status === "skipped" ? "— " : "✓ "}{person.display_name || person.username || person.slug}{person.error ? " · failed" : person.status === "skipped" ? " · skipped" : ""}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
      <details className="mx-4 border-t border-border/60 py-3 text-xs text-muted-foreground sm:mx-6">
        <summary className="flex cursor-pointer list-none items-center gap-2 [&::-webkit-details-marker]:hidden"><Info aria-hidden="true" className="size-3.5" />Why a refresh can take a while<ChevronRight aria-hidden="true" className="ml-auto size-3" /></summary>
        <p className="max-w-3xl pl-5 pt-2 leading-relaxed">Plex removes titles from a collection one at a time. On a large TV library, removing several titles can take a few minutes. Unchanged rows skip that work. Open a person’s row for its library and delivery details.</p>
      </details>
    </section>
  );
}
