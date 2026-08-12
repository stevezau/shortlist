import { describe, expect, it } from "vitest";

import { groupRunByRow, rowCounts, rowSummary } from "@/lib/run-rows";
import type { RunDetail, RunUserResult } from "@/lib/types";

/** The two engine blobs are open maps with many required fields; these tests only care about the
 *  couple of keys the grouping reads, so they are cast rather than filled in noise. */
const breakdown = (...entries: { row_slug: string; row_title?: string }[]) =>
  entries as unknown as RunUserResult["breakdown"];
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
    id: 37,
    trigger: "manual",
    status: "ok",
    started_at: "2026-08-12T09:48:47Z",
    finished_at: "2026-08-12T09:50:00Z",
    dry_run: false,
    stats: {},
    error: null,
    promotion_blockers: [],
    users: [],
    shared_rows: [],
    ...overrides,
  } as RunDetail;
}

describe("groupRunByRow", () => {
  it("puts a skipped person under the rows they were skipped for", () => {
    // The case the whole view exists for: on live run #37 every one of 46 people was skipped because
    // nothing was due. They have no breakdown at all, so grouping on delivery alone would leave the
    // entire page empty — `rows_considered` is the only thing that places them.
    const groups = groupRunByRow(
      run({
        users: [
          user({
            status: "skipped",
            reason:
              "None of this person's rows were due to rebuild in this run.",
            rows_considered: { picked: "not_due", because: "not_due" },
          }),
        ],
      }),
      { picked: "✨ Picked for You", because: "🎯 Because you watched" },
    );

    expect(groups.map((g) => g.title)).toEqual([
      "✨ Picked for You",
      "🎯 Because you watched",
    ]);
    expect(groups[0]!.people).toHaveLength(1);
    expect(groups[0]!.people[0]!.decision).toBe("not_due");
    expect(rowSummary(groups[0]!)).toBe("1 person · 0 built, 1 not due");
  });

  it("prefers the title the run actually delivered over the row's current name", () => {
    // A row renamed since the run must not rewrite what that run says it built.
    const groups = groupRunByRow(
      run({
        users: [
          user({
            breakdown: breakdown({ row_slug: "picked", row_title: "Old Name" }),
            rows_considered: { picked: "due" },
          }),
        ],
      }),
      { picked: "Brand New Name" },
    );

    expect(groups[0]!.title).toBe("Old Name");
  });

  it("falls back to the slug rather than rendering a blank row", () => {
    const groups = groupRunByRow(
      run({ users: [user({ rows_considered: { picked: "due" } })] }),
    );

    expect(groups[0]!.title).toBe("picked");
  });

  it("carries a shared row alongside the people, never among them", () => {
    const groups = groupRunByRow(
      run({
        users: [user({ rows_considered: { picked: "not_due" } })],
        shared_rows: sharedRows(
          {
            collection_slug: "popular",
            row_title: "👥 Popular Movies on SFLIX",
            status: "ok",
            error: null,
            reason: null,
            duration_ms: 48000,
            llm_tokens: 0,
            llm_tokens_by_step: {},
            exa_searches: 0,
            diff: { added: ["Dune", "Arrival"], removed: ["Old"] },
            picks: [{ rank: 1, title: "Dune" }],
            breakdown: [],
            has_trace: true,
          },
        ),
      } as Partial<RunDetail>),
    );

    const shared = groups.find((g) => g.kind === "shared");
    expect(shared?.people).toEqual([]);
    expect(shared?.shared?.has_trace).toBe(true);
    expect(rowSummary(shared!)).toBe("1 pick · +2 −1");
  });

  it("counts `due` as built only when the person's own run succeeded", () => {
    // "due" is INTENT. A person the run meant to build for and then failed on must not be counted
    // as a success — that is the difference between a green row and a real problem.
    const group = groupRunByRow(
      run({
        users: [
          user({ slug: "a", status: "ok", rows_considered: { picked: "due" } }),
          user({
            slug: "b",
            status: "error",
            error: "TMDB 401",
            rows_considered: { picked: "due" },
          }),
        ],
      }),
    )[0]!;

    expect(rowCounts(group!)).toMatchObject({ people: 2, built: 1, failed: 1 });
    expect(rowSummary(group!)).toBe("2 people · 1 built, 1 failed");
  });

  it("says 'not recorded' for a run from before the decision was stored", () => {
    // A legacy run has `{}`, and guessing "not due" would print a confident wrong answer on every
    // historical run. It still has to appear in the tree, via its breakdown.
    const group = groupRunByRow(
      run({
        users: [
          user({
            rows_considered: {},
            breakdown: breakdown({
              row_slug: "picked",
              row_title: "✨ Picked for You",
            }),
          }),
        ],
      }),
    )[0]!;

    expect(group!.people[0]!.decision).toBeNull();
    expect(rowSummary(group!)).toBe("1 person · 0 built, 1 not recorded");
  });
});
