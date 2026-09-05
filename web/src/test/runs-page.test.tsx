import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import { RunsPage } from "@/pages/runs";

const { getRuns, startRun, getJobs, getRunsSummary } = vi.hoisted(() => ({
  getRuns: vi.fn(),
  startRun: vi.fn(),
  // The page also renders the background-jobs history; unmocked it would error and put a second
  // alert on screen, which is not what these tests are about.
  getJobs: vi.fn(async () => []),
  getRunsSummary: vi.fn(
    async (): Promise<{
      total: number;
      ok: number;
      error: number;
      last_finished: string | null;
      last_status: string | null;
    }> => ({
      total: 0,
      ok: 0,
      error: 0,
      last_finished: null,
      last_status: null,
    }),
  ),
}));

// Only the transport is faked — ApiError and apiErrorMessage stay real, because the whole point is
// that the server's own words reach the screen.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getRuns: () => getRuns(),
      startRun: (body: unknown) => startRun(body),
      getJobs: () => getJobs(),
      getRunsSummary: () => getRunsSummary(),
    },
  };
});

/** An EventSource whose listeners a test can fire, so a live `run.finished` can be simulated. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners: Record<string, ((event: MessageEvent<string>) => void)[]> = {};
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.instances.push(this);
  }
  addEventListener(
    type: string,
    handler: (event: MessageEvent<string>) => void,
  ) {
    (this.listeners[type] ??= []).push(handler);
  }
  removeEventListener() {}
  close() {}
  emit(type: string, data: unknown) {
    for (const handler of this.listeners[type] ?? []) {
      handler({ data: JSON.stringify(data) } as MessageEvent<string>);
    }
  }
}
vi.stubGlobal("EventSource", FakeEventSource);

const START_FAILURE =
  "Service temporarily unavailable — try again in a moment.";

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RunsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RunsPage", () => {
  beforeEach(() => {
    getRuns.mockReset();
    startRun.mockReset();
  });

  it("clears a finished run from the list without a manual refresh", async () => {
    // The list had no live updates at all: no SSE, no polling. A run that finished left its row
    // reading "Running" with a ticking timer for as long as the page stayed open — so a cancel that
    // HAD worked looked like one that was ignored, which is exactly how it was reported (SFLIX,
    // 2026-08-13: the log said the run completed at 2m51s while this page still said Running at
    // 3m20s).
    const running = {
      id: 2,
      trigger: "manual",
      status: "running",
      started_at: "2026-07-15T04:18:00Z",
      finished_at: null,
      dry_run: false,
      stats: {},
    };
    getRuns.mockResolvedValue([running]);
    renderPage();
    expect(await screen.findByText(/Running/i)).toBeTruthy();

    getRuns.mockResolvedValue([
      { ...running, status: "aborted", finished_at: "2026-07-15T04:21:00Z" },
    ]);
    FakeEventSource.instances.at(-1)?.emit("run.finished", {
      run_id: 2,
      status: "aborted",
    });

    await waitFor(() => expect(screen.getByText(/aborted/i)).toBeTruthy());
    expect(screen.queryByText(/^Running$/)).toBeNull();
  });

  it("surfaces the server's reason when a run can't start", async () => {
    getRuns.mockResolvedValue([]);
    startRun.mockRejectedValue(new ApiError(503, START_FAILURE));
    renderPage();
    await screen.findByText(/No runs yet/i);

    await userEvent.click(
      screen.getByRole("button", { name: /Run all rows now/i }),
    );

    // The failure used to be swallowed: the button just stopped, as if nothing had happened.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Service temporarily unavailable/i,
    );
  });

  it("says nothing when the run starts", async () => {
    getRuns.mockResolvedValue([]);
    startRun.mockResolvedValue({ run_id: 1 });
    renderPage();
    await screen.findByText(/No runs yet/i);

    await userEvent.click(
      screen.getByRole("button", { name: /Run all rows now/i }),
    );

    expect(startRun).toHaveBeenCalledWith({});
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("RunsPage — the headline above the table", () => {
  beforeEach(() => {
    getRuns.mockReset();
    getRuns.mockResolvedValue([]);
    startRun.mockReset();
    getRunsSummary.mockReset();
  });

  // Four tiles — Runs 1 / Succeeded 1 / Failed 0 / Last run — gave four boxes to one run's worth of
  // information, three of them derivable from the fourth (audit finding, Sep 2026). Two tiles now:
  // when it last ran, and how the history reads.
  it("does not spend a tile each on numbers derivable from the count", async () => {
    getRunsSummary.mockResolvedValue({
      total: 1,
      ok: 1,
      error: 0,
      last_finished: "2026-09-04T18:00:00Z",
      last_status: "ok",
    });
    renderPage();

    expect(await screen.findByText("Last run")).toBeInTheDocument();
    expect(screen.getByText("Runs recorded")).toBeInTheDocument();
    expect(screen.getByText(/all finished cleanly/i)).toBeInTheDocument();
    expect(screen.queryByText("Succeeded")).toBeNull();
    expect(screen.queryByText("Failed")).toBeNull();
  });

  it("keeps the one number that cannot be derived — how many failed", async () => {
    getRunsSummary.mockResolvedValue({
      total: 12,
      ok: 9,
      error: 3,
      last_finished: "2026-09-04T18:00:00Z",
      last_status: "error",
    });
    renderPage();

    expect(await screen.findByText("Runs recorded")).toBeInTheDocument();
    expect(screen.getByText("3 failed")).toBeInTheDocument();
  });

  it("labels the added / rotated-out figures the way the run's own page does", async () => {
    // "3 ok · +60/−0" in the Users column had no legend, while run detail calls the same two
    // figures "added" and "rotated out" — one fact, two vocabularies.
    getRunsSummary.mockResolvedValue({
      total: 1,
      ok: 1,
      error: 0,
      last_finished: "2026-09-04T18:00:00Z",
      last_status: "ok",
    });
    getRuns.mockResolvedValue([
      {
        id: 7,
        trigger: "schedule",
        status: "ok",
        started_at: "2026-09-04T17:58:00Z",
        finished_at: "2026-09-04T18:00:00Z",
        dry_run: false,
        stats: {
          users_ok: 3,
          users_error: 0,
          titles_added: 60,
          titles_removed: 4,
        },
      },
    ]);
    renderPage();

    expect(await screen.findByText(/60/)).toBeInTheDocument();
    expect(screen.getByText(/added/)).toBeInTheDocument();
    expect(screen.getByText(/rotated out/)).toBeInTheDocument();
  });

  it("says nothing about titles rotated out when none were", async () => {
    // "−0" is not a fact anyone needs; it was only ever there to complete the "+N/−N" shape.
    getRunsSummary.mockResolvedValue({
      total: 1,
      ok: 1,
      error: 0,
      last_finished: "2026-09-04T18:00:00Z",
      last_status: "ok",
    });
    getRuns.mockResolvedValue([
      {
        id: 7,
        trigger: "schedule",
        status: "ok",
        started_at: "2026-09-04T17:58:00Z",
        finished_at: "2026-09-04T18:00:00Z",
        dry_run: false,
        stats: {
          users_ok: 3,
          users_error: 0,
          titles_added: 60,
          titles_removed: 0,
        },
      },
    ]);
    renderPage();

    expect(await screen.findByText(/added/)).toBeInTheDocument();
    expect(screen.queryByText(/rotated out/)).toBeNull();
  });
});
