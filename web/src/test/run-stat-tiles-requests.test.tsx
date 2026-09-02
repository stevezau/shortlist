/**
 * The REQUESTED tile has to explain its own zero.
 *
 * Audit round 16, 2026-08-18: `requests_pool` / `requests_examined` reached the run stats but nothing
 * read them, so "0 requested" still said nothing — and the guide told the owner to look at numbers
 * the UI never showed. A bare zero reads the same whether nothing was wanted, the floors emptied the
 * pool, or the gate ran out of lookups before reaching anything good. Only the last is actionable,
 * and telling them apart used to mean reading the container log by hand.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunStatTiles } from "@/components/runs/run-stat-tiles";
import type { RunDetail } from "@/lib/types";

function renderTiles(stats: Record<string, unknown>) {
  const run = {
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
      // these cases from a historic run that cannot say either way.
      requests_queued: 0,
      ...stats,
    },
  } as unknown as RunDetail;
  render(<RunStatTiles run={run} />);
}

describe("the REQUESTED tile", () => {
  it("names the floors when they emptied the pool", () => {
    renderTiles({ requests_pool: 0 });
    expect(
      screen.getByText(/nothing cleared the demand or year limits/),
    ).toBeInTheDocument();
  });

  it("says how far the gate got when it stopped short — the actionable case", () => {
    renderTiles({ requests_pool: 400, requests_examined: 100 });
    expect(screen.getByText(/rated 100 of 400/)).toBeInTheDocument();
    // Not "wanted": both numbers are sums of per-row checks, so on a multi-row run they double-count
    // a title two rows share — while the `requests_wanted` on the same card is distinct. The two
    // disagreed in print (release review 2026-08-18).
    expect(screen.queryByText(/of 400 wanted/)).not.toBeInTheDocument();
  });

  it("blames the rating limit when everything was rated", () => {
    // Telling this owner to raise the lookup budget would be advice that cannot possibly work.
    renderTiles({ requests_pool: 40, requests_examined: 40 });
    expect(screen.getByText(/rated all 40/)).toBeInTheDocument();
    expect(screen.queryByText(/40 wanted/)).not.toBeInTheDocument();
  });

  it("goes back to the plain hint once something was sent", () => {
    renderTiles({
      titles_requested: 3,
      requests_pool: 40,
      requests_examined: 40,
    });
    // App-neutral wording: the same run can route to Radarr/Sonarr or to Overseerr, and this tile
    // has no access to which — naming one of them was wrong half the time.
    expect(screen.getByText(/sent to be downloaded/)).toBeInTheDocument();
  });

  it("still prefers a real config warning over the explanation", () => {
    renderTiles({
      requests_pool: 0,
      requests_warnings: ["Radarr not fully configured"],
    });
    expect(screen.getByText(/Radarr not fully configured/)).toBeInTheDocument();
  });
});

describe("the REQUESTED tile when titles are waiting", () => {
  // Round 31, 2026-08-18: caught on a REAL run. `auto_min_demand` had just been raised, so five
  // titles cleared the gate and went to the inbox — and the tile said "none good enough", which is
  // false and points at the wrong setting entirely.
  function renderTiles(stats: Record<string, unknown>) {
    const run = {
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
      stats: { users_ok: 1, users_error: 0, titles_requested: 0, ...stats },
    } as unknown as RunDetail;
    render(<RunStatTiles run={run} />);
  }

  it("says how many are waiting rather than blaming the rating", () => {
    renderTiles({
      requests_queued: 5,
      requests_pool: 100,
      requests_examined: 88,
    });
    expect(
      screen.getByText(/5 waiting for you to approve in Requests/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/none good enough/)).toBeNull();
  });

  it("still blames the gate when nothing qualified at all", () => {
    renderTiles({
      requests_queued: 0,
      requests_pool: 100,
      requests_examined: 88,
    });
    expect(screen.getByText(/rated 88 of 100/)).toBeInTheDocument();
  });
});

describe("a run recorded before the queued count existed", () => {
  it("does not claim none were good enough", () => {
    // ABSENT is not zero. The stats blob is a record of what THAT run reported, so a historic run
    // cannot tell us whether titles are waiting — asserting "none good enough" would point the
    // reader at the rating floor on no evidence.
    const run = {
      id: 1,
      trigger: "schedule",
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
        requests_pool: 100,
        requests_examined: 88,
      },
    } as unknown as RunDetail;
    render(<RunStatTiles run={run} />);
    expect(screen.queryByText(/none good enough/)).toBeNull();
    expect(
      screen.getByText(/see Requests for anything waiting/),
    ).toBeInTheDocument();
  });
});

describe("a run with nothing missing is not a floors problem", () => {
  // Architecture review, 2026-08-18: `pool === 0` is ALSO what a healthy run on a complete library
  // produces, so "nothing cleared the demand or year limits" reported a fault where there is none.
  // `requests_wanted` was declared in the types but never emitted, so the tile could not tell.
  function renderTiles(stats: Record<string, unknown>) {
    const run = {
      id: 1,
      trigger: "schedule",
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
        requests_queued: 0,
        ...stats,
      },
    } as unknown as RunDetail;
    render(<RunStatTiles run={run} />);
  }

  it("says nothing NEW was missing rather than blaming the floors", () => {
    // "new" because `wanted` is net of handled: a run whose whole inbox was rejected lands here too,
    // and those titles are still missing — just already dealt with.
    renderTiles({ requests_wanted: 0, requests_pool: 0 });
    expect(screen.getByText(/nothing new was missing/)).toBeInTheDocument();
  });

  it("still blames the floors when titles were wanted and none got through", () => {
    renderTiles({ requests_wanted: 700, requests_pool: 0 });
    expect(
      screen.getByText(/nothing cleared the demand or year limits/),
    ).toBeInTheDocument();
  });
});
