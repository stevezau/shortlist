/**
 * The run page reports what the run measured about who can see whose rows.
 *
 * Reporting only: `privacy` comes from the run's own stats (`api/runs.py::_run_privacy`). The page's
 * job is to say it without overclaiming — "OK with warnings" only for an OK run that flagged an
 * account, "Not measured" when the run never looked (older runs, dry runs), and never "every row is
 * hidden" off a check that did not run.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RunDetailPage } from "@/pages/run-detail";
import type { RunDetail } from "@/lib/types";

const { getRun, getUsers, getRunLog, listCollections } = vi.hoisted(() => ({
  listCollections: vi.fn(),
  getRun: vi.fn(),
  getUsers: vi.fn(),
  getRunLog: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getRun: (id: number) => getRun(id),
      getUsers: () => getUsers(),
      listCollections: () => listCollections(),
      getRunLog: (id: number) => getRunLog(id),
    },
  };
});

function person(username: string, added: number): RunDetail["users"][number] {
  return {
    username,
    slug: username,
    display_name: username,
    status: "ok",
    rows_considered: { picked: "due" },
    error: null,
    reason: null,
    exa_searches: 0,
    has_trace: false,
    llm_tokens_by_step: {},
    duration_ms: 1000,
    llm_tokens: 0,
    diff: {},
    cost: null,
    picks: [],
    breakdown: [
      {
        row_slug: "picked",
        row_title: "Picked for You",
        library_key: "1",
        library_title: "Movies",
        added: Array.from({ length: added }, (_, i) => `${username} title ${i}`),
        removed: [],
        kept: [],
        deleted: [],
        created: false,
        picks: [],
      },
    ],
  } as RunDetail["users"][number];
}

function run(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 7,
    shared_rows: [],
    trigger: "schedule",
    status: "ok",
    started_at: "2026-10-03T02:30:00Z",
    began_at: "2026-10-03T02:30:04Z",
    finished_at: "2026-10-03T02:30:07Z",
    dry_run: false,
    stats: { users_ok: 4, users_error: 0, titles_added: 60, titles_removed: 0 },
    error: null,
    promotion_blockers: [],
    privacy: { can_see_others: [], unreadable_filters: [], filters_not_enforced: [] },
    users: [person("sarah", 20), person("mike", 10), person("jess", 20), person("kid", 10)],
    ...overrides,
  } as RunDetail;
}

function renderDetail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/runs/7"]}>
        <Routes>
          <Route path="/runs/:id" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function strip(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Run summary" });
}

beforeEach(() => {
  getRun.mockReset();
  getUsers.mockResolvedValue([]);
  getRunLog.mockResolvedValue([]);
  listCollections.mockResolvedValue([{ slug: "picked", name: "Picked for You" }]);
});

describe("the run summary's Result", () => {
  it("reads 'OK with warnings' when an OK run flagged an account", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [] } }));
    renderDetail();

    expect(within(await strip()).getByText("OK with warnings")).toBeInTheDocument();
    expect(within(await strip()).getByText(/1 warning/)).toBeInTheDocument();
  });

  it("reads plain OK when the run flagged nobody", async () => {
    getRun.mockResolvedValue(run());
    renderDetail();

    const summary = await strip();
    expect(within(summary).getByText("OK")).toBeInTheDocument();
    expect(within(summary).queryByText(/with warnings/)).toBeNull();
  });

  it("reads plain OK when the run did not measure privacy at all", async () => {
    getRun.mockResolvedValue(run({ privacy: null }));
    renderDetail();

    const summary = await strip();
    expect(within(summary).getByText("OK")).toBeInTheDocument();
    expect(within(summary).queryByText(/with warnings/)).toBeNull();
  });

  it("reads plain OK when the enforcement check did not run and nothing else was flagged", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: [], unreadable_filters: [], filters_not_enforced: null } }));
    renderDetail();

    const summary = await strip();
    expect(within(summary).getByText("OK")).toBeInTheDocument();
    expect(within(summary).queryByText(/with warnings/)).toBeNull();
  });
});

describe("the run summary's Privacy cell", () => {
  it("counts the accounts that hide every row and links to Privacy naming the one that does not", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [] } }));
    renderDetail();

    const summary = await strip();
    expect(within(summary).getByText("3 of 4 accounts hide every row")).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: /kid can see others’ rows/ })).toHaveAttribute("href", "/privacy");
  });

  it("says every account hides every row when the run measured and flagged nobody", async () => {
    getRun.mockResolvedValue(run());
    renderDetail();

    expect(within(await strip()).getByText("4 of 4 accounts hide every row")).toBeInTheDocument();
  });

  it("says 'Not measured' — never a count — when the run did not measure", async () => {
    getRun.mockResolvedValue(run({ privacy: null }));
    renderDetail();

    const summary = await strip();
    expect(within(summary).getByText("Not measured")).toBeInTheDocument();
    expect(within(summary).queryByText(/hide every row/)).toBeNull();
  });

  it("says 'Not measured' for a dry run, which builds nothing to hide", async () => {
    getRun.mockResolvedValue(run({ dry_run: true, privacy: null }));
    renderDetail();

    expect(within(await strip()).getByText("Not measured")).toBeInTheDocument();
  });

  it("still counts when only the enforcement spot-check did not run", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: [], unreadable_filters: [], filters_not_enforced: null } }));
    renderDetail();

    expect(within(await strip()).getByText("4 of 4 accounts hide every row")).toBeInTheDocument();
  });

  it("says 'Not fully measured' — never 'every row' — when Plex's filter read did not run", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: [], unreadable_filters: null, filters_not_enforced: null } }));
    renderDetail();

    const summary = await strip();
    expect(within(summary).getByText("Not fully measured")).toBeInTheDocument();
    expect(within(summary).queryByText(/hide every row/)).toBeNull();
    expect(within(summary).queryByText(/with warnings/)).toBeNull();
  });
});

describe("the privacy callout", () => {
  it("names the account that can see other people's rows", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [] } }));
    renderDetail();

    const callout = await screen.findByTestId("run-privacy-callout");
    expect(callout).toHaveTextContent("kid’s row was built, but kid can see everyone else’s.");
    expect(within(callout).getByRole("link", { name: /Fix in Privacy/ })).toHaveAttribute("href", "/privacy");
  });

  it("is absent when nothing was flagged, and when nothing was measured", async () => {
    getRun.mockResolvedValue(run({ privacy: null }));
    renderDetail();
    await strip();

    expect(screen.queryByTestId("run-privacy-callout")).toBeNull();
  });
});

describe("the people list in a row", () => {
  it("shows each person's new picks and marks the flagged one as not private", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [] } }));
    renderDetail();

    const kid = await screen.findByRole("tab", { name: /kid/ });
    expect(kid).toHaveTextContent("+10 new");
    expect(within(kid).getByText("not private")).toBeInTheDocument();

    const sarah = screen.getByRole("tab", { name: /sarah/ });
    expect(sarah).toHaveTextContent("+20 new");
    expect(within(sarah).queryByText("not private")).toBeNull();
  });

  it("lists every person in the run, the flagged one included", async () => {
    getRun.mockResolvedValue(run({ privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [] } }));
    renderDetail();

    await screen.findByRole("tab", { name: /kid/ });
    expect(screen.getAllByRole("tab", { name: /sarah|mike|jess|kid/ })).toHaveLength(4);
  });
});
