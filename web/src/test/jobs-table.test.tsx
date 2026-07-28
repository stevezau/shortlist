import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobsTable } from "@/components/jobs-table";
import type * as ApiModule from "@/lib/api";
import type { Job } from "@/lib/types";

const { getJobs } = vi.hoisted(() => ({ getJobs: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { getJobs } };
});

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 1,
    kind: "sync.check",
    status: "done",
    attempts: 1,
    max_attempts: 3,
    detail: "Checked every row; corrected 0",
    error: null,
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function renderTable() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <JobsTable />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("JobsTable", () => {
  beforeEach(() => {
    getJobs.mockReset();
  });

  it("explains an empty list instead of rendering nothing", async () => {
    // Rendering nothing is how a working feature reads as a missing one — which is exactly what
    // happened on the first deploy: the list was there, had no rows, and looked absent.
    getJobs.mockResolvedValue([]);
    renderTable();

    expect(await screen.findByText(/nothing yet/i)).toBeInTheDocument();
    expect(
      screen.getByText(/when a new person gets access/i),
    ).toBeInTheDocument();
  });

  it("names jobs in plain English, not by their internal kind", async () => {
    getJobs.mockResolvedValue([
      job({ kind: "user.cleanup", detail: "Removed 2 row(s) for sarah" }),
    ]);
    renderTable();

    expect(
      await screen.findByText(/remove a disabled user's rows/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("user.cleanup")).toBeNull();
  });

  it("shows a retry as retrying, not as merely queued", async () => {
    // A job back in `queued` AFTER an attempt is the queue retrying. "Queued" would read as
    // "nothing has happened yet" — the opposite of the truth.
    getJobs.mockResolvedValue([
      job({ status: "queued", attempts: 2, detail: "", error: "ConnectError" }),
    ]);
    renderTable();

    expect(
      await screen.findByText(/retrying \(attempt 2\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/ConnectError/)).toBeInTheDocument();
  });

  it("distinguishes work that never started from a retry", async () => {
    getJobs.mockResolvedValue([
      job({ status: "queued", attempts: 0, detail: "" }),
    ]);
    renderTable();

    expect(await screen.findByText("Queued")).toBeInTheDocument();
  });

  it("says how many attempts a failed job used before giving up", async () => {
    getJobs.mockResolvedValue([
      job({
        status: "failed",
        attempts: 3,
        detail: "",
        error: "Plex unreachable",
      }),
    ]);
    renderTable();

    expect(
      await screen.findByText(/failed after 3 attempts/i),
    ).toBeInTheDocument();
  });

  it("surfaces a load failure rather than looking empty", async () => {
    getJobs.mockRejectedValue(new Error("boom"));
    renderTable();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/nothing yet/i)).toBeNull();
  });
});
