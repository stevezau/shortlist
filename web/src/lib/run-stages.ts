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
  done: "done",
  skipped: "skipped",
  error: "failed",
};

/**
 * Plain-English phrasing for the per-stage count keys the engine emits, so the activity log reads in
 * human terms rather than raw jargon (a "seed" is one of the person's favourites used to find similar
 * titles — never shown as "28 seeds").
 */
export function countLabel(key: string, value: number): string {
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
    default:
      return `${value} ${key}`;
  }
}
