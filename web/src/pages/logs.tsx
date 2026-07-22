import { Check, Copy, Download, ScrollText } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { useLogs } from "@/lib/queries";
import type { LogLine, LogPage } from "@/lib/types";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { cn } from "@/lib/utils";

// No TRACE: the rotating file sink these lines are read from is opened at DEBUG
// (`configure_logging`), so TRACE entries never reach disk and the option could only ever show the
// same rows as DEBUG while implying something quieter was being hidden.
const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
type Level = (typeof LEVELS)[number];

const LIMIT = 1000;

/** Level → colour. Only the ones that mean "look at me" get a colour; the rest stay quiet so a
 *  screenful of DEBUG doesn't read as an emergency. */
const LEVEL_CLASS: Record<string, string> = {
  TRACE: "text-muted-foreground/70",
  DEBUG: "text-muted-foreground",
  INFO: "text-foreground",
  SUCCESS: "text-success",
  WARNING: "text-warning",
  ERROR: "text-destructive",
  CRITICAL: "text-destructive",
};

function LogRow({ line }: { line: LogLine }) {
  // The message can be multi-line (a folded traceback), so it wraps and preserves its own newlines
  // while the row as a whole never forces the page sideways.
  return (
    <div className="grid grid-cols-[auto_5.5rem_1fr] gap-x-3 px-3 py-1 odd:bg-muted/20">
      <span className="whitespace-nowrap text-muted-foreground/70">
        {line.ts?.slice(11) ?? ""}
      </span>
      <span
        className={cn(
          "font-medium",
          LEVEL_CLASS[line.level] ?? "text-muted-foreground",
        )}
      >
        {line.level}
      </span>
      <span className="min-w-0">
        <span className="whitespace-pre-wrap break-words text-foreground/90">
          {line.message}
        </span>
        <span className="ml-2 text-muted-foreground/50">{line.source}</span>
      </span>
    </div>
  );
}

function toPlainText(lines: LogLine[]): string {
  return lines
    .map((l) => `${l.ts ?? ""} | ${l.level} | ${l.source} - ${l.message}`)
    .join("\n");
}

export function LogsPage() {
  const [level, setLevel] = useState<Level>("INFO");
  // The next level DOWN, for the empty-state hint — suggesting a hardcoded "DEBUG" is useless
  // advice when you are already on it, and wrong advice when you are on TRACE-like breadth.
  const quieter = LEVELS[LEVELS.indexOf(level) - 1];
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const [copied, setCopied] = useState(false);
  const debouncedSearch = useDebouncedValue(search, 300);
  const query = useLogs(level, debouncedSearch, LIMIT, follow);
  const endRef = useRef<HTMLDivElement>(null);

  const lines = useMemo(() => query.data?.lines ?? [], [query.data]);

  // Follow the tail as new lines arrive, but never yank the page for reduced-motion users.
  useEffect(() => {
    if (!follow) return;
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    )?.matches;
    endRef.current?.scrollIntoView?.({
      block: "nearest",
      behavior: reduce ? "auto" : "smooth",
    });
  }, [lines.length, follow]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(toPlainText(lines));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard is unavailable over plain http or without permission; the button simply stays
      // put — the log text is on screen and the download still works.
    }
  };

  return (
    <div>
      <PageHeader
        icon={ScrollText}
        title="Logs"
        subtitle="What Shortlist has been doing, straight from this instance. Passwords, tokens and API keys are stripped out before anything reaches this page — so it's safe to copy into a bug report."
        actions={
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => void copy()}
              disabled={lines.length === 0}
            >
              {copied ? (
                <Check aria-hidden="true" />
              ) : (
                <Copy aria-hidden="true" />
              )}
              {copied ? "Copied" : "Copy"}
            </Button>
            <Button asChild variant="outline">
              <a href={api.logsDownloadUrl()} download>
                <Download aria-hidden="true" />
                Download .zip
              </a>
            </Button>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Segmented<Level>
          value={level}
          onChange={setLevel}
          ariaLabel="Minimum log level"
          options={LEVELS.map((value) => ({ value, label: value }))}
        />
        <Input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Filter lines…"
          aria-label="Filter log lines"
          className="h-9 w-full sm:w-64"
        />
        <label className="flex cursor-pointer items-center gap-2 text-sm text-muted-foreground">
          <Switch
            checked={follow}
            onCheckedChange={setFollow}
            aria-label="Follow new log lines"
          />
          Follow
        </label>
      </div>

      <QueryBoundary
        query={query}
        skeleton={<Skeleton className="h-96 w-full" />}
        isEmpty={(page: LogPage) => page.lines.length === 0}
        empty={
          <EmptyState
            title={search ? "Nothing matches that filter" : "No log lines yet"}
            hint={
              search
                ? `No ${level}-or-louder lines contain “${search}”.${quieter ? ` Try ${quieter}, or clear the filter.` : " Try clearing the filter."}`
                : `Nothing has been logged at ${level} or louder yet.${quieter ? ` Try ${quieter}, or run something first.` : " Run something first."}`
            }
          />
        }
      >
        {(page: LogPage) => (
          <div className="space-y-2">
            <div className="overflow-hidden rounded-xl border bg-background">
              <div
                className="max-h-[65vh] overflow-y-auto font-mono text-xs leading-relaxed"
                role="log"
                aria-label="Application logs"
              >
                {page.lines.map((line, i) => (
                  <LogRow key={`${line.ts}-${i}`} line={line} />
                ))}
                <div ref={endRef} />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              {page.truncated
                ? `Showing the newest ${page.lines.length} of ${page.total_matched} matching lines`
                : `${page.lines.length} ${page.lines.length === 1 ? "line" : "lines"}`}
              {page.file ? ` · ${page.file}` : ""} · the full history is in the
              download
            </p>
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
