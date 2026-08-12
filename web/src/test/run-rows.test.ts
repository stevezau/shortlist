import { describe, expect, it } from "vitest";

import {
  groupRunByRow,
  rowCounts,
  rowDisplayName,
  rowSummary,
} from "@/lib/run-rows";
import type { RunDetail, RunUserResult } from "@/lib/types";

/** The engine blobs have many required fields; these tests only care about the few keys the
 *  grouping reads, so they are cast rather than filled in with noise. */
const breakdown = (
  ...entries: {
    row_slug: string;
    row_title?: string;
    library_title?: string;
    library_key?: string;
  }[]
) => entries as unknown as RunUserResult["breakdown"];
const sharedRows = (...rows: Record<string, unknown>[]) =>
  rows as unknown as RunDetail["shared_rows"];

function user(overrides: Partial<RunUserResult> = {}): RunUserResult {
  return {
    username: "sarah",
    display_name: "Sarah",
    slug: "sarah",
    status: "ok",
    error: null,
    reason: null,
    duration_ms: 100,
    llm_tokens: 0,
    llm_tokens_by_step: {},
    exa_searches: 0,
    diff: {},
    picks: [],
    breakdown: [],
    has_trace: false,
    rows_considered: {},
    ...overrides,
  } as RunUserResult;
}

function run(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 41,
    trigger: "manual",
    status: "ok",
    started_at: "2026-08-13T06:43:00Z",
    finished_at: "2026-08-13T07:13:00Z",
    dry_run: false,
    stats: {},
    error: null,
    promotion_blockers: [],
    users: [],
    shared_rows: [],
    ...overrides,
  } as RunDetail;
}

const CONFIG_NAMES = {
  picked: "✨ {library_name} Picked for You",
  because: "🎯 Because you watched {top_seed}",
  popular: "👥 Popular {library_name} on SFLIX",
};

describe("rowDisplayName", () => {
  it("strips the template placeholders a row is configured with", () => {
    // A row that delivered nothing has no rendered title anywhere, so the raw template leaked onto
    // the page as "✨ {library_name} Picked for You".
    expect(rowDisplayName(CONFIG_NAMES.picked)).toBe("✨ Picked for You");
    expect(rowDisplayName(CONFIG_NAMES.because)).toBe("🎯 Because you watched");
    expect(rowDisplayName(CONFIG_NAMES.popular)).toBe("👥 Popular on SFLIX");
  });
});

describe("groupRunByRow", () => {
  it("shows only the rows the run actually ran", () => {
    // Live run 41: one row selected. The engine scoped correctly, but listing every row that merely
    // EXISTS made a scoped run look like it had touched rows the operator never selected.
    const { groups, notInRun } = groupRunByRow(
      run({
        users: [
          user({
            rows_considered: { picked: "due", because: "not_due" },
            breakdown: breakdown({
              row_slug: "picked",
              library_title: "Movies",
              library_key: "1",
            }),
          }),
        ],
      }),
      CONFIG_NAMES,
    );

    expect(groups.map((g) => g.title)).toEqual(["✨ Picked for You"]);
    expect(notInRun).toEqual([
      { slug: "because", title: "🎯 Because you watched" },
    ]);
  });

  it("names a multi-library row once and lists its libraries separately", () => {
    // The bug that made Movies look like it never ran: `picked` delivered BOTH libraries, and
    // taking the row's name from a delivered title kept whichever arrived last.
    const { groups } = groupRunByRow(
      run({
        users: [
          user({
            rows_considered: { picked: "due" },
            breakdown: breakdown(
              {
                row_slug: "picked",
                row_title: "✨ Movies Picked for You",
                library_title: "Movies",
                library_key: "1",
              },
              {
                row_slug: "picked",
                row_title: "✨ TV Shows Picked for You",
                library_title: "TV Shows",
                library_key: "2",
              },
            ),
          }),
        ],
      }),
      CONFIG_NAMES,
    );

    expect(groups[0]!.title).toBe("✨ Picked for You");
    expect(groups[0]!.libraries).toEqual(["Movies", "TV Shows"]);
  });

  it("gives each person the slice of the row that is theirs", () => {
    // The regression this view was meant to fix: you could see WHO ran but never what they got.
    const { groups } = groupRunByRow(
      run({
        users: [
          user({
            rows_considered: { picked: "due", other: "due" },
            breakdown: breakdown(
              { row_slug: "picked", library_title: "Movies", library_key: "1" },
              { row_slug: "other", library_title: "Movies", library_key: "1" },
            ),
          }),
        ],
      }),
      CONFIG_NAMES,
    );

    const picked = groups.find((g) => g.slug === "picked")!;
    expect(picked.people[0]!.breakdown).toHaveLength(1);
    expect(picked.people[0]!.breakdown[0]!.row_slug).toBe("picked");
  });

  it("carries a shared row alongside the people, never among them", () => {
    const { groups } = groupRunByRow(
      run({
        users: [user({ rows_considered: { picked: "due" } })],
        shared_rows: sharedRows({
          collection_slug: "popular",
          row_title: "👥 Popular Movies on SFLIX",
          status: "ok",
          error: null,
          reason: null,
          duration_ms: 64000,
          llm_tokens: 0,
          llm_tokens_by_step: {},
          exa_searches: 0,
          diff: { added: ["Dune"], removed: ["Old"] },
          picks: [{ rank: 1, title: "Dune" }],
          breakdown: [{ library_title: "Movies", library_key: "1" }],
          has_trace: true,
        }),
      }),
      CONFIG_NAMES,
    );

    const shared = groups.find((g) => g.kind === "shared")!;
    expect(shared.title).toBe("👥 Popular on SFLIX");
    expect(shared.libraries).toEqual(["Movies"]);
    expect(shared.people).toEqual([]);
    expect(rowSummary(shared)).toBe("1 pick · +1 −1");
  });

  it("keeps a muted or out-of-audience person under the row, with the reason", () => {
    // The row RAN; these two just weren't built for. Dropping them would hide people the operator
    // is looking for; showing them without the reason is the wall of unexplained "skipped".
    const { groups } = groupRunByRow(
      run({
        users: [
          user({ slug: "a", rows_considered: { picked: "due" } }),
          user({
            slug: "b",
            status: "skipped",
            rows_considered: { picked: "muted" },
          }),
          user({
            slug: "c",
            status: "skipped",
            rows_considered: { picked: "not_in_audience" },
          }),
        ],
      }),
      CONFIG_NAMES,
    );

    expect(rowCounts(groups[0]!)).toMatchObject({
      people: 3,
      built: 1,
      muted: 1,
      notInAudience: 1,
    });
    expect(rowSummary(groups[0]!)).toBe(
      "1 of 3 built · 1 muted · 1 not in the audience",
    );
  });

  it("counts a failed person as failed, not built", () => {
    const { groups } = groupRunByRow(
      run({
        users: [
          user({ slug: "a", rows_considered: { picked: "due" } }),
          user({
            slug: "b",
            status: "error",
            error: "TMDB 401",
            rows_considered: { picked: "due" },
          }),
        ],
      }),
      CONFIG_NAMES,
    );

    expect(rowSummary(groups[0]!)).toBe("1 of 2 built · 1 failed");
    expect(groups[0]!.people[1]!.error).toBe("TMDB 401");
  });

  it("still places a legacy run's rows from what it delivered", () => {
    // A run recorded before `rows_considered` existed has `{}`, so scope has to come from the
    // deliveries. It must not vanish from the page.
    const { groups, notInRun } = groupRunByRow(
      run({
        users: [
          user({
            rows_considered: {},
            breakdown: breakdown({
              row_slug: "picked",
              row_title: "✨ Movies Picked for You",
              library_title: "Movies",
              library_key: "1",
            }),
          }),
        ],
      }),
      CONFIG_NAMES,
    );

    expect(groups.map((g) => g.title)).toEqual(["✨ Picked for You"]);
    expect(notInRun).toEqual([]);
  });
});
