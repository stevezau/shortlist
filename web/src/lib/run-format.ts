import type { RunDetail, RunLogEntry } from "@/lib/types";
import {
  isServerStage,
  isTailStage,
  progressLabel,
  STAGE_LABELS,
} from "@/lib/run-stages";

/** How often a run that has not finished is re-fetched when the live stream tells us nothing. */
const RUN_POLL_FALLBACK_MS = 5_000;

/** A run as the poll predicates read it — only whether it has settled matters. */
type Settleable = { finished_at: string | null };

/**
 * How often to re-fetch ONE run, or `false` once it has settled.
 *
 * Exists because the only other thing that refreshes an in-flight run is the live SSE stream
 * (`run-detail.tsx` and `runs.tsx` both wire `run.finished` to `invalidateQueries`) — and
 * `EventSource` replays nothing it missed while it was disconnected. Its `onerror` retries with
 * backoff up to 30s forever, and until it reconnects `run.finished` is never delivered, so a run
 * that has ended still reads "Running" with a ticking timer. That is the SFLIX 2026-08-13 symptom
 * (`runs.tsx`) reached through a dropped connection rather than an idle one, and it is also what
 * leaves a cancelled run stuck on "Stopping…": cancelling records `cancel_requested`, and only the
 * stream ever reports that the run then actually stopped.
 *
 * `false` once `finished_at` is set, so a settled run is never re-fetched for nothing.
 */
export function runRefetchIntervalMs(
  run: Settleable | undefined,
): number | false {
  return run && !run.finished_at ? RUN_POLL_FALLBACK_MS : false;
}

/**
 * {@link runRefetchIntervalMs} for the paged runs list: poll while ANY page holds an unfinished run.
 *
 * Every page, not just the first: the list is fetched in chunks and "Load more" keeps the older,
 * already-settled pages in the cache beside the live one.
 */
export function runsListRefetchIntervalMs(
  pages: Settleable[][] | undefined,
): number | false {
  if (!pages) return false;
  return pages.some((page) => page.some((run) => !run.finished_at))
    ? RUN_POLL_FALLBACK_MS
    : false;
}

/** The one recognised failure class a raw engine/Plex error belongs to, or `null` for anything
 *  unrecognised. The single source of truth both `friendlyError` (what to SAY) and `errorBucket`
 *  (what counts as "the same problem") are built from, so the two can never drift apart. */
type ErrorClass = "plex_5xx" | "timeout" | "rate_limited" | null;

function classifyError(raw: string): ErrorClass {
  if (/\b50\d\b|internal_server_error/i.test(raw)) return "plex_5xx";
  if (/timed?\s?out|timeout/i.test(raw)) return "timeout";
  if (/\b429\b|too many requests/i.test(raw)) return "rate_limited";
  return null;
}

/** A one-line, plain-English take on a raw engine/Plex error — the raw text stays available below. */
export function friendlyError(raw: string): string {
  switch (classifyError(raw)) {
    case "plex_5xx":
      return "Plex hit a server error (500) while writing this row — it was most likely overloaded. This usually clears on the next run.";
    case "timeout":
      return "Plex timed out while writing this row — it was busy. This usually clears on the next run.";
    case "rate_limited":
      return "Plex was rate-limiting writes (429) — too many at once. This usually clears on the next run.";
    default:
      return "Something went wrong building this person’s row.";
  }
}

/**
 * A bucket key for "N people failed with the same problem" — deliberately NARROWER than
 * `friendlyError`. `friendlyError` returns one generic sentence for any unrecognised error, so
 * bucketing on ITS return value grouped five unrelated failures under a claim that they were the
 * same problem. Only the three recognised classes above are ever claimed to match one another; an
 * unrecognised error gets no bucket (`null`) and is never counted toward the banner.
 */
export function errorBucket(raw: string): ErrorClass {
  return classifyError(raw);
}

/** Rank badge colour by tier — the top picks stand out, lower ones recede. */
export function rankClass(rank: number): string {
  if (rank <= 3) return "text-amber-400";
  if (rank <= 10) return "text-foreground";
  return "text-muted-foreground";
}

/** Plain-English names for the AI steps in an llm_tokens_by_step map. */
const STEP_LABELS: Record<string, string> = {
  curate: "final picks",
  llm_web: "web search",
  llm_library: "library scan",
};

/** "final picks 12,340 · web search 4,100" for a by-step token map, or "" when empty. Callers wrap
 *  it in parentheses or not, whichever their sentence needs — the two previous copies differed only
 *  by that punctuation. */
export function tokenStepBreakdown(byStep?: Record<string, number>): string {
  if (!byStep) return "";
  return Object.entries(byStep)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([step, n]) => `${STEP_LABELS[step] ?? step} ${n.toLocaleString()}`)
    .join(" · ");
}

/** " · N web search(es)" when any ran, else "". Shown apart from tokens because an external search is
 * billed (or rate-limited) per REQUEST, not per token. Deliberately names no vendor: the same counter
 * serves Exa and a self-hosted SearXNG. */
export function webSearchSummary(count?: number): string {
  if (!count) return "";
  return ` · ${count} web search${count === 1 ? "" : "es"}`;
}

/**
 * The breakdown behind a row's total time, as one sentence for a `title`.
 *
 * The line used to read "25ms · 8ms waiting · shared setup 159ms", which is three numbers and two
 * engineer concepts: "waiting" is blocked on the Plex write lock, and "shared setup" is work
 * amortised across everyone in the run. Neither is something the owner acts on, and neither is
 * guessable. The total is what belongs on screen; this is what belongs behind it.
 *
 * @param durationMs This row's wall time, waiting included.
 * @param blockedMs Of that, time spent waiting for the Plex write lock.
 * @param setupMs Shared setup this row drew on, or 0/undefined when there was none. NOT part of
 *   `durationMs` — it is one cost spread over every person in the run, so adding them would count
 *   the same milliseconds once per person.
 * @param format How to render a duration, so this stays consistent with the number on screen.
 * @returns The sentence, or "" when there is nothing worth explaining.
 */
export function rowTimingTitle(
  durationMs: number,
  blockedMs: number,
  setupMs: number | undefined,
  format: (ms: number) => string,
): string {
  const parts: string[] = [];
  if (blockedMs > 0) {
    parts.push(
      `${format(durationMs - blockedMs)} working, ${format(blockedMs)} waiting for the Plex write lock`,
    );
  }
  if (setupMs && setupMs > 0) {
    parts.push(
      `a further ${format(setupMs)} of setup was shared across everyone in this run`,
    );
  }
  if (parts.length === 0) return "";
  return `${format(durationMs)} for this row: ${parts.join("; ")}.`;
}

/** What the run is doing right now, and whether that is the server-wide tail. */
export type RunPhase = {
  /** The phrase itself — "merging share filters 12/46". */
  label: string;
  /** True once every person is terminal and only server-wide work is left, which is the only time
   *  the header may say "Finishing up". */
  tail: boolean;
};

function phrase(entry: RunLogEntry): string {
  const label = STAGE_LABELS[entry.stage] ?? entry.stage;
  const progress = progressLabel(entry.counts ?? {});
  return progress ? `${label} ${progress}` : label;
}

/** The per-person roster the run declared, read defensively — `stats` is an open blob and a run
 *  recorded before `expected_users` existed simply has none. Never includes a shared row: those file
 *  under a synthetic `shared_<row>` slug and the API keeps them in `shared_rows`, not `users`.
 *
 *  Empty means "cannot count", NOT "count what we have". Falling back to `run.users` looks safer than
 *  it is: the pending entries in there are synthesised FROM this same roster, so without it every
 *  person present reads as finished and a run mid-flight would report "12 of 12 people done". */
function rosterOf(run: RunDetail): Set<string> {
  const stats = (run.stats ?? {}) as { expected_users?: unknown[] };
  const roster = new Set<string>();
  for (const entry of stats.expected_users ?? []) {
    const slug = (entry as { slug?: string })?.slug;
    if (slug) roster.add(slug);
  }
  return roster;
}

/** How many shared rows this run means to build, off its own manifest. */
function sharedRowCount(run: RunDetail): number {
  const stats = (run.stats ?? {}) as { expected_rows?: { build?: string }[] };
  return (stats.expected_rows ?? []).filter((row) => row?.build === "shared")
    .length;
}

/** How far the per-user stretch has got, or null before the first person starts.
 *
 *  Counted off the run's own roster and its per-person results — the same two fields the Rows tab
 *  counts, so the header can never disagree with the "9 of 46 people done" sitting beside it.
 *
 *  NOT tallied from the log, which was the first attempt at this: "a subject that isn't Shortlist"
 *  is not the same thing as "a person". The library index emits under the SECTION TITLE
 *  (pipeline.py:214-220 — "Movies", "TV Shows") and a shared row under `shared_<row>`
 *  (rows.py:2480,2667), so counting log subjects inflated the total by both — and because indexing
 *  runs inside `preparing`, it also declared the per-user stretch had begun before a single person
 *  had, replacing the one line that WAS accurate during setup.
 *
 *  The roster is therefore also the filter on the log: a section title and a `shared_*` slug are in
 *  nobody's roster, so neither can make an unstarted run look started.
 */
function peopleProgress(
  run: RunDetail,
  entries: RunLogEntry[],
): { done: number; total: number } | null {
  const roster = rosterOf(run);
  if (roster.size === 0) return null;
  if (!entries.some((e) => e.stage !== "queued" && roster.has(e.user)))
    return null;
  const done = run.users.filter(
    (user) => roster.has(user.slug) && user.status !== "pending",
  ).length;
  return { done, total: roster.size };
}

/** The stage the run is in RIGHT NOW, phrased for the header.
 *
 *  Everything after the last person finishes is server-wide, and used to be silent — so a run in its
 *  tail looked identical to a wedged one. Naming the phase is the whole fix.
 *
 *  But "server-wide" is not the same as "the tail". Reading back to the newest `Shortlist` line and
 *  calling it the current phase meant that for the whole per-user stretch — the long part — the
 *  header reported `preparing`, emitted once before the index build and stale from the first person
 *  onward. Run #10 sat on "Finishing up · getting ready — reading your libraries" while the Rows tab
 *  next to it correctly read "9 of 46 people done". Per-user work gets counted here instead, and
 *  only a genuine `TAIL_STAGES` entry is allowed to claim the run is finishing.
 */
export function currentPhase(
  run: RunDetail,
  entries: RunLogEntry[],
): RunPhase | null {
  let server: RunLogEntry | null = null;
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const entry = entries[i];
    if (entry && isServerStage(entry.user)) {
      server = entry;
      break;
    }
  }

  if (server?.stage === "finished") return null;
  if (server && isTailStage(server.stage))
    return { label: phrase(server), tail: true };

  const people = peopleProgress(run, entries);
  if (people) {
    // Everyone is terminal but `users_done` has not landed yet: `_deliver_phase` is still building
    // the shared rows, which belong to no person and so never move the count. Left on the people
    // line the header goes static for exactly the kind of multi-minute window it exists to
    // explain — one shared-row write into a TV library is ~16.5s on its own before curation.
    const shared = sharedRowCount(run);
    if (people.done >= people.total && shared > 0)
      return {
        label:
          shared === 1 ? "building the shared row" : "building shared rows",
        tail: false,
      };
    return {
      // Deliberately the Rows tab's own wording — the two sit on the same screen and disagreeing
      // about the same number in different words is what made the header look broken.
      label: `building rows — ${people.done} of ${people.total} people done`,
      tail: false,
    };
  }

  return server ? { label: phrase(server), tail: false } : null;
}
