import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityIndicator } from "@/components/layout/activity-indicator";
import type { Job } from "@/lib/types";

const { getJobs, getJobCatalog, toastLoading, toastSuccess, toastError } =
  vi.hoisted(() => ({
    getJobs: vi.fn(),
    getJobCatalog: vi.fn(),
    toastLoading: vi.fn(),
    toastSuccess: vi.fn(),
    toastError: vi.fn(),
  }));

vi.mock("sonner", () => ({
  toast: { loading: toastLoading, success: toastSuccess, error: toastError },
}));

vi.mock("@/lib/api", () => ({
  api: {
    getJobs: (kind?: string, limit?: number) => getJobs(kind, limit),
    getJobCatalog: () => getJobCatalog(),
  },
}));

function job(patch: Partial<Job> & { id: number }): Job {
  return {
    // NOT a per-user kind: those are deliberately silent on success (the page announces them), so
    // using one here would make these tests about the suppression rather than the mechanism.
    kind: "backup.take",
    status: "done",
    attempts: 0,
    max_attempts: 3,
    detail: "",
    error: null,
    created_at: null,
    started_at: null,
    finished_at: null,
    ...patch,
  } as Job;
}

/** Renders the indicator and returns a way to make the next poll return something different.
 *
 *  `poll` waits for the data to be COMMITTED, not merely requested. Waiting only for the request lets
 *  the first two polls coalesce into one render, so the component seeds on the second one and is
 *  correctly silent — a green-looking harness that proves nothing.
 */
async function renderIndicator(first: Job[] = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  getJobs.mockResolvedValue(first);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ActivityIndicator />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  const settled = async (jobs: Job[]) => {
    await waitFor(() =>
      expect(client.getQueryData(["jobs", "activity"])).toEqual(jobs),
    );
    // One extra flush, so the effect that seeds `seen` has run against this data.
    await act(async () => {});
  };
  await settled(first);

  return {
    poll: async (jobs: Job[]) => {
      getJobs.mockResolvedValue(jobs);
      await act(async () => {
        await client.refetchQueries({ queryKey: ["jobs", "activity"] });
      });
      await settled(jobs);
    },
  };
}

describe("ActivityIndicator toasts", () => {
  beforeEach(() => {
    getJobs.mockReset();
    getJobCatalog.mockReset();
    getJobCatalog.mockResolvedValue([
      { kind: "backup.take", label: "Remove someone's rows" },
    ]);
    toastLoading.mockClear();
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  it("stays silent on the first poll, however much history it loads", async () => {
    // Otherwise opening any page announces every job the server ran yesterday. This — not the shape
    // of jobTransitions — is what keeps first load quiet, so it has to be asserted here.
    await renderIndicator([
      job({ id: 1, status: "done" }),
      job({ id: 2, status: "done" }),
      job({ id: 3, status: "failed", error: "Plex is down" }),
    ]);

    await waitFor(() => expect(getJobCatalog).toHaveBeenCalled());
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
    expect(toastLoading).not.toHaveBeenCalled();
  });

  it("announces a job that finished between two polls", async () => {
    // The idle poll is 30s and most jobs finish in well under a second, so the queue is routinely
    // read for the first time AFTER the work is done. That is the common case for "I disabled someone
    // and the cleanup ran", and it used to produce no toast at all.
    const { poll } = await renderIndicator();

    await poll([job({ id: 7, status: "done", detail: "Removed 2 rows" })]);

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Remove someone's rows", {
        id: "job-7",
        description: "Removed 2 rows",
      }),
    );
  });

  it("announces a failure the same way", async () => {
    const { poll } = await renderIndicator();

    await poll([job({ id: 8, status: "failed", error: "Plex is down" })]);

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Remove someone's rows", {
        id: "job-8",
        description: "Plex is down",
      }),
    );
  });

  it("says a job started, then says how it ended — not both at once", async () => {
    const { poll } = await renderIndicator();

    await poll([job({ id: 9, status: "running" })]);
    await waitFor(() => expect(toastLoading).toHaveBeenCalledTimes(1));
    expect(toastSuccess).not.toHaveBeenCalled();

    await poll([job({ id: 9, status: "done", detail: "All good" })]);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1));
    // Same toast id, so the spinner is REPLACED rather than stacking a second card.
    expect(toastSuccess.mock.calls[0]?.[1]).toMatchObject({ id: "job-9" });
    expect(toastLoading).toHaveBeenCalledTimes(1);
  });
});

describe("ActivityIndicator — not announcing the same decision twice", () => {
  beforeEach(() => {
    getJobs.mockReset();
    getJobs.mockResolvedValue([]);
    getJobCatalog.mockReset();
    getJobCatalog.mockResolvedValue([
      { kind: "user.cleanup", label: "Remove a disabled person's rows" },
    ]);
    toastLoading.mockClear();
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  it("stays quiet when a per-user job SUCCEEDS — the page already said so by name", async () => {
    // Turning someone off already toasts "Turning off sarah…" then "sarah is off". Adding "Remove a
    // disabled person's rows · Removed 0 row(s) for s_flix" is a second, vaguer toast for one
    // decision, naming the machinery instead of the person.
    const { poll } = await renderIndicator();

    await poll([job({ id: 5, kind: "user.cleanup", status: "done", detail: "Removed 0 row(s)" })]);

    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("still announces a per-user job that FAILS", async () => {
    // Nothing else would ever tell you the cleanup didn't work.
    const { poll } = await renderIndicator();

    await poll([
      job({ id: 6, kind: "user.cleanup", status: "failed", error: "Plex is down" }),
    ]);

    await waitFor(() => expect(toastError).toHaveBeenCalled());
  });
});
