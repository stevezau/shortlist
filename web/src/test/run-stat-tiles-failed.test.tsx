/**
 * A run that failed before it built anything must not caption its zeros as if it had run.
 *
 * Issue #139: Plex was unreachable, the run errored at the start, and the tiles read "0 — nothing
 * was due" and "0 — built, but not promoted". Neither was true: nothing was considered or built.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunStatTiles } from "@/components/runs/run-stat-tiles";
import type { RunDetail } from "@/lib/types";

function renderTiles(overrides: Record<string, unknown>) {
  const run = {
    id: 130,
    trigger: "manual",
    status: "error",
    dry_run: false,
    started_at: "2026-09-30T05:25:00Z",
    began_at: "2026-09-30T05:25:00Z",
    finished_at: "2026-09-30T05:26:34Z",
    users: [],
    shared_rows: [],
    error: "Shortlist could not reach Plex at http://pms:32400: it did not answer in time.",
    promotion_blockers: [],
    stats: { error: "Shortlist could not reach Plex." },
    ...overrides,
  } as unknown as RunDetail;
  render(<RunStatTiles run={run} />);
}

describe("the tiles of a run that failed", () => {
  it("say nothing was built when the run stopped at the start", () => {
    renderTiles({});

    expect(screen.getByText("none were built")).toBeInTheDocument();
    expect(screen.getByText("nobody was built")).toBeInTheDocument();
    expect(screen.queryByText("nothing was due")).toBeNull();
    expect(screen.queryByText("built, but not promoted")).toBeNull();
  });

  it("still say people were built but not promoted when they were", () => {
    renderTiles({ stats: { users_ok: 2, users_error: 0 } });

    expect(screen.getByText("built, but not promoted")).toBeInTheDocument();
  });

  it("leave a clean run that had nothing due as it was", () => {
    renderTiles({ status: "ok", error: null, stats: { users_ok: 0 } });

    expect(screen.getByText("nothing was due")).toBeInTheDocument();
  });
});

describe("the tiles of a run cancelled before anyone's turn", () => {
  it("say it was cancelled, not that nothing was due or missing", () => {
    // Live run 77: a dry run cancelled after a minute, before its first person. Its zeros read as a
    // clean night: "nothing was due" and "nothing new was missing".
    renderTiles({
      status: "aborted",
      dry_run: true,
      error: null,
      stats: { users_skipped: 46, requests_queued: 0, requests_wanted: 0 },
    });

    expect(screen.getByText("cancelled before any were built")).toBeInTheDocument();
    expect(screen.getByText("the run was cancelled")).toBeInTheDocument();
    expect(screen.queryByText("nothing was due")).toBeNull();
    expect(screen.queryByText(/nothing new was missing/)).toBeNull();
  });
});
