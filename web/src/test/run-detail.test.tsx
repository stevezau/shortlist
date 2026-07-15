import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RunDetailPage } from "@/pages/run-detail";
import type { RunDetail } from "@/lib/types";

const { getRun, getUsers, getRunLog } = vi.hoisted(() => ({
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
      getRunLog: (id: number) => getRunLog(id),
    },
  };
});

// useSSE opens an EventSource; jsdom has none, so stub a no-op one.
class FakeEventSource {
  addEventListener() {}
  close() {}
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
}
vi.stubGlobal("EventSource", FakeEventSource);

function run(breakdown: RunDetail["users"][number]["breakdown"]): RunDetail {
  return {
    id: 2,
    trigger: "manual",
    status: "ok",
    started_at: "2026-07-15T04:18:00Z",
    finished_at: "2026-07-15T04:24:00Z",
    dry_run: false,
    stats: { users_ok: 1, users_error: 0, titles_requested: 0 },
    users: [
      {
        username: "MooHouse",
        slug: "moohouse",
        status: "ok",
        error: null,
        duration_ms: 335000,
        llm_tokens: 5030,
        diff: {},
        picks: [],
        breakdown,
      },
    ],
  } as RunDetail;
}

function renderDetail() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/runs/2"]}>
        <Routes>
          <Route path="/runs/:id" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RunDetailPage — grouped by library", () => {
  beforeEach(() => {
    getRun.mockReset();
    getUsers.mockReset();
    getRunLog.mockReset();
    getUsers.mockResolvedValue([]);
    getRunLog.mockResolvedValue([]);
  });

  it("shows each library as its own group with its own picks, not one merged list", async () => {
    getRun.mockResolvedValue(
      run([
        {
          row_slug: "picked",
          row_title: "✨ Picked for You",
          library_key: "1",
          library_title: "Movies",
          added: ["Saving Private Ryan"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Saving Private Ryan",
              reason: "war epic",
              seed_title: "Pressure",
            },
          ],
        },
        {
          row_slug: "picked",
          row_title: "✨ Picked for You",
          library_key: "2",
          library_title: "TV Shows",
          added: ["Deadliest Catch"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Deadliest Catch",
              reason: "survival series",
              seed_title: "Gold Rush",
            },
          ],
        },
      ]),
    );

    renderDetail();

    // Both library groups are labelled and carry their own picks — each ranked #1 within its library.
    expect(await screen.findByText("Movies")).toBeInTheDocument();
    expect(screen.getByText("TV Shows")).toBeInTheDocument();
    // The pick reasons are unique to each library's pick list.
    expect(screen.getByText(/war epic/)).toBeInTheDocument();
    expect(screen.getByText(/survival series/)).toBeInTheDocument();
    // Two "#1" ranks — one per library — not a single merged list with duplicate ranks.
    expect(screen.getAllByText("#1")).toHaveLength(2);
  });

  it("groups entries by row, so two different rows render as separate groups", async () => {
    getRun.mockResolvedValue(
      run([
        {
          row_slug: "picked",
          row_title: "✨ Picked for You",
          library_key: "1",
          library_title: "Movies",
          added: ["A"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [{ rank: 1, title: "A", reason: "a" }],
        },
        {
          row_slug: "hidden_gems",
          row_title: "💎 Hidden Gems",
          library_key: "1",
          library_title: "Movies",
          added: ["B"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [{ rank: 1, title: "B", reason: "b" }],
        },
      ]),
    );

    renderDetail();

    // Each row shows as its own group header — not collapsed into one.
    expect(await screen.findByText("✨ Picked for You")).toBeInTheDocument();
    expect(screen.getByText("💎 Hidden Gems")).toBeInTheDocument();
  });

  it("shows the run's activity log, seeded from the server buffer", async () => {
    getRun.mockResolvedValue(run([]));
    getRunLog.mockResolvedValue([
      {
        ts: "2026-07-15T04:18:05Z",
        run_id: 2,
        user: "moohouse",
        stage: "curating",
        counts: { candidates: 120 },
      },
    ]);

    renderDetail();

    // The stage renders with its human label + the count detail.
    expect(await screen.findByText(/curating with AI/)).toBeInTheDocument();
    expect(screen.getByText(/120 candidates/)).toBeInTheDocument();
  });

  it("falls back to the merged list for legacy runs with no breakdown", async () => {
    getRun.mockResolvedValue(run([]));
    // Legacy run: no breakdown, but a flat diff/picks path still renders.
    getRun.mockResolvedValue({
      ...run([]),
      users: [
        {
          username: "MooHouse",
          slug: "moohouse",
          status: "ok",
          error: null,
          duration_ms: 1000,
          llm_tokens: 0,
          diff: { added: ["Old Title"] },
          picks: [],
          breakdown: [],
        },
      ],
    } as RunDetail);

    renderDetail();

    expect(await screen.findByText("Old Title")).toBeInTheDocument();
  });
});
