import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { RunRowsTab } from "@/components/runs/run-rows-tab";
import type { RunDetail, RunUserResult } from "@/lib/types";

function user(overrides: Partial<RunUserResult> = {}): RunUserResult {
  return {
    username: "sarah",
    display_name: "Sarah",
    slug: "sarah",
    status: "skipped",
    error: null,
    reason: "None of this person's rows were due to rebuild in this run.",
    duration_ms: 0,
    llm_tokens: 0,
    llm_tokens_by_step: {},
    exa_searches: 0,
    diff: {},
    picks: [],
    breakdown: [],
    has_trace: false,
    rows_considered: { picked: "not_due" },
    ...overrides,
  } as RunUserResult;
}

/** Live run #37's shape: everybody skipped, one shared row that actually built something. */
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
    users: [user(), user({ slug: "mike", display_name: "Mike" })],
    shared_rows: [
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
        diff: { added: ["Dune"], removed: [] },
        picks: [{ rank: 1, title: "Dune" }],
        breakdown: [],
        has_trace: true,
      },
    ],
    ...overrides,
  } as unknown as RunDetail;
}

function renderTab(detail: RunDetail = run()) {
  return render(
    <MemoryRouter>
      <RunRowsTab
        run={detail}
        titles={{ picked: "✨ Picked for You" }}
        idBySlug={new Map([["sarah", 1]])}
      />
    </MemoryRouter>,
  );
}

describe("RunRowsTab", () => {
  it("shows the shared row's own result beside the per-person rows", async () => {
    // The complaint this view answers: the run built a shared row and the page showed only a wall of
    // skipped people. Both rows must be on screen, and the shared one must say what it produced.
    renderTab();

    expect(screen.getByText("👥 Popular Movies on SFLIX")).toBeInTheDocument();
    expect(screen.getByText("✨ Picked for You")).toBeInTheDocument();
    expect(screen.getByText(/1 pick · \+1 −0/)).toBeInTheDocument();
    expect(
      screen.getByText(/2 people · 0 built, 2 not due/),
    ).toBeInTheDocument();
  });

  it("keeps the people collapsed until asked, then names why each was skipped", async () => {
    // 46 people expanded by default is the unbroken scroll this view exists to end.
    renderTab();
    expect(screen.queryByText("Sarah")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText("Sarah")).toBeInTheDocument();
    expect(screen.getAllByText("not due")).toHaveLength(2);
  });

  it("offers a trace for a shared row, pointing at its own route", () => {
    renderTab();

    const trace = screen.getByRole("link", { name: /trace/i });
    expect(trace).toHaveAttribute("href", "/runs/37/trace/row/popular");
  });

  it("explains a shared row that built nothing rather than showing a bare status", () => {
    // A shared row has no person to carry its reason, so a skip used to be answerable from the
    // container log and nowhere else (issue #3).
    renderTab(
      run({
        shared_rows: [
          {
            collection_slug: "popular",
            row_title: "👥 Popular Movies on SFLIX",
            status: "skipped",
            error: null,
            reason:
              "A shared row needs at least 2 people with overlapping viewing.",
            duration_ms: 0,
            llm_tokens: 0,
            llm_tokens_by_step: {},
            exa_searches: 0,
            diff: {},
            picks: [],
            breakdown: [],
            has_trace: false,
          },
        ],
      } as unknown as Partial<RunDetail>),
    );

    expect(
      screen.getByText(/needs at least 2 people with overlapping viewing/i),
    ).toBeInTheDocument();
  });

  it("says a legacy run recorded nothing per row instead of rendering blank", () => {
    renderTab(run({ users: [], shared_rows: [] }));

    expect(screen.getByText("No rows in this run")).toBeInTheDocument();
  });
});
