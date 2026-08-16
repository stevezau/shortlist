import { Check, Copy, ScrollText, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { DownloadButton } from "@/components/download-button";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { useLogs } from "@/lib/queries";
import type { LogLine, LogPage } from "@/lib/types";
import { useCopy } from "@/lib/use-copy";
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
  // /85 rather than /70: at /70 this measured 4.31:1 on the row background, under AA for 12px text.
  TRACE: "text-muted-foreground/85",
  DEBUG: "text-muted-foreground",
  INFO: "text-foreground",
  SUCCESS: "text-success",
  WARNING: "text-warning",
  ERROR: "text-destructive-text",
  CRITICAL: "text-destructive-text",
};

function LogRow({ line }: { line: LogLine }) {
  // The message can be multi-line (a folded traceback), so it wraps and preserves its own newlines
  // while the row as a whole never forces the page sideways.
  return (
    // Three columns only where three columns fit. At 390 the timestamp (~85px) and the level
    // (5.5rem) plus gutters left the message ~137px, so every line wrapped five or six deep and
    // four log lines filled a phone screen. Below `sm` the stamp and level share one line and the
    // message gets the full width underneath.
    <div className="px-3 py-1 odd:bg-muted/20 sm:grid sm:grid-cols-[auto_5.5rem_1fr] sm:gap-x-3">
      {/* Opacities here are set by measured contrast, not taste: /70 put the timestamp at 4.31:1 and
          /50 put the source ref at 2.78:1, both under AA at this 12px monospace size. */}
      <span className="mr-3 whitespace-nowrap text-muted-foreground/85 sm:mr-0">
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
      <span className="block min-w-0 sm:inline">
        <span className="whitespace-pre-wrap break-words text-foreground/90">
          {line.message}
        </span>
        {/* `break-words`: a dotted module path has no space to wrap at, so without it a long one
            pushes the pane into a sideways scroll on a narrow window. */}
        <span className="ml-2 break-words text-muted-foreground/75">
          {line.source}
        </span>
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
  const { state: copyState, copy } = useCopy();
  const debouncedSearch = useDebouncedValue(search, 300);
  const query = useLogs(level, debouncedSearch, LIMIT, follow);
  const paneRef = useRef<HTMLDivElement>(null);

  const lines = useMemo(() => query.data?.lines ?? [], [query.data]);

  // Follow the tail as new lines arrive, but never yank the page for reduced-motion users.
  //
  // Scrolls the PANE, not the element into view. `scrollIntoView` walks every scrollable ancestor
  // up to the document, so on a phone — where the pane's bottom is well below the fold — following
  // the tail scrolled the window too, and you arrived on the page already past its own heading,
  // mid-sentence in the subtitle. Moving one element's scrollTop keeps the effect inside the pane.
  useEffect(() => {
    if (!follow) return;
    const pane = paneRef.current;
    if (!pane) return;
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    )?.matches;
    // Optional-called: jsdom gives an element no `scrollTo`, and a test environment should not be
    // able to crash the page it is rendering.
    pane.scrollTo?.({
      top: pane.scrollHeight,
      behavior: reduce ? "auto" : "smooth",
    });
  }, [lines.length, follow]);

  return (
    <div>
      <PageHeader
        icon={ScrollText}
        title="Logs"
        subtitle="What Shortlist has been doing. Passwords, tokens and API keys are stripped out — safe to paste into a bug report."
        actions={
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => copy(toPlainText(lines))}
              disabled={lines.length === 0}
            >
              {copyState === "copied" ? (
                <Check aria-hidden="true" />
              ) : copyState === "error" ? (
                <TriangleAlert aria-hidden="true" />
              ) : (
                <Copy aria-hidden="true" />
              )}
              {copyState === "copied"
                ? "Copied"
                : copyState === "error"
                  ? "Couldn’t copy — try again"
                  : "Copy"}
            </Button>
            <DownloadButton
              url={api.logsDownloadUrl()}
              filename="shortlist-logs.zip"
            >
              Download .zip
            </DownloadButton>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Segmented<Level>
          value={level}
          onChange={setLevel}
          ariaLabel="Show lines at this level or louder"
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
                ref={paneRef}
                className="max-h-[65vh] overflow-y-auto font-mono text-xs leading-relaxed"
                role="log"
                aria-label="Application logs"
              >
                {page.lines.map((line, i) => (
                  <LogRow key={`${line.ts}-${i}`} line={line} />
                ))}
              </div>
            </div>
            {/* The recording-vs-showing point lives HERE rather than in a paragraph above the
                buttons. The file sink is opened at DEBUG whatever Settings → Advanced says (that
                control is named for the console, and the two read as one knob), so the level
                buttons filter what is shown and never what was kept — which is only worth saying
                next to the line that already distinguishes this view from the download. */}
            <p className="text-xs text-muted-foreground">
              {page.truncated
                ? `Showing the newest ${page.lines.length} of ${page.total_matched} matching lines`
                : `${page.lines.length} ${page.lines.length === 1 ? "line" : "lines"}`}
              {page.file ? ` · ${page.file}` : ""} · everything down to DEBUG is
              recorded whatever level you pick &mdash; the full history is in
              the download
            </p>
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
