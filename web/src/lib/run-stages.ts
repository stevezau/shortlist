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
