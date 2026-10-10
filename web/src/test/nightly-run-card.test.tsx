import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";

import { NightlyRunCard } from "@/components/jobs/nightly-run-card";
import type * as ApiModule from "@/lib/api";

const { getSchedule, getUsers, getRuns, startRun } = vi.hoisted(() => ({
  getSchedule: vi.fn(),
  getUsers: vi.fn(),
  getRuns: vi.fn(),
  startRun: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { getSchedule, getUsers, getRuns, startRun } };
});

function renderCard() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <NightlyRunCard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  const soon = new Date(Date.now() + 3 * 3_600_000).toISOString();
  getSchedule.mockResolvedValue({
    jobs: [],
    rows: [{ cron: "30 3 * * *", next_run: soon, type: "cron", rows: [{ id: 1, name: "A", slug: "a" }, { id: 2, name: "B", slug: "b" }] }],
  });
  getUsers.mockResolvedValue([
    { id: 1, enabled: true, departed: false },
    { id: 2, enabled: true, departed: false },
    { id: 3, enabled: false, departed: false },
  ]);
  getRuns.mockResolvedValue([
    { id: 7, status: "ok", dry_run: false, finished_at: new Date().toISOString(), stats: {}, privacy: null },
  ]);
  startRun.mockResolvedValue({ run_id: 8 });
});

it("says when the rows run, what they build for, and how the last run went", async () => {
  renderCard();

  expect(await screen.findByText(/2 rows for 2 people/)).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Nightly rows run" })).toBeInTheDocument();
  expect(await screen.findByText("OK")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Change schedule" })).toHaveAttribute("href", "/rows");
});

it("starts a real run from Run now", async () => {
  renderCard();

  await userEvent.click(await screen.findByRole("button", { name: "Run now" }));

  expect(startRun).toHaveBeenCalledWith({});
});

it("names the last run's warnings the same way the dashboard and Runs list do", async () => {
  getRuns.mockResolvedValue([
    {
      id: 7,
      status: "ok",
      dry_run: false,
      finished_at: new Date().toISOString(),
      stats: {},
      privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [] },
    },
  ]);
  renderCard();

  expect(await screen.findByText("OK · 1 warning")).toBeInTheDocument();
});
