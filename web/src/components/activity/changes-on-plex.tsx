import { FileDiff, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";

import { EmptyState, ErrorState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  changeFailed,
  changeMode,
  changeRunId,
  describeChange,
  isNoOpChange,
  jobLabel,
  type ChangeDescription,
  type ChangeMode,
  type NameLookup,
} from "@/lib/describe-change";
import { triggerLabel } from "@/lib/format";
import {
  useCollections,
  usePlexChanges,
  useRuns,
  useSettings,
  useUsers,
} from "@/lib/queries";
import { rowDisplayName } from "@/lib/run-rows";
import type { AuditEvent, Run } from "@/lib/types";
import { personName } from "@/lib/user-names";

type ModeFilter = "all" | "real" | "dry";

interface ChangeLine {
  event: AuditEvent;
  description: ChangeDescription;
  mode: ChangeMode;
  failed: boolean;
}

/** Consecutive events that belong together: one run, one job's pass on one day, or edits made in the app. */
interface ChangeGroup {
  key: string;
  label: string;
  facts: string[];
  lines: ChangeLine[];
  /** People whose row this run left exactly as it was — counted, not listed (see `isNoOpChange`). */
  unchanged: number;
}

function localDay(iso: string): string {
  const date = new Date(iso);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function longDate(iso: string, withTime = true): string {
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}

function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function peopleIn(run: Run): number {
  return run.stats.users_ok + run.stats.users_error + (run.stats.users_skipped ?? 0);
}

function groupKey(event: AuditEvent): string {
  const runId = changeRunId(event);
  if (runId !== null) return `run:${runId}`;
  const job = typeof event.message.job === "string" ? event.message.job : "";
  return job ? `job:${job}:${localDay(event.ts)}` : `app:${localDay(event.ts)}`;
}

/** The header line's words for one group, from the run itself when the runs list still has it. */
function groupHeading(first: AuditEvent, events: AuditEvent[], runs: Map<number, Run>): { label: string; facts: string[] } {
  const runId = changeRunId(first);
  if (runId !== null) {
    const run = runs.get(runId);
    const dry = run ? run.dry_run : events.every((e) => e.message.dry_run === true);
    const facts = run
      ? [longDate(run.started_at), triggerLabel(run.trigger), `${peopleIn(run)} ${peopleIn(run) === 1 ? "person" : "people"}`]
      : [longDate(first.ts)];
    if (dry) facts.push("dry run · nothing written");
    return { label: `Run #${runId}`, facts };
  }
  const job = typeof first.message.job === "string" ? first.message.job : "";
  if (job) return { label: `Job: ${jobLabel(job)}`, facts: [longDate(first.ts, false)] };
  return { label: "Outside a run", facts: [longDate(first.ts, false)] };
}

function groupChanges(events: AuditEvent[], runs: Map<number, Run>, names: NameLookup): ChangeGroup[] {
  const buckets: { key: string; events: AuditEvent[] }[] = [];
  for (const event of events) {
    const key = groupKey(event);
    const last = buckets[buckets.length - 1];
    if (last && last.key === key) last.events.push(event);
    else buckets.push({ key, events: [event] });
  }
  return buckets.map(({ key, events: bucket }, index) => {
    const first = bucket[0] as AuditEvent;
    const lines = bucket
      .filter((event) => !isNoOpChange(event))
      .map((event) => ({
        event,
        description: describeChange(event, names),
        mode: changeMode(event),
        failed: changeFailed(event),
      }));
    return {
      // The index keeps keys unique when one run's events are split by another writer's in between.
      key: `${key}:${index}`,
      ...groupHeading(first, bucket, runs),
      lines,
      unchanged: bucket.length - lines.length,
    };
  });
}

/** `change` wraps filter rules in backticks; they are set in the code face, ligatures off for `!=`. */
function ChangeText({ text }: { text: string }) {
  return (
    <>
      {text.split("`").map((part, i) =>
        i % 2 === 1 ? (
          <code
            key={i}
            className="break-all font-mono text-[0.8125rem] text-accent-foreground [font-feature-settings:'liga'_0,'calt'_0] [font-variant-ligatures:none]"
          >
            {part}
          </code>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

function ModePill({ mode }: { mode: ChangeMode }) {
  // Real is the normal case, so only the exceptions get a badge; the word stays for screen readers.
  if (mode === "real") return <span className="sr-only">Real</span>;
  if (mode === "dry") {
    return (
      <Badge variant="outline" className="border-dashed text-muted-foreground">
        Dry run
      </Badge>
    );
  }
  if (mode === "setting") {
    return (
      <Badge variant="secondary" title="Only Shortlist's own setting changed; nothing was written to Plex.">
        Setting only
      </Badge>
    );
  }
  return (
    <span className="text-muted-foreground" title="This kind of change doesn't record whether it was a dry run.">
      <span aria-hidden="true">—</span>
      <span className="sr-only">Dry run not recorded</span>
    </span>
  );
}

function RetentionNote({ months }: { months: number | undefined }) {
  if (months === undefined) return null;
  return (
    <p className="text-sm text-muted-foreground">
      {months > 0 ? `Kept for ${months} ${months === 1 ? "month" : "months"}.` : "Kept forever."}{" "}
      <Link to="/settings#advanced" className="text-accent-foreground underline underline-offset-4">
        Change in Settings → System
      </Link>
    </p>
  );
}

/**
 * The Activity page's "Changes on Plex" tab: every write Shortlist made to Plex or plex.tv, real or
 * dry run, newest first — the audit trail plex-safety rule 10 keeps, finally with a screen.
 *
 * Grouped by the run that made each change (the run's own date, trigger and head count, when the runs
 * list still has it); a change made by a job or an edit in the app is grouped by day. The sentence comes
 * from `describeChange`; "Diff" shows the event exactly as it was stored, so nothing is ever only
 * available as our paraphrase of it.
 */
export function ChangesOnPlex() {
  const changes = usePlexChanges();
  const runsQuery = useRuns();
  const settings = useSettings();
  const collections = useCollections();
  const users = useUsers();
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState<ModeFilter>("all");
  const [opened, setOpened] = useState<ChangeLine | null>(null);

  const names = useMemo<NameLookup>(() => {
    const rows = new Map((collections.data ?? []).map((c) => [c.slug, rowDisplayName(c.name || c.fallback_name)]));
    const people = new Map((users.data ?? []).map((u) => [u.slug, personName(u)]));
    return { row: (slug) => rows.get(slug) || undefined, person: (slug) => people.get(slug) || undefined };
  }, [collections.data, users.data]);
  const runs = useMemo(() => new Map((runsQuery.data ?? []).map((run) => [run.id, run])), [runsQuery.data]);
  const events = useMemo(() => changes.data?.pages.flat() ?? [], [changes.data]);
  const groups = useMemo(() => groupChanges(events, runs, names), [events, runs, names]);

  const needle = search.trim().toLocaleLowerCase();
  const filtering = needle !== "" || mode !== "all";
  const matches = (line: ChangeLine) =>
    (mode === "all" || line.mode === mode) &&
    (!needle ||
      `${line.description.who} ${line.description.what} ${line.description.change} ${line.event.scope}`
        .toLocaleLowerCase()
        .includes(needle));
  const shown = groups
    .map((group) => ({ ...group, lines: group.lines.filter(matches) }))
    .filter((group) => group.lines.length > 0 || (!filtering && group.unchanged > 0));
  const shownCount = shown.reduce((sum, group) => sum + group.lines.length, 0);
  const totalCount = groups.reduce((sum, group) => sum + group.lines.length, 0);
  const retention = settings.data?.["events.retention"];

  return (
    <div className="space-y-4">
      <p className="max-w-[90ch] text-sm leading-relaxed text-muted-foreground">
        Every write to Plex and plex.tv, real or dry run, newest first. On the Jobs tab, the jobs that
        make these writes are marked <Badge className="align-[1px]">Changes Plex</Badge> or{" "}
        <Badge className="align-[1px]">Can delete</Badge>.
      </p>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex w-full flex-wrap items-center gap-3 sm:w-auto">
          <div className="relative w-full sm:w-72">
            <Search aria-hidden="true" className="absolute left-3 top-2.5 size-4 text-faint-foreground" />
            <Input
              type="search"
              aria-label="Filter changes"
              placeholder="Filter by person, row or account…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className="pl-9"
            />
          </div>
          <Segmented<ModeFilter>
            ariaLabel="Show"
            value={mode}
            onChange={setMode}
            options={[
              { value: "all", label: "All" },
              { value: "real", label: "Real" },
              { value: "dry", label: "Dry run" },
            ]}
          />
        </div>
        <RetentionNote months={typeof retention === "number" ? retention : settings.data ? 0 : undefined} />
      </div>

      {changes.isPending ? (
        <Skeleton className="h-72 w-full" />
      ) : changes.isError ? (
        <ErrorState error={changes.error} onRetry={() => void changes.refetch()} />
      ) : events.length === 0 ? (
        <EmptyState
          title="Nothing has changed on Plex yet"
          hint="Every write a run, a job or one of your edits makes to Plex or plex.tv is recorded here, real or dry run."
        />
      ) : shown.length === 0 ? (
        <EmptyState
          title="No changes match"
          hint="Nothing loaded here matches that filter. Older changes may still match: load them below."
          action={
            <Button
              variant="outline"
              onClick={() => {
                setSearch("");
                setMode("all");
              }}
            >
              Clear filters
            </Button>
          }
        />
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card">
          <Table>
            <TableHeader className="hidden md:table-header-group">
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-28 pl-4">Time</TableHead>
                <TableHead>Who / what</TableHead>
                <TableHead>Change</TableHead>
                <TableHead className="w-28">Mode</TableHead>
                <TableHead className="w-20 pr-4">
                  <span className="sr-only">Raw change</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="grid md:table-row-group">
              {shown.map((group) => [
                <TableRow key={group.key} className="block bg-muted/30 hover:bg-muted/30 md:table-row">
                  <TableCell colSpan={5} className="block px-4 py-2 text-sm text-muted-foreground md:table-cell">
                    <span className="font-semibold text-foreground">{group.label}</span>
                    {[...group.facts, ...(group.unchanged > 0 ? [`${group.unchanged} unchanged`] : [])].map((fact) => (
                      <span key={fact}> · {fact}</span>
                    ))}
                  </TableCell>
                </TableRow>,
                ...group.lines.map((line) => (
                  <TableRow
                    key={line.event.id}
                    className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 px-4 py-3 md:table-row md:p-0 [&>td]:p-0 md:[&>td]:px-3 md:[&>td]:py-3"
                  >
                    <TableCell className="whitespace-nowrap text-muted-foreground tabular-nums md:pl-4">
                      <time dateTime={line.event.ts}>{clockTime(line.event.ts)}</time>
                    </TableCell>
                    <TableCell className="col-span-2 row-start-2 min-w-0 md:row-auto">
                      <div className="font-semibold [overflow-wrap:anywhere]">{line.description.who}</div>
                      {line.description.what && (
                        <div className="text-sm text-muted-foreground [overflow-wrap:anywhere]">
                          {line.description.what}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="col-span-2 row-start-3 min-w-0 [overflow-wrap:anywhere] md:row-auto">
                      {line.failed && (
                        <span
                          aria-hidden="true"
                          className="mr-2 inline-block size-2 rounded-full bg-destructive align-middle"
                          title="This change failed or did not take"
                        />
                      )}
                      {line.failed && <span className="sr-only">Problem: </span>}
                      <ChangeText text={line.description.change} />
                    </TableCell>
                    <TableCell className="col-start-2 row-start-1 justify-self-end md:justify-self-auto">
                      <ModePill mode={line.mode} />
                    </TableCell>
                    <TableCell className="col-span-2 row-start-4 -ml-3 md:row-auto md:ml-0 md:pr-4 md:text-right">
                      <Button variant="ghost" size="sm" onClick={() => setOpened(line)}>
                        <FileDiff aria-hidden="true" />
                        Diff
                        <span className="sr-only">
                          {" "}
                          for {line.description.who}
                          {line.description.what ? `, ${line.description.what}` : ""}
                        </span>
                      </Button>
                    </TableCell>
                  </TableRow>
                )),
              ])}
            </TableBody>
          </Table>
        </div>
      )}

      {events.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
          <span role="status">
            Showing {shownCount} of {totalCount} {totalCount === 1 ? "change" : "changes"}
            {changes.hasNextPage ? " loaded so far" : ""}
          </span>
          {changes.hasNextPage && (
            <Button
              variant="outline"
              size="sm"
              loading={changes.isFetchingNextPage}
              onClick={() => void changes.fetchNextPage()}
            >
              Load older changes
            </Button>
          )}
          <span>Times are in your browser&rsquo;s local time.</span>
        </div>
      )}

      <Dialog open={opened !== null} onOpenChange={(open) => !open && setOpened(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Change #{opened?.event.id}</DialogTitle>
            <DialogDescription>
              {opened ? `${opened.event.scope} · ${longDate(opened.event.ts)} · exactly as it was recorded` : ""}
            </DialogDescription>
          </DialogHeader>
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted/30 p-3 font-mono text-xs [font-variant-ligatures:none]">
            {opened ? JSON.stringify(opened.event.message, null, 2) : ""}
          </pre>
        </DialogContent>
      </Dialog>
    </div>
  );
}
