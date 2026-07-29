import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecentRuns } from "@/components/user-detail/recent-runs";
import type * as ApiModule from "@/lib/api";
import type { UserRunSummary } from "@/lib/types";

const { getUserRuns, getUserRunsSummary } = vi.hoisted(() => ({
  getUserRuns: vi.fn(),
  getUserRunsSummary: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUserRuns: () => getUserRuns(),
      getUserRunsSummary: () => getUserRunsSummary(),
    },
  };
});

function run(patch: Partial<UserRunSummary> = {}): UserRunSummary {
  return {
    run_id: 42,
    started_at: "2026-07-28T19:30:00Z",
    finished_at: "2026-07-28T19:39:00Z",
    status: "ok",
    error: null,
    reason: "",
    duration_ms: 366_000,
    run_status: "ok",
    dry_run: false,
    diff: { added: ["A", "B"], removed: ["C"] },
    picks: [],
    ...patch,
  };
}

function renderRuns() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RecentRuns userId={7} userSlug="wjat" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RecentRuns", () => {
  beforeEach(() => {
    getUserRuns.mockReset();
    getUserRunsSummary.mockReset();
    getUserRunsSummary.mockResolvedValue({ included: 1, total: 1 });
  });

  it("deep-links each run to this person's own panel", async () => {
    getUserRuns.mockResolvedValue([run()]);
    renderRuns();

    expect(
      await screen.findByRole("link", { name: /Run #42/ }),
    ).toHaveAttribute("href", "/runs/42?user=wjat");
  });

  it("reads a skip as a deliberate outcome with its reason, not as a failure", async () => {
    getUserRuns.mockResolvedValue([
      run({ status: "skipped", reason: "no watch history yet", diff: {} }),
    ]);
    renderRuns();

    expect(await screen.findByText("skipped")).toBeTruthy();
    expect(screen.getByText("no watch history yet")).toBeTruthy();
    // A skip is healthy — it must not be dressed as an error.
    expect(screen.queryByText(/Something went wrong/i)).toBeNull();
  });

  it("shows this person's duration and what changed for them", async () => {
    getUserRuns.mockResolvedValue([run()]);
    renderRuns();

    expect(await screen.findByText(/6m 6s/)).toBeTruthy();
    expect(screen.getByText(/\+2 new · −1 rotated out/)).toBeTruthy();
  });

  it("says how many runs did NOT include this person", async () => {
    getUserRuns.mockResolvedValue([run()]);
    getUserRunsSummary.mockResolvedValue({ included: 6, total: 148 });
    renderRuns();

    // Without this, six entries read as "the server has only run six times".
    expect(
      await screen.findByText(/142 other runs didn’t include this person/i),
    ).toBeTruthy();
  });

  it("says nothing about other runs when this person was in all of them", async () => {
    getUserRuns.mockResolvedValue([run()]);
    getUserRunsSummary.mockResolvedValue({ included: 3, total: 3 });
    renderRuns();

    await screen.findByRole("link", { name: /Run #42/ });
    expect(screen.queryByText(/didn’t include this person/i)).toBeNull();
  });

  it("explains the empty state in terms of runs including them", async () => {
    getUserRuns.mockResolvedValue([]);
    renderRuns();

    expect(
      await screen.findByText(/No runs have included this person yet/i),
    ).toBeTruthy();
  });
});
