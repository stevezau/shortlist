import { Check, Copy, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

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
const VIEWS = [
  { value: "all", label: "All", level: "INFO" },
  // Reads at INFO and keeps only the warnings and the lines around them, so a warning is seen next
  // to what happened just before it rather than as a line with no story.
  { value: "warnings", label: "Warnings", level: "INFO" },
  { value: "errors", label: "Errors", level: "ERROR" },
  // Last: the tabs run from the usual view to the most specific, and Debug is for digging.
  { value: "debug", label: "Debug", level: "DEBUG" },
] as const;
type View = (typeof VIEWS)[number]["value"];

/** The view that shows more than this one, for the empty state's "show me more" button. */
const MORE_VERBOSE: Record<View, View | null> = { errors: "warnings", warnings: "all", all: "debug", debug: null };

/** How many lines either side of a warning the Warnings view keeps. */
const CONTEXT = 2;

const isWarning = (line: LogLine) => ["WARNING", "ERROR", "CRITICAL"].includes(line.level);

/** The lines a view shows: everything the server returned, or for Warnings only the warnings and
 *  the lines within {@link CONTEXT} of one. */
function visibleLines(lines: LogLine[], view: View): LogLine[] {
  if (view !== "warnings") return lines;
  return lines.filter((_, i) =>
    lines.slice(Math.max(0, i - CONTEXT), i + CONTEXT + 1).some(isWarning),
  );
}

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
  const tint = line.level === "WARNING" ? "bg-warning/10" : isWarning(line) ? "bg-destructive/10" : "odd:bg-muted/20";
  // The message can be multi-line (a folded traceback), so it wraps and preserves its own newlines
  // while the row as a whole never forces the page sideways.
  return (
    // Three columns only where three columns fit. At 390 the timestamp (~85px) and the level
    // (5.5rem) plus gutters left the message ~137px, so every line wrapped five or six deep and
    // four log lines filled a phone screen. Below `sm` the stamp and level share one line and the
    // message gets the full width underneath.
    <div className={cn("px-3 py-1 sm:grid sm:grid-cols-[auto_5.5rem_1fr] sm:gap-x-3", tint)}>
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

/** The Log tab of the Activity page (it was the Logs page until the two merged). */
export function LogsPanel() {
  const [view, setView] = useState<View>("all");
  const { level } = VIEWS.find((option) => option.value === view) ?? VIEWS[0];
  // A hardcoded "Debug" would be a no-op when you are already on it, and the button has to
  // disappear rather than do nothing.
  const quieter = VIEWS.find((option) => option.value === MORE_VERBOSE[view]);
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const { state: copyState, copy } = useCopy();
  const debouncedSearch = useDebouncedValue(search, 300);
  const query = useLogs(level, debouncedSearch, LIMIT, follow);
  const paneRef = useRef<HTMLDivElement>(null);

  const lines = useMemo(() => visibleLines(query.data?.lines ?? [], view), [query.data, view]);

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
    // Optional-called: jsdom gives an element no `scrollTo`, and a test environment should not be
    // able to crash the page it is rendering.
    pane.scrollTo?.({
      top: pane.scrollHeight,
      behavior: "auto",
    });
  }, [lines.length, follow]);

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <p className="min-w-[16rem] flex-1 text-sm text-muted-foreground">
          Passwords, tokens and API keys are redacted. Review account names, titles and server
          addresses before sharing.
        </p>
        <div className="flex shrink-0 gap-2">
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
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Segmented<View>
          value={view}
          onChange={setView}
          ariaLabel="Show lines at this level or louder"
          options={VIEWS.map(({ value, label }) => ({ value, label }))}
        />
        <Input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Filter lines…"
          aria-label="Filter log lines"
          className="h-9 w-full sm:w-64"
        />
        {view === "warnings" && (
          <span className="text-sm text-muted-foreground sm:ml-auto">Warnings, with the lines around them</span>
        )}
        <label
          className={cn(
            "flex cursor-pointer items-center gap-2 text-sm text-muted-foreground",
            view !== "warnings" && "sm:ml-auto",
          )}
        >
          <Switch
            checked={follow}
            onCheckedChange={setFollow}
            aria-label="Follow new log lines"
          />
          Follow
        </label>
        {!follow && <Button variant="outline" size="sm" onClick={() => setFollow(true)}>Jump to latest</Button>}
      </div>

      <QueryBoundary
        query={query}
        skeleton={<Skeleton className="h-96 w-full" />}
        isEmpty={(page: LogPage) => visibleLines(page.lines, view).length === 0}
        // The hint states the fact; the remedies are buttons. They were prose ("Try DEBUG, or clear
        // the filter") naming two controls already on this page — an instruction to go and find
        // something, where the thing itself fits in the same space.
        empty={
          <EmptyState
            title={
              search ? "Nothing matches that filter" : view === "warnings" ? "No warnings" : "No log lines yet"
            }
            hint={
              search
                ? `No ${level}-or-louder lines contain “${search}”.`
                : view === "warnings"
                  ? "Nothing has logged a warning or an error recently."
                  : `Nothing has been logged at ${level} or louder yet.`
            }
            action={
              search || quieter ? (
                <div className="flex flex-wrap justify-center gap-2">
                  {search && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setSearch("")}
                    >
                      Clear filter
                    </Button>
                  )}
                  {quieter && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setView(quieter.value)}
                    >
                      Show {quieter.label} and louder
                    </Button>
                  )}
                </div>
              ) : undefined
            }
          />
        }
      >
        {(page: LogPage) => (
          <div className="space-y-2">
            <div className="overflow-hidden rounded-xl border bg-background">
              <div
                ref={paneRef}
                onScroll={(event) => {
                  const pane = event.currentTarget;
                  if (follow && pane.scrollHeight - pane.clientHeight - pane.scrollTop > 16) setFollow(false);
                }}
                className="max-h-[65vh] overflow-y-auto font-mono text-xs leading-relaxed"
                role="log"
                aria-label="Application logs"
              >
                {lines.map((line, i) => (
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
                : `${lines.length} ${lines.length === 1 ? "line" : "lines"}`}
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
