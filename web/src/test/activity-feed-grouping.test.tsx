import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityFeed } from "@/components/jobs/activity-feed";
import type * as ApiModule from "@/lib/api";
import type { Job, JobCatalogEntry } from "@/lib/types";

const { getJobs } = vi.hoisted(() => ({ getJobs: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { getJobs } };
});

function job(id: number, kind: string, status: Job["status"] = "done"): Job {
  return {
    id,
    kind,
    status,
    attempts: status === "failed" ? 3 : 1,
    max_attempts: 3,
    created_at: "2026-10-10T05:00:00Z",
    started_at: null,
    finished_at: null,
    detail: "",
    error: null,
    payload: {},
    result: {},
  };
}

const CATALOG = [
  { kind: "playback.credit", label: "Credit a finished playback" },
  { kind: "backup.take", label: "Back up" },
] as JobCatalogEntry[];

function renderFeed() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ActivityFeed catalog={CATALOG} />
    </QueryClientProvider>,
  );
}

describe("ActivityFeed repeats", () => {
  beforeEach(() => getJobs.mockReset());

  it("folds consecutive finished runs of one job into a single counted row that expands", async () => {
    getJobs.mockResolvedValue([
      job(6, "playback.credit"),
      job(5, "playback.credit"),
      job(4, "playback.credit"),
      job(3, "backup.take"),
      job(2, "playback.credit"),
    ]);
    renderFeed();
    const group = await screen.findByRole("button", { name: /Credit a finished playback.*×\s*3/ });
    // 1 group + the backup + the lone later credit: three rows, not five.
    expect(screen.getAllByText("Credit a finished playback")).toHaveLength(2);
    expect(group).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(group);
    expect(group).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByText("Credit a finished playback")).toHaveLength(5);
  });

  it("never folds a failure into its neighbours", async () => {
    getJobs.mockResolvedValue([job(3, "playback.credit", "failed"), job(2, "playback.credit", "failed")]);
    renderFeed();
    expect(await screen.findAllByText("Credit a finished playback")).toHaveLength(2);
    expect(screen.queryByText(/×/)).not.toBeInTheDocument();
  });
});
