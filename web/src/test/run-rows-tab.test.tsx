import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { RunRowsTab } from "@/components/runs/run-rows-tab";
import type { RunDetail, RunUserResult } from "@/lib/types";

const CONFIG_NAMES = {
  picked: "✨ {library_name} Picked for You",
  because: "🎯 Because you watched {top_seed}",
  popular: "👥 Popular {library_name} on SFLIX",
};

const pick = (rank: number, title: string, reason = "") => ({
  rank,
  title,
  reason,
  seed_title: null,
  sources: [],
  affinity: 1,
  year: null,
  rating: null,
});

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
    has_trace: true,
    rows_considered: { picked: "due", because: "not_due" },
    ...overrides,
  } as unknown as RunUserResult;
}

/** Live run 41: one row selected, delivered to two libraries, for two people. */
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
    shared_rows: [],
    users: [
      user({
        breakdown: [
          {
            row_slug: "picked",
            row_title: "✨ Movies Picked for You",
            library_key: "1",
            library_title: "Movies",
            added: ["Sicario"],
            removed: [],
            kept: [],
            deleted: [],
            created: false,
            picks: [pick(1, "Sicario", "you finished Wind River")],
          },
          {
            row_slug: "picked",
            row_title: "✨ TV Shows Picked for You",
            library_key: "2",
            library_title: "TV Shows",
            added: ["The Bear"],
            removed: [],
            kept: [],
            deleted: [],
            created: false,
            picks: [pick(1, "The Bear")],
          },
        ],
      }),
      user({ slug: "mike", display_name: "Mike", breakdown: [] }),
    ],
    ...overrides,
  } as unknown as RunDetail;
}

function renderTab(detail: RunDetail = run()) {
  return render(
    <MemoryRouter>
      <RunRowsTab
        run={detail}
        titles={CONFIG_NAMES}
        idBySlug={new Map([["sarah", 1]])}
      />
    </MemoryRouter>,
  );
}

describe("RunRowsTab", () => {
  it("shows only the row the run ran, and names the rest as not in it", () => {
    // The complaint: one row was selected, but the page listed every row that exists — so a scoped
    // run looked like it had touched rows the operator never chose.
    renderTab();

    expect(screen.getByText("✨ Picked for You")).toBeInTheDocument();
    expect(
      screen.queryByText("🎯 Because you watched"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/1 row wasn.t in this run/i)).toBeInTheDocument();
  });

  it("names a multi-library row once, with its libraries beside it", () => {
    // Movies looked like it never ran because the row took its name from whichever delivered title
    // arrived last. The row has ONE name; the libraries are their own field.
    renderTab();

    expect(screen.getByText("Movies · TV Shows")).toBeInTheDocument();
    expect(
      screen.queryByText("✨ TV Shows Picked for You"),
    ).not.toBeInTheDocument();
  });

  it("opens a single-row run and shows the picks, not just the names", () => {
    // The regression this replaces: expanding a row gave 46 names, a status and a Trace link, so
    // you could see WHO ran but never what they got without leaving the page.
    renderTab();

    expect(screen.getByText("Sarah")).toBeInTheDocument();
    expect(screen.getByText("Sicario")).toBeInTheDocument();
    expect(screen.getByText("The Bear")).toBeInTheDocument();
    // Per library, with that library's own diff.
    expect(screen.getByText("Movies")).toBeInTheDocument();
    expect(screen.getByText("TV Shows")).toBeInTheDocument();
  });

  it("switches the picks panel when another person is chosen", async () => {
    renderTab();
    expect(screen.getByText("Sicario")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Mike/ }));

    // Mike built nothing for this row, so the panel says so rather than showing Sarah's picks.
    expect(screen.queryByText("Sicario")).not.toBeInTheDocument();
    expect(screen.getByText(/delivered nothing here/i)).toBeInTheDocument();
  });

  it("gives a shared row the same panel, minus the person picker", async () => {
    renderTab(
      run({
        users: [user({ rows_considered: { picked: "not_due" } })],
        shared_rows: [
          {
            collection_slug: "popular",
            row_title: "👥 Popular Movies on SFLIX",
            status: "ok",
            error: null,
            reason: null,
            duration_ms: 64000,
            llm_tokens: 0,
            llm_tokens_by_step: {},
            exa_searches: 0,
            diff: { added: ["Dune"], removed: [] },
            picks: [pick(1, "Dune")],
            breakdown: [
              {
                row_slug: "popular",
                row_title: "👥 Popular Movies on SFLIX",
                library_key: "1",
                library_title: "Movies",
                added: ["Dune"],
                removed: [],
                kept: [],
                deleted: [],
                created: false,
                picks: [pick(1, "Dune", "11 people watched it")],
              },
            ],
            has_trace: true,
          },
        ],
      } as unknown as Partial<RunDetail>),
    );

    expect(screen.getByText("👥 Popular on SFLIX")).toBeInTheDocument();
    expect(screen.getByText("Dune")).toBeInTheDocument();
    // No person picker — there is nobody to choose.
    expect(screen.queryByText("Sarah")).not.toBeInTheDocument();
    // Trace sits on the row, because the row is the thing it traces.
    expect(screen.getByRole("link", { name: /trace/i })).toHaveAttribute(
      "href",
      "/runs/41/trace/row/popular",
    );
  });

  it("explains a shared row that built nothing", () => {
    renderTab(
      run({
        users: [user({ rows_considered: { picked: "not_due" } })],
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

  it("puts a failed person's error in the panel instead of an empty pick list", async () => {
    renderTab(
      run({
        users: [
          user({
            slug: "mike",
            display_name: "Mike",
            status: "error",
            error: "TMDB 401 Unauthorized",
            breakdown: [],
          }),
        ],
      }),
    );

    const alert = screen.getByRole("alert");
    expect(
      within(alert).getByText(/TMDB 401 Unauthorized/),
    ).toBeInTheDocument();
  });

  it("says so when a run built no rows at all", () => {
    renderTab(run({ users: [], shared_rows: [] }));

    expect(screen.getByText("This run built no rows")).toBeInTheDocument();
  });
});
