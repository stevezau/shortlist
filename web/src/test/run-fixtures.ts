import type { RunDetail } from "@/lib/types";

/**
 * A finished, successful run with no people and no rows, for the tests that only vary `stats` (the
 * RunStatTiles files). Cast once here: a RunDetail has far more fields than the tiles read.
 */
export function makeTilesRun(patch: Record<string, unknown> = {}, stats: Record<string, unknown> = {}): RunDetail {
  return {
    id: 1,
    trigger: "manual",
    status: "ok",
    dry_run: false,
    started_at: "2026-08-18T04:18:00Z",
    began_at: "2026-08-18T04:18:00Z",
    finished_at: "2026-08-18T04:24:00Z",
    users: [],
    shared_rows: [],
    error: null,
    promotion_blockers: [],
    stats: {
      users_ok: 1,
      users_error: 0,
      titles_requested: 0,
      // Emitted by every current run; 0 means "known: nothing is waiting", which is what separates
      // a current run from a historic one that cannot say either way.
      requests_queued: 0,
      ...stats,
    },
    ...patch,
  } as unknown as RunDetail;
}
