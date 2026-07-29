/** Pipeline stages in build order + human labels — shared by the first-run
 * wizard step, the global activity pill, and anything else narrating a run. */

export const RUN_STAGES = [
  "history",
  "candidates",
  "curating",
  "delivering",
] as const;

export const STAGE_LABELS: Record<string, string> = {
  queued: "queued",
  preparing: "getting ready — reading your libraries",
  indexing: "reading library…",
  indexed: "library read",
  cataloguing: "reading library for AI picks…",
  history: "reading watch history",
  candidates: "gathering candidates",
  curating: "curating with AI",
  delivering: "writing the row to Plex",
  // The tail phases, which run once for the whole server rather than per person. Without these
  // the activity pill froze on the last user's last stage for the duration — minutes of a
  // healthy run looking wedged.
  reading_history: "reading watch history",
  users_done: "all users done",
  filters: "merging share filters",
  promoting: "putting rows on Home",
  converging: "checking for stranded rows",
  converged: "stranded rows corrected",
  ordering: "ordering rows",
  shelves: "positioning rows on shelves",
  requesting: "asking Sonarr/Radarr for missing titles",
  finished: "run finished",
  done: "done",
  skipped: "skipped",
  error: "failed",
};

/** The server-wide phases, in the order `run()` performs them. Drives the "finishing up" header
 *  line and the Overview tab's phase timeline. `queued`/per-user stages are deliberately absent —
 *  these are the phases that happen once, for everyone, after the last person is done. */
export const TAIL_STAGES = [
  "users_done",
  "filters",
  "promoting",
  "converging",
  "ordering",
  "shelves",
  "requesting",
  "finished",
] as const;

/** Is this a server-wide phase rather than one person's? The engine names the subject "Shortlist"
 *  for these; the log renders them without a person's name attached. */
export function isServerStage(userSlug: string): boolean {
  return userSlug === "Shortlist";
}

/** Stages that write to Plex or plex.tv. The one filter worth having on a privacy-sensitive app:
 *  "what did this run actually change on the server?" is a different question from "what did it do". */
const PLEX_WRITE_STAGES = new Set([
  "delivering",
  "filters",
  "promoting",
  "converging",
  "converged",
  "ordering",
  "shelves",
]);

export type LogFilter = "all" | "errors" | "writes" | "people";

export const LOG_FILTERS: { value: LogFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "errors", label: "Errors" },
  { value: "writes", label: "Plex writes" },
  { value: "people", label: "Per-person" },
];

export function matchesLogFilter(
  filter: LogFilter,
  stage: string,
  userSlug: string,
): boolean {
  switch (filter) {
    case "errors":
      return stage === "error";
    case "writes":
      return PLEX_WRITE_STAGES.has(stage);
    case "people":
      return !isServerStage(userSlug);
    default:
      return true;
  }
}

/**
 * Plain-English phrasing for the per-stage count keys the engine emits, so the activity log reads in
 * human terms rather than raw jargon (a "seed" is one of the person's favourites used to find similar
 * titles — never shown as "28 seeds").
 */
export function countLabel(key: string, value: number | string): string {
  if (key === "row") return String(value);
  switch (key) {
    case "position":
      return `#${value} in line`;
    case "history":
      return `${value} watched titles`;
    case "seeds":
      return `${value} favourites to match`;
    case "candidates":
      return `${value} candidates`;
    case "picks":
      return `${value} picks`;
    case "items":
      return `${value} items`;
    case "done":
      return String(value); // paired with `total` below into "3/5"
    case "total":
      return `of ${value}`;
    case "seconds":
      return `${value}s`;
    case "wanted":
      return `${value} missing titles`;
    case "collections":
      return `${value} rows`;
    case "demoted":
      return `${value} taken down`;
    case "removed":
      return `${value} removed`;
    default:
      return `${value} ${key}`;
  }
}

/** "3/5" for the phases that count out per-account work, so a long phase reads as moving rather
 *  than as a single line that sits there for minutes. Null when this isn't a counted phase. */
export function progressLabel(counts: Record<string, unknown>): string | null {
  const done = counts.done;
  const total = counts.total;
  if (typeof done !== "number" || typeof total !== "number") return null;
  return `${done}/${total}`;
}
