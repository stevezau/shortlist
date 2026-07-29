import { ArrowDownToLine, Download, Loader2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  countLabel,
  isServerStage,
  LOG_FILTERS,
  matchesLogFilter,
  progressLabel,
  STAGE_LABELS,
  type LogFilter,
} from "@/lib/run-stages";
import type { RunLogEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

function LogLine({ entry }: { entry: RunLogEntry }) {
  const time = entry.ts ? new Date(entry.ts).toLocaleTimeString() : "";
  const label = STAGE_LABELS[entry.stage] ?? entry.stage;
  const counts = entry.counts ?? {};
  // "3/5" reads as progress; "3 done · of 5" reads as two unrelated numbers.
  const progress = progressLabel(counts);
  const detail =
    progress ??
    Object.entries(counts)
      .map(([k, v]) => countLabel(k, v))
      .join(" · ");
  const server = isServerStage(entry.user);

  return (
    <div className="flex gap-2 py-px">
      {time && (
        <span className="w-20 shrink-0 tabular-nums text-muted-foreground">
          {time}
        </span>
      )}
      <span
        className={cn(
          "w-32 shrink-0 truncate",
          server ? "text-muted-foreground/50" : "font-medium",
        )}
      >
        {/* A server-wide phase has no person; an em-dash keeps the column aligned without
            inventing a subject for it. */}
        {server ? "——" : entry.user}
      </span>
      <span
        className={cn(
          "min-w-0",
          entry.stage === "error"
            ? "text-destructive"
            : "text-muted-foreground",
        )}
      >
        {label}
        {detail ? ` · ${detail}` : ""}
        {entry.reason ? ` — ${entry.reason}` : ""}
      </span>
    </div>
  );
}

/**
 * A run's full activity log: filterable, searchable, downloadable, and pinned to the tail while live.
 *
 * The log used to be a squashed 18rem box at the very bottom of a long page — the one thing you
 * actually need while a run is in flight, placed where you had to scroll past everything else to
 * reach it.
 */
export function RunLogPanel({
  runId,
  entries,
  running,
  people,
}: {
  runId: number;
  entries: RunLogEntry[];
  running: boolean;
  /** Slugs seen in this run, for the person filter. */
  people: string[];
}) {
  const [filter, setFilter] = useState<LogFilter>("all");
  const [person, setPerson] = useState("");
  const [search, setSearch] = useState("");
  // Follow the tail unless the reader has scrolled up. Pinning is a state, not a guess about
  // scroll position, so reading an earlier line isn't fought on the next update.
  const [pinned, setPinned] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return entries.filter((entry) => {
      if (!matchesLogFilter(filter, entry.stage, entry.user)) return false;
      if (person && entry.user !== person) return false;
      if (!needle) return true;
      const label = STAGE_LABELS[entry.stage] ?? entry.stage;
      return `${entry.user} ${label} ${entry.reason ?? ""}`
        .toLowerCase()
        .includes(needle);
    });
  }, [entries, filter, person, search]);

  useEffect(() => {
    const box = boxRef.current;
    if (box && pinned) box.scrollTop = box.scrollHeight;
  }, [visible.length, pinned]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          value={filter}
          options={LOG_FILTERS}
          onChange={setFilter}
          ariaLabel="Filter the activity log"
        />
        {people.length > 1 && (
          <select
            value={person}
            onChange={(event) => setPerson(event.target.value)}
            aria-label="Filter by person"
            className="h-8 rounded-md border bg-background px-2 text-sm"
          >
            <option value="">Everyone</option>
            {people.map((slug) => (
              <option key={slug} value={slug}>
                {slug}
              </option>
            ))}
          </select>
        )}
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Filter…"
          aria-label="Search the activity log"
          className="h-8 w-40"
        />
        <div className="ml-auto flex items-center gap-2">
          {running && (
            <Button
              variant={pinned ? "secondary" : "ghost"}
              size="sm"
              onClick={() => setPinned((v) => !v)}
              title={
                pinned
                  ? "Following new lines as they arrive"
                  : "Jump to the latest line and follow it"
              }
            >
              {pinned ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <ArrowDownToLine className="h-3.5 w-3.5" aria-hidden />
              )}
              {pinned ? "Live" : "Jump to latest"}
            </Button>
          )}
          <Button asChild variant="ghost" size="sm">
            <a href={`/api/runs/${runId}/log?format=text`} download>
              <Download className="h-3.5 w-3.5" aria-hidden />
              Download
            </a>
          </Button>
        </div>
      </div>

      <div
        ref={boxRef}
        // Tall enough to be the page's main content rather than a footnote, and scrolling in its
        // own box so the page body never scrolls sideways or jumps.
        className="h-[60vh] min-h-64 overflow-auto rounded-md bg-muted/40 p-3 font-mono text-xs"
        role="log"
        aria-live={pinned ? "polite" : "off"}
        aria-label="Run activity log"
        onScroll={(event) => {
          const box = event.currentTarget;
          const nearBottom =
            box.scrollHeight - box.scrollTop - box.clientHeight < 40;
          if (!nearBottom && pinned) setPinned(false);
        }}
      >
        {visible.length === 0 ? (
          <p className="text-muted-foreground">
            {entries.length === 0
              ? running
                ? "Starting…"
                : "No activity recorded for this run."
              : "Nothing matches those filters."}
          </p>
        ) : (
          visible.map((entry) => (
            <LogLine
              key={entry.seq ?? `${entry.ts}-${entry.stage}`}
              entry={entry}
            />
          ))
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        {visible.length === entries.length
          ? `${entries.length} ${entries.length === 1 ? "line" : "lines"}`
          : `${visible.length} of ${entries.length} lines`}
      </p>
    </div>
  );
}
