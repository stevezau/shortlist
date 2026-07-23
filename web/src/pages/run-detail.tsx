import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Check,
  CircleSlash,
  Clock,
  Copy,
  Download,
  Info,
  Loader2,
  Search,
  Shuffle,
  Sparkles,
  Users,
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { PickList } from "@/components/pick-list";
import { provenanceLabel } from "@/lib/pick-provenance";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { StatTile } from "@/components/stat-tile";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  formatDate,
  formatDuration,
  runElapsedMs,
  runStatusLabel,
  runStatusVariant,
  triggerLabel,
} from "@/lib/format";
import { githubIssueSnippet } from "@/lib/github";
import { queryKeys, useCancelRun, useRun, useUsers } from "@/lib/queries";
import { mergeRunLog } from "@/lib/run-log";
import { countLabel, STAGE_LABELS } from "@/lib/run-stages";
import { useSSE } from "@/lib/sse";
import type {
  Pick,
  RunDetail,
  RunLibraryBreakdown,
  RunLogEntry,
  RunUserResult,
  RunUserStageEvent,
} from "@/lib/types";

/** A run's live activity log: seeded from the server buffer, topped up by the SSE stage stream. */
function ActivityLog({
  entries,
  running,
}: {
  entries: RunLogEntry[];
  running: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  // Follow the tail as new lines arrive, but don't yank the page for reduced-motion users.
  useEffect(() => {
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    )?.matches;
    endRef.current?.scrollIntoView?.({
      block: "nearest",
      behavior: reduce ? "auto" : "smooth",
    });
  }, [entries.length]);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-base">Activity</CardTitle>
        {running && (
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            live
          </span>
        )}
      </CardHeader>
      <CardContent>
        <div
          className="max-h-72 space-y-1 overflow-y-auto rounded-md bg-muted/40 p-3 font-mono text-xs"
          role="log"
          aria-live="polite"
          aria-label="Run activity log"
        >
          {entries.length === 0 ? (
            <p className="text-muted-foreground">
              {running ? "Starting…" : "No activity recorded for this run."}
            </p>
          ) : (
            entries.map((entry, i) => <LogLine key={i} entry={entry} />)
          )}
          <div ref={endRef} />
        </div>
      </CardContent>
    </Card>
  );
}

function LogLine({ entry }: { entry: RunLogEntry }) {
  const time = entry.ts ? new Date(entry.ts).toLocaleTimeString() : "";
  const label = STAGE_LABELS[entry.stage] ?? entry.stage;
  const detail = Object.entries(entry.counts ?? {})
    .map(([k, v]) => countLabel(k, Number(v)))
    .join(" · ");
  return (
    <div className="flex gap-2">
      {time && <span className="shrink-0 text-muted-foreground">{time}</span>}
      <span className="shrink-0 font-medium">{entry.user}</span>
      <span className="text-muted-foreground">
        {label}
        {detail ? ` · ${detail}` : ""}
        {/* A skip carries its reason here — this feed is the only place a SHARED row's outcome
            appears at all, since a shared row has no per-user panel. */}
        {entry.reason ? ` — ${entry.reason}` : ""}
      </span>
    </div>
  );
}

function CopyForGitHubButton({
  run,
  result,
}: {
  run: RunDetail;
  result: RunUserResult;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(githubIssueSnippet(run, result));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be unavailable (http, permissions); the button simply
      // stays in its default state — the error text itself is on screen.
    }
  };

  return (
    <Button variant="outline" size="sm" onClick={() => void copy()}>
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      {copied ? "Copied" : "Copy for GitHub issue"}
    </Button>
  );
}

/** A one-line, plain-English take on a raw engine/Plex error — the raw text stays available below. */
function friendlyError(raw: string): string {
  if (/\b50\d\b|internal_server_error/i.test(raw))
    return "Plex hit a server error (500) while writing this row — it was most likely overloaded. This usually clears on the next run.";
  if (/timed?\s?out|timeout/i.test(raw))
    return "Plex timed out while writing this row — it was busy. This usually clears on the next run.";
  if (/\b429\b|too many requests/i.test(raw))
    return "Plex was rate-limiting writes (429) — too many at once. This usually clears on the next run.";
  return "Something went wrong building this person’s row.";
}

/** A stable bucket key for an error, so identical failures (same 500 on different collection ids /
 *  rating keys) group together — used to surface "N people failed with the same problem". */
function errorBucket(raw: string): string {
  return friendlyError(raw);
}

/** Rank badge colour by tier — the top picks stand out, lower ones recede. */
function rankClass(rank: number): string {
  if (rank <= 3) return "text-amber-400";
  if (rank <= 10) return "text-foreground";
  return "text-muted-foreground";
}

/** One ranked pick: rank, a status dot (green = new this run), title + reason, and where it
 *  came from. */
function PickLine({ pick, isNew }: { pick: Pick; isNew: boolean }) {
  return (
    <li className="flex items-baseline gap-3 py-1.5">
      <span
        className={cn(
          "w-9 shrink-0 text-right text-sm font-semibold tabular-nums",
          rankClass(pick.rank),
        )}
      >
        #{pick.rank}
      </span>
      <span
        className={cn(
          "mt-1.5 h-2 w-2 shrink-0 rounded-full",
          isNew ? "bg-success" : "bg-muted-foreground/30",
        )}
        aria-label={isNew ? "new this run" : "kept"}
        title={isNew ? "New this run" : "Kept from last run"}
      />
      <span className="min-w-0 flex-1 text-sm">
        <span className="block truncate">
          <span className="font-medium">{pick.title}</span>
          {pick.reason && (
            <span className="text-muted-foreground"> — {pick.reason}</span>
          )}
        </span>
        {/* Where it came from. This page has its own pick renderer rather than using PickList, so
            the provenance line has to be repeated here — it is the page people open to ask exactly
            this question. */}
        {provenanceLabel(pick) && (
          <span className="block truncate text-xs text-muted-foreground/80">
            {provenanceLabel(pick)}
          </span>
        )}
      </span>
    </li>
  );
}

/** One library's ranked picks: first five, a show-all toggle, and a quiet "removed" footer. */
/** Plain-English names for the AI steps in an llm_tokens_by_step map. */
const STEP_LABELS: Record<string, string> = {
  curate: "final picks",
  llm_web: "web search",
  llm_library: "library scan",
};

/** " (final picks 12,340 · web search 4,100)" for a by-step token map, or "" when empty. */
function tokenStepSummary(byStep?: Record<string, number>): string {
  if (!byStep) return "";
  const parts = Object.entries(byStep)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([step, n]) => `${STEP_LABELS[step] ?? step} ${n.toLocaleString()}`);
  return parts.length ? ` (${parts.join(" · ")})` : "";
}

/** " · N Exa search(es)" when any ran, else "". Exa bills per search, so it's shown apart from tokens. */
function exaSummary(count?: number): string {
  if (!count) return "";
  return ` · ${count} Exa search${count === 1 ? "" : "es"}`;
}

/** "final picks 251,295 · web search 126,133" for a by-step map — same as tokenStepSummary, no parens. */
function tokenStepInline(byStep?: Record<string, number>): string {
  if (!byStep) return "";
  return Object.entries(byStep)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([step, n]) => `${STEP_LABELS[step] ?? step} ${n.toLocaleString()}`)
    .join(" · ");
}

/** Why a run failed for a reason that belongs to no single person — a share filter Plex refused, a
 *  sweep that could not run. The reason was always recorded, but lived only in `stats.error`, which
 *  nothing rendered: the page said "Failed" and left the operator reading container logs (issue #1). */
function RunFailureBanner({ run }: { run: RunDetail }) {
  const blockers = run.promotion_blockers ?? [];
  if (run.status !== "error" || (!run.error && blockers.length === 0))
    return null;
  return (
    <div
      role="alert"
      className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
    >
      <p className="font-medium text-foreground">
        {blockers.length > 0
          ? "Nothing was promoted — Plex wouldn’t accept a share filter"
          : "This run didn’t finish cleanly"}
      </p>
      {blockers.length > 0 && (
        <p className="text-muted-foreground">
          Rows are only put on Home once every other account is set to hide
          them. Plex refused that change for{" "}
          {blockers.length === 1
            ? "this account"
            : `${blockers.length} accounts`}
          , so the rows were built but deliberately left hidden rather than risk
          showing one person’s row to someone else.
        </p>
      )}
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-background/60 p-2.5 font-mono text-xs text-destructive">
        {blockers.length > 0 ? blockers.join("\n") : run.error}
      </pre>
    </div>
  );
}

/** The finished-run stats as at-a-glance tiles (Dashboard style) rather than one dense text line. */
function RunStatTiles({ run }: { run: RunDetail }) {
  const s = run.stats;
  const elapsed = runElapsedMs(run.started_at, run.finished_at);
  const failed = s.users_error ?? 0;
  // Skipped is neither a success nor a failure — a run where everyone was skipped used to read
  // "3 · all succeeded" above three rows badged "Skipped".
  const skipped = s.users_skipped ?? 0;
  const requested = s.titles_requested ?? 0;
  const tokens = s.llm_tokens ?? 0;
  const exa = s.exa_searches ?? 0;
  const exaCacheHits = s.exa_cache_hits ?? 0;
  // "web search 467,463 · final picks 52,625 tokens" — the trailing unit makes clear these are token
  // counts, not the (separate) Exa search count shown in its own tile below.
  const stepInline = tokenStepInline(s.llm_tokens_by_step);
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile
        icon={Clock}
        label="Duration"
        value={elapsed != null ? formatDuration(elapsed) : "—"}
        hint="start → finish"
      />
      <StatTile
        icon={Users}
        label="People"
        value={s.users_ok ?? 0}
        hint={
          failed > 0
            ? `${failed} failed${skipped > 0 ? `, ${skipped} skipped` : ""}`
            : skipped > 0
              ? `${skipped} skipped, built nothing`
              : // Everyone can succeed while the RUN fails (a refused share filter belongs to no
                // person) — "all succeeded" under a "Failed" badge is how that looked before.
                run.status === "error"
                ? "built, but not promoted"
                : "all succeeded"
        }
        tone={
          failed > 0
            ? "destructive"
            : skipped > 0 || run.status === "error"
              ? "warning"
              : "success"
        }
      />
      <StatTile
        icon={Shuffle}
        label="Titles changed"
        value={`+${s.titles_added ?? 0}/−${s.titles_removed ?? 0}`}
        hint="added / rotated out"
      />
      <StatTile
        icon={Download}
        label="Requested"
        value={requested}
        hint="to Sonarr / Radarr"
      />
      {tokens > 0 && (
        <StatTile
          icon={Sparkles}
          label="AI tokens"
          value={tokens.toLocaleString()}
          hint={stepInline ? `${stepInline} tokens` : "curate + AI sources"}
          title="Total AI tokens this run cost, split by what the AI did. Turn AI sources off in Settings → Recommendations to lower it."
        />
      )}
      {(exa > 0 || exaCacheHits > 0) && (
        <StatTile
          icon={Search}
          label="Exa searches"
          value={exa}
          // A warm cache means most lookups aren't billed — showing only the billed "1" made a
          // fully-cached run look like the source did nothing. The hint names what the cache served.
          hint={
            exaCacheHits > 0
              ? `billed · ${exaCacheHits.toLocaleString()} from cache`
              : "web lookups · billed per search"
          }
          title="Billable Exa web-search requests this run made — a count, not tokens. Exa bills per search; results are cached for two weeks and shared across everyone, so most lookups are served from cache and cost nothing."
        />
      )}
    </div>
  );
}

function LibraryPicks({ entry }: { entry: RunLibraryBreakdown }) {
  const [expanded, setExpanded] = useState(false);
  const added = new Set(entry.added);
  const shown = expanded ? entry.picks : entry.picks.slice(0, 5);
  return (
    <div className="space-y-2">
      {entry.picks.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No picks in this library.
        </p>
      ) : (
        <ul className="divide-y divide-border/40">
          {shown.map((pick) => (
            <PickLine
              key={pick.rank}
              pick={pick}
              isNew={added.has(pick.title)}
            />
          ))}
        </ul>
      )}
      {entry.picks.length > 5 && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Show fewer" : `Show all ${entry.picks.length}`}
        </Button>
      )}
      {entry.removed.length > 0 && (
        <p className="pt-1 text-xs text-muted-foreground">
          <span className="font-medium text-foreground/70">
            −{entry.removed.length} rotated out
          </span>{" "}
          — the row keeps its size, so these made room for the new picks above:{" "}
          <span className="line-through">{entry.removed.join(", ")}</span>
        </p>
      )}
      {entry.deleted.length > 0 && (
        <p className="text-xs font-medium text-destructive">
          Row deleted (this person no longer gets this row):{" "}
          {entry.deleted.join(", ")}
        </p>
      )}
    </div>
  );
}

/** One row (its libraries as tabs when there's more than one), showing the selected library's picks. */
function RowSection({ entries }: { entries: RunLibraryBreakdown[] }) {
  const [libKey, setLibKey] = useState(entries[0]?.library_key ?? "");
  const active =
    entries.find((entry) => entry.library_key === libKey) ?? entries[0];
  // Title, new-count and tokens all follow the SELECTED library, so a `{library_name}` row title
  // renders for the tab you're viewing (e.g. "Movies Picked for You" ↔ "TV Shows Picked for You")
  // instead of being stuck on the first library's rendering.
  const added = active?.added.length ?? 0;
  const rowTokens = active?.llm_tokens ?? 0;
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">{active?.row_title}</h3>
        {added > 0 && (
          <span className="text-xs text-success">+{added} new</span>
        )}
        {rowTokens > 0 && (
          <span
            className="text-xs text-muted-foreground"
            title="AI tokens the curator spent choosing this row's picks."
          >
            {rowTokens.toLocaleString()} AI tokens
          </span>
        )}
      </div>
      {entries.length > 1 && (
        <Segmented
          value={libKey}
          onChange={setLibKey}
          ariaLabel="Library"
          options={entries.map((entry) => ({
            value: entry.library_key,
            label: `${entry.library_title} · ${entry.picks.length}`,
          }))}
        />
      )}
      {active && <LibraryPicks entry={active} />}
    </div>
  );
}

/** A key for the run results — what the dots and the strikethrough mean — so the view reads without
 *  hovering to guess. Shown once above a person's rows. */
function ResultsLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <span className="font-medium text-foreground/70">What changed:</span>
      <span className="inline-flex items-center gap-1.5">
        <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
        New this run
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span
          className="h-2 w-2 rounded-full bg-muted-foreground/30"
          aria-hidden="true"
        />
        Kept from last run
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="line-through">Title</span>
        Rotated out for variety
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="font-semibold tabular-nums text-amber-400">#1–3</span>
        Top picks
      </span>
    </div>
  );
}

/** The selected user's result: an error, or their rows grouped from the per-(row, library) breakdown. */
function UserPanel({ run, result }: { run: RunDetail; result: RunUserResult }) {
  if (result.error !== null) {
    return (
      <div role="alert" className="space-y-3 rounded-md bg-destructive/10 p-3">
        <p className="text-sm font-medium text-foreground">
          {friendlyError(result.error)}
        </p>
        {/* Raw detail is contained: it scrolls inside its own box and wraps long tokens (the encoded
            Plex uri) so it can never push the page sideways. */}
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-background/60 p-2.5 font-mono text-xs text-destructive">
          {result.error}
        </pre>
        <CopyForGitHubButton run={run} result={result} />
      </div>
    );
  }
  // A skip is a configuration outcome, not a failure — so it explains itself rather than sitting
  // on "Working on this person…" forever, which is how it read to the beta user who filed issue #3.
  if (result.status === "skipped") {
    return (
      <div className="flex gap-3 rounded-md bg-muted/40 p-3 text-sm">
        <CircleSlash
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <div>
          <p className="font-medium text-foreground">
            Nothing to build for this person
          </p>
          <p className="mt-1 text-muted-foreground">
            {result.reason ??
              "No row was due for them in this run. Check that a per-person row is enabled and that they’re in its audience."}
          </p>
        </div>
      </div>
    );
  }
  if (result.breakdown.length === 0) {
    // Still running (this user hasn't finished) or a legacy run with no breakdown.
    if (result.picks.length > 0)
      return <PickList picks={result.picks} className="mt-1" />;
    return (
      <p className="text-sm text-muted-foreground">
        {result.status === "ok" || result.status === "cold_start"
          ? "No changes — this person’s rows were already up to date."
          : "Working on this person…"}
      </p>
    );
  }
  const rows = new Map<string, RunLibraryBreakdown[]>();
  for (const entry of result.breakdown) {
    rows.set(entry.row_slug, [...(rows.get(entry.row_slug) ?? []), entry]);
  }
  return (
    <div className="space-y-6">
      <ResultsLegend />
      {[...rows.values()].map((entries) => (
        <RowSection key={entries[0]?.row_slug} entries={entries} />
      ))}
    </div>
  );
}

/** One person as a full-width list row — far more scannable at 48 users than a wall of pills:
 *  name on the left, status/duration on the right, selected row highlighted. */
function UserRow({
  result,
  selected,
  onSelect,
}: {
  result: RunUserResult;
  selected: string;
  onSelect: (slug: string) => void;
}) {
  const failed = result.error !== null;
  const isSelected = result.slug === selected;
  return (
    <button
      type="button"
      role="tab"
      aria-selected={isSelected}
      onClick={() => onSelect(result.slug)}
      className={cn(
        "flex w-full items-center gap-3 border-l-2 px-3 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
        failed ? "border-l-destructive/70" : "border-l-transparent",
        isSelected ? "bg-primary/10" : "hover:bg-muted/60",
      )}
    >
      <UserAvatar name={result.username} size="sm" />
      <span className="min-w-0 flex-1 truncate font-medium">
        {result.display_name || result.username}
      </span>
      {failed ? (
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-destructive">
          <AlertCircle className="h-3.5 w-3.5" aria-hidden="true" />
          Failed
        </span>
      ) : result.status === "skipped" ? (
        // A green tick on someone who built nothing is the row-level version of "all succeeded".
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          Skipped
          <CircleSlash className="h-3.5 w-3.5" aria-hidden="true" />
        </span>
      ) : (
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          {formatDuration(result.duration_ms)}
          <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
        </span>
      )}
    </button>
  );
}

/** The user nav at the top of a run. At 48 users a flat grid is a wall, so: a one-line summary,
 *  failures always up front, the (usually many) successes tucked behind a toggle, and a search box. */
function UserTabs({
  results,
  selected,
  onSelect,
}: {
  results: RunUserResult[];
  selected: string;
  onSelect: (slug: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "failed" | "ok">("all");
  const q = query.trim().toLowerCase();
  const failedTotal = results.filter((r) => r.error !== null).length;
  const skippedTotal = results.filter(
    (r) => r.error === null && r.status === "skipped",
  ).length;
  const okTotal = results.length - failedTotal - skippedTotal;
  // Three outcomes, so classify by all three EVERYWHERE. Grouping on `error === null` alone put
  // skipped people under a "Succeeded" heading while their own row said "Skipped" — the same
  // contradiction the stat tile had, surviving one level down.
  const isSkipped = (r: RunUserResult) =>
    r.error === null && r.status === "skipped";
  const isOk = (r: RunUserResult) => r.error === null && !isSkipped(r);
  const mixed = failedTotal > 0 && okTotal + skippedTotal > 0; // a filter only helps when there's a mix
  const byStatus =
    !mixed || filter === "all"
      ? results
      : results.filter((r) =>
          filter === "failed" ? r.error !== null : !r.error,
        );
  const shown = q
    ? byStatus.filter(
        (r) =>
          r.username.toLowerCase().includes(q) ||
          (r.display_name ?? "").toLowerCase().includes(q),
      )
    : byStatus;
  const failed = shown.filter((r) => r.error !== null);
  const ok = shown.filter(isOk);
  const skipped = shown.filter(isSkipped);
  const many = results.length > 10;
  // Show group labels only when more than one group is on screen — otherwise the summary says it.
  const bothGroups =
    [failed.length, ok.length, skipped.length].filter(Boolean).length > 1;

  return (
    <div className="space-y-3" role="tablist" aria-label="Users in this run">
      <div className="space-y-2">
        {mixed ? (
          <Segmented<"all" | "failed" | "ok">
            value={filter}
            onChange={setFilter}
            ariaLabel="Filter people by status"
            options={[
              { value: "all", label: `All ${results.length}` },
              { value: "failed", label: `Failed ${failedTotal}` },
              { value: "ok", label: `OK ${okTotal + skippedTotal}` },
            ]}
          />
        ) : (
          <p className="text-sm text-muted-foreground">
            {failedTotal > 0 ? (
              <span className="font-medium text-destructive">
                {failedTotal} failed
              </span>
            ) : okTotal === 0 && skippedTotal > 0 ? (
              `${skippedTotal} skipped — nothing was built`
            ) : (
              `${okTotal} succeeded${skippedTotal > 0 ? `, ${skippedTotal} skipped` : ""}`
            )}
          </p>
        )}
        {many && (
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a person…"
            className="h-8 w-full rounded-md border bg-background px-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="Search users in this run"
          />
        )}
      </div>

      {/* One scannable, scrollable list — failures first, so a partly-failed run opens on what you
          came for. A vertical list reads far better than a wrapped grid of 48 near-identical pills. */}
      <div className="overflow-hidden rounded-lg border">
        <div className="max-h-96 divide-y divide-border/50 overflow-y-auto">
          {bothGroups && <GroupLabel>Failed · {failed.length}</GroupLabel>}
          {failed.map((result) => (
            <UserRow
              key={result.slug}
              result={result}
              selected={selected}
              onSelect={onSelect}
            />
          ))}
          {bothGroups && ok.length > 0 && (
            <GroupLabel>Succeeded · {ok.length}</GroupLabel>
          )}
          {ok.map((result) => (
            <UserRow
              key={result.slug}
              result={result}
              selected={selected}
              onSelect={onSelect}
            />
          ))}
          {bothGroups && skipped.length > 0 && (
            <GroupLabel>Skipped · {skipped.length}</GroupLabel>
          )}
          {skipped.map((result) => (
            <UserRow
              key={result.slug}
              result={result}
              selected={selected}
              onSelect={onSelect}
            />
          ))}
          {shown.length === 0 && (
            <p className="px-3 py-8 text-center text-sm text-muted-foreground">
              No one matches “{query}”.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

/** A sticky section header inside the scrollable user list. */
function GroupLabel({ children }: { children: ReactNode }) {
  return (
    <p className="sticky top-0 z-10 bg-muted/90 px-3 py-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground backdrop-blur-sm">
      {children}
    </p>
  );
}

export function RunDetailPage() {
  const { id } = useParams();
  const runId = Number(id);
  const runQuery = useRun(runId, Number.isFinite(runId));
  const usersQuery = useUsers();
  const queryClient = useQueryClient();
  const cancel = useCancelRun();

  // Run results carry slug/username but no user id, so map slug → id to deep-link each result to
  // its user page. Users removed from Plex since the run won't be in the map — those stay plain text.
  const idBySlug = new Map(
    (usersQuery.data ?? []).map((user) => [user.slug, user.id]),
  );

  // The activity log: seed from the server's in-memory buffer, then top it up live from the SSE
  // stage stream. Held in a ref+state so appends don't depend on stale closures.
  const logQuery = useQuery({
    queryKey: ["run-log", runId],
    queryFn: () => api.getRunLog(runId),
    enabled: Number.isFinite(runId),
  });
  const [liveLog, setLiveLog] = useState<RunLogEntry[]>([]);
  // Seed from the server snapshot; mergeRunLog dedups, so re-merging the same data is a no-op and an
  // event captured by BOTH the snapshot and the live stream is never doubled.
  useEffect(() => {
    if (logQuery.data) {
      setLiveLog((prev) => mergeRunLog(prev, logQuery.data, runId));
    }
  }, [logQuery.data, runId]);

  const appendStage = useCallback(
    (event: RunUserStageEvent) => {
      setLiveLog((prev) => mergeRunLog(prev, [event], runId));
    },
    [runId],
  );

  // Keep an in-flight run's page live: refetch on every stage/finish event, and append the stage to
  // the activity log so it scrolls in real time.
  useSSE({
    onRunUserStage: (event) => {
      appendStage(event);
      void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
    },
    onRunFinished: (event) => {
      if (event.run_id === runId) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      }
    },
  });

  // Which user's rows are on screen. Default to the first FAILED user (what you opened the page to
  // see), else the first user; keep the current pick as long as they're still in the run.
  const [selectedSlug, setSelectedSlug] = useState("");
  useEffect(() => {
    const users = runQuery.data?.users ?? [];
    const first = users[0];
    if (first && !users.some((u) => u.slug === selectedSlug)) {
      setSelectedSlug((users.find((u) => u.error !== null) ?? first).slug);
    }
  }, [runQuery.data, selectedSlug]);

  return (
    <div className="space-y-6">
      <BackLink to="/runs" label="All runs" />

      {!Number.isFinite(runId) ? (
        <EmptyState
          title="That run doesn’t exist"
          hint="The link may be wrong or the run was removed."
          action={
            <Button asChild variant="outline">
              <Link to="/runs">Back to all runs</Link>
            </Button>
          }
        />
      ) : (
        <QueryBoundary
          query={runQuery}
          skeleton={<Skeleton className="h-64 w-full" />}
        >
          {(run) => (
            <div className="space-y-6">
              <header className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="text-2xl font-semibold tracking-tight">
                    Run #{run.id}
                  </h1>
                  <Badge variant={runStatusVariant(run.status)}>
                    {runStatusLabel(run.status)}
                  </Badge>
                  {run.dry_run && (
                    <Badge variant="outline">
                      Test run — nothing was written to Plex
                    </Badge>
                  )}
                  {!run.finished_at && (
                    <Button
                      variant="destructive"
                      size="sm"
                      className="ml-auto"
                      loading={cancel.isPending}
                      disabled={cancel.isPending || cancel.isSuccess}
                      onClick={() => cancel.mutate(run.id)}
                      title="Stop this run. It finishes the person it's on, then stops — everyone already done stays."
                    >
                      {cancel.isSuccess ? "Stopping…" : "Cancel run"}
                    </Button>
                  )}
                </div>
                {/* A slim provenance line; the numbers moved into the tiles below so they read at a glance. */}
                <p className="text-sm text-muted-foreground">
                  {triggerLabel(run.trigger)} · started{" "}
                  {formatDate(run.started_at)}
                  {run.finished_at
                    ? ` · finished ${formatDate(run.finished_at)}`
                    : " · still running"}
                </p>
              </header>

              <RunFailureBanner run={run} />

              {/* Stats are only finalized once a run ends; while it's live we show the why-slow note instead. */}
              {run.finished_at && <RunStatTiles run={run} />}

              {!run.finished_at && (
                <div className="flex gap-3 rounded-lg border bg-muted/40 p-4 text-sm">
                  <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                  <div className="space-y-1">
                    <p className="font-medium">
                      Why a refresh can take a while
                    </p>
                    <p className="text-muted-foreground">
                      Building everyone's rows means updating Plex one change at
                      a time — Plex only accepts them one-by-one, and it's
                      especially slow to update TV Shows. A big refresh across
                      all users can take a while. It's much quicker after the
                      first run, when most rows only need small tweaks.
                    </p>
                  </div>
                </div>
              )}

              {run.users.length === 0 ? (
                <EmptyState
                  title={
                    run.finished_at
                      ? "No per-user results"
                      : "Working — results appear as each user finishes"
                  }
                  hint={
                    run.finished_at
                      ? "This run didn't process any users."
                      : "Each user's picks land here when they finish; the activity log below shows live progress."
                  }
                />
              ) : (
                (() => {
                  // Failures first in the nav, so a partly-failed run opens on the error you came for.
                  const ordered = [...run.users].sort(
                    (a, b) =>
                      Number(b.error !== null) - Number(a.error !== null),
                  );
                  const selected =
                    run.users.find((u) => u.slug === selectedSlug) ??
                    ordered[0];
                  if (!selected) return null;
                  const failed = selected.error !== null;
                  const userId = idBySlug.get(selected.slug) ?? null;
                  // When many people failed the same way (e.g. one Plex outage), say it ONCE up top so
                  // you don't click through 47 identical errors.
                  const buckets = new Map<string, number>();
                  for (const u of run.users)
                    if (u.error)
                      buckets.set(
                        errorBucket(u.error),
                        (buckets.get(errorBucket(u.error)) ?? 0) + 1,
                      );
                  const topError = [...buckets.entries()].sort(
                    (a, b) => b[1] - a[1],
                  )[0];
                  const commonError =
                    topError && topError[1] >= 2
                      ? { msg: topError[0], count: topError[1] }
                      : null;
                  return (
                    <div className="space-y-4">
                      {commonError && (
                        <div
                          role="alert"
                          className="flex gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
                        >
                          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                          <p>
                            <span className="font-medium">
                              {commonError.count} people failed with the same
                              problem.
                            </span>{" "}
                            {commonError.msg} Open any person below for the raw
                            details.
                          </p>
                        </div>
                      )}
                      <div
                        className={cn(
                          run.users.length > 1 &&
                            "grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)] lg:items-start",
                        )}
                      >
                        {run.users.length > 1 && (
                          <div className="lg:sticky lg:top-4">
                            <UserTabs
                              results={ordered}
                              selected={selected.slug}
                              onSelect={setSelectedSlug}
                            />
                          </div>
                        )}
                        <Card
                          className={cn(
                            "min-w-0",
                            failed && "border-destructive/50",
                          )}
                        >
                          <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
                            <CardTitle className="flex items-center gap-2.5">
                              <UserAvatar name={selected.username} size="sm" />
                              {userId !== null ? (
                                <Link
                                  to={`/users/${userId}`}
                                  className="rounded-sm hover:text-primary hover:underline"
                                >
                                  {selected.display_name || selected.username}
                                </Link>
                              ) : (
                                selected.display_name || selected.username
                              )}
                              <Badge
                                variant={
                                  failed
                                    ? "destructive"
                                    : runStatusVariant(selected.status)
                                }
                              >
                                {runStatusLabel(selected.status)}
                              </Badge>
                            </CardTitle>
                            <p className="text-sm text-muted-foreground">
                              {formatDuration(selected.duration_ms)}
                              {selected.llm_tokens > 0
                                ? ` · ${selected.llm_tokens.toLocaleString()} AI tokens${tokenStepSummary(
                                    selected.llm_tokens_by_step,
                                  )}`
                                : ""}
                              {exaSummary(selected.exa_searches)}
                            </p>
                          </CardHeader>
                          <CardContent>
                            <UserPanel run={run} result={selected} />
                          </CardContent>
                        </Card>
                      </div>
                    </div>
                  );
                })()
              )}

              {(liveLog.length > 0 || !run.finished_at) && (
                <ActivityLog entries={liveLog} running={!run.finished_at} />
              )}
            </div>
          )}
        </QueryBoundary>
      )}
    </div>
  );
}
