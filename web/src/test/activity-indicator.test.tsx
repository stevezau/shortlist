import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityIndicator } from "@/components/layout/activity-indicator";
import type { Job, Run } from "@/lib/types";

const {
  getJobs,
  getJobCatalog,
  getRuns,
  getSchedule,
  toastLoading,
  toastSuccess,
  toastError,
} = vi.hoisted(() => ({
  getJobs: vi.fn(),
  getJobCatalog: vi.fn(),
  getRuns: vi.fn(),
  getSchedule: vi.fn(),
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
    getRuns: (collection?: string, beforeId?: number, limit?: number) =>
      getRuns(collection, beforeId, limit),
    getSchedule: () => getSchedule(),
  },
}));

function run(patch: Partial<Run> & { id: number; status: string }): Run {
  return {
    trigger: "manual",
    started_at: "2026-07-30T06:00:00Z",
    finished_at: null,
    dry_run: false,
    stats: { users_ok: 0, users_error: 0 },
    ...patch,
  } as Run;
}

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
    payload: {},
    result: {},
    // `JobOut.created_at` is not nullable — the row is stamped on insert. The fixture used to say
    // null, which the `as Job` cast hid.
    created_at: "2026-07-28T10:00:00Z",
    started_at: null,
    finished_at: null,
    ...patch,
  };
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
    getRuns.mockReset();
    getRuns.mockResolvedValue([]);
    getSchedule.mockReset();
    getSchedule.mockResolvedValue({ jobs: [], rows: [] });
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
    getRuns.mockReset();
    getRuns.mockResolvedValue([]);
    getSchedule.mockReset();
    getSchedule.mockResolvedValue({ jobs: [], rows: [] });
    toastLoading.mockClear();
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  it("stays quiet when a per-user job SUCCEEDS — the page already said so by name", async () => {
    // Turning someone off already toasts "Turning off sarah…" then "sarah is off". Adding "Remove a
    // disabled person's rows · Removed 0 row(s) for s_flix" is a second, vaguer toast for one
    // decision, naming the machinery instead of the person.
    const { poll } = await renderIndicator();

    await poll([
      job({
        id: 5,
        kind: "user.cleanup",
        status: "done",
        detail: "Removed 0 row(s)",
      }),
    ]);

    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("still announces a per-user job that FAILS", async () => {
    // Nothing else would ever tell you the cleanup didn't work.
    const { poll } = await renderIndicator();

    await poll([
      job({
        id: 6,
        kind: "user.cleanup",
        status: "failed",
        error: "Plex is down",
      }),
    ]);

    await waitFor(() => expect(toastError).toHaveBeenCalled());
  });
});

describe("ActivityIndicator — queued vs running", () => {
  beforeEach(() => {
    getJobs.mockReset();
    getJobCatalog.mockReset();
    getJobCatalog.mockResolvedValue([
      { kind: "privacy.sync", label: "Privacy sync" },
      { kind: "backup.take", label: "Back up the database" },
    ]);
    getRuns.mockReset();
    getSchedule.mockReset();
    getSchedule.mockResolvedValue({
      jobs: [
        {
          type: "job",
          kind: "privacy.sync",
          writes_plex: true,
          cron: "",
          next_run: null,
        },
        {
          type: "job",
          kind: "backup.take",
          writes_plex: false,
          cron: "",
          next_run: null,
        },
      ],
      rows: [],
    });
    toastLoading.mockClear();
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  async function open() {
    await userEvent.click(
      await screen.findByRole("button", { name: /background work/i }),
    );
  }

  it("puts a RUNNING job under Running, with no 'waiting' text", async () => {
    getRuns.mockResolvedValue([]);
    await renderIndicator([
      job({ id: 1, kind: "privacy.sync", status: "running" }),
    ]);
    await open();

    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
    expect(screen.queryByText(/waiting/i)).not.toBeInTheDocument();
  });

  it("tells a queued Plex-writing job it's waiting on the run, when one is active", async () => {
    getRuns.mockResolvedValue([run({ id: 50, status: "running" })]);
    await renderIndicator([
      job({ id: 2, kind: "privacy.sync", status: "queued" }),
    ]);
    await open();

    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(screen.queryByText("Running")).not.toBeInTheDocument();
    expect(
      await screen.findByText(/waiting for the run to finish/i),
    ).toBeInTheDocument();
  });

  it("does not blame a run for a queued job when no run is active", async () => {
    getRuns.mockResolvedValue([
      run({ id: 51, status: "ok", finished_at: "2026-07-30T05:00:00Z" }),
    ]);
    await renderIndicator([
      job({ id: 3, kind: "privacy.sync", status: "queued" }),
    ]);
    await open();

    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(screen.getByText(/waiting its turn/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/waiting for the run to finish/i),
    ).not.toBeInTheDocument();
  });

  it("gives a read-only queued job a reason that isn't the run, even while one is active", async () => {
    // Only Plex-writing jobs wait on a run (services/jobs.py::_claimable) — a read-only job queued
    // during a run is queued for the parallel-reader cap instead, and must not be told otherwise.
    getRuns.mockResolvedValue([run({ id: 52, status: "running" })]);
    await renderIndicator([
      job({ id: 4, kind: "backup.take", status: "queued" }),
    ]);
    await open();

    expect(
      await screen.findByText(/waiting for a free slot/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/waiting for the run to finish/i),
    ).not.toBeInTheDocument();
  });

  it("closes when you click away, like the bell beside it", async () => {
    // It stayed open until you clicked its own button again, which reads as a stuck panel. The
    // notification bell next to it always had this; only this one was missing it.
    await renderIndicator([job({ id: 1, status: "running" })]);

    await userEvent.click(
      await screen.findByRole("button", { name: /Background work/ }),
    );
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    await userEvent.click(document.body);

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("closes on Escape", async () => {
    await renderIndicator([job({ id: 1, status: "running" })]);

    await userEvent.click(
      await screen.findByRole("button", { name: /Background work/ }),
    );
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });
});
