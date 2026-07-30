import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { JobsPage } from "@/pages/jobs";

const { syncWatched, syncUsers, getJobs, getJobCatalog, runJob, getSchedule } =
  vi.hoisted(() => ({
    syncWatched: vi.fn(),
    syncUsers: vi.fn(),
    getJobs: vi.fn(),
    getJobCatalog: vi.fn(),
    runJob: vi.fn(),
    getSchedule: vi.fn(),
  }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      syncWatched,
      syncUsers,
      getJobs,
      getJobCatalog,
      runJob,
      getSchedule,
    },
  };
});

/** A catalogue entry with the shape the page renders from; override what a test cares about. */
function entry(kind: string, patch: Record<string, unknown> = {}) {
  return {
    kind,
    label: kind,
    description: "",
    manual: true,
    trigger: "",
    scheduled: false,
    next_run: null,
    last: null,
    total: 0,
    queued: 0,
    running: 0,
    failed: 0,
    ...patch,
  };
}

const CATALOG = [
  entry("sync.history", { label: "Sync watch history" }),
  entry("sync.users", { label: "Sync people from Plex" }),
  entry("sync.check", {
    label: "Sync check",
    description:
      "Checks every row on Plex against what Shortlist intends, and fixes anything that drifted. Rows fall out of step when a run doesn't finish, when the container restarts mid-write, or when someone was paused or disabled while their row was already live.",
  }),
  entry("privacy.sync", { label: "Privacy sync" }),
  entry("backup.take", { label: "Back up the database" }),
  entry("user.cleanup", {
    label: "Remove a disabled person's rows",
    manual: false,
    trigger: "Queued when you disable someone.",
  }),
];

// useSSE opens an EventSource; jsdom has none. This capturing stub lets a test drive the sync bars
// by emitting `sync.progress` / `sync.finished` frames the way the server would.
type Listener = (event: MessageEvent<string>) => void;
class FakeEventSource {
  static latest: FakeEventSource | null = null;
  readonly listeners = new Map<string, Listener[]>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.latest = this;
  }
  addEventListener(name: string, listener: Listener): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  close(): void {}
  emit(name: string, data: unknown): void {
    for (const listener of this.listeners.get(name) ?? []) {
      listener({ data: JSON.stringify(data) } as MessageEvent<string>);
    }
  }
}

function emitSse(name: string, data: unknown): void {
  const source = FakeEventSource.latest;
  if (!source) throw new Error("no EventSource was created");
  act(() => source.emit(name, data));
}

function renderPage(path = "/jobs") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <JobsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("JobsPage — sync users and watch history", () => {
  beforeEach(() => {
    syncWatched.mockReset();
    syncUsers.mockReset();
    getJobs.mockReset();
    getJobs.mockResolvedValue([]);
    getJobCatalog.mockReset();
    getJobCatalog.mockResolvedValue(CATALOG);
    runJob.mockReset();
    FakeEventSource.latest = null;
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  it("reports real added/updated counts after syncing users", async () => {
    syncUsers.mockResolvedValue({ added: 2, updated: 5, total: 7 });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Run: Sync people from Plex$/,
      }),
    );

    expect(
      await screen.findByText(/synced 7 users — 2 added, 5 updated/i),
    ).toBeInTheDocument();
  });

  it("says users are up to date when the sync changed nothing", async () => {
    syncUsers.mockResolvedValue({ added: 0, updated: 0, total: 7 });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Run: Sync people from Plex$/,
      }),
    );

    expect(
      await screen.findByText(/all 7 users are already up to date/i),
    ).toBeInTheDocument();
  });

  it("shows a live watch-history bar from sync events, then the finished count", async () => {
    // The POST only returns 202 "started" — the outcome must come from the bus, not the mutation.
    syncWatched.mockResolvedValue({ started: true });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /^Run: Sync watch history$/ }),
    );

    emitSse("sync.progress", { kind: "watched", done: 0, total: 4 });
    emitSse("sync.progress", { kind: "watched", done: 2, total: 4 });
    const bar = await screen.findByRole("progressbar", {
      name: /syncing watch history/i,
    });
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByText(/syncing 2 of 4 users/i)).toBeInTheDocument();

    // sync.finished clears the bar and reports the real count — not a "started in background" line.
    emitSse("sync.finished", { kind: "watched", ok: true, count: 4 });
    expect(
      screen.queryByRole("progressbar", { name: /syncing watch history/i }),
    ).not.toBeInTheDocument();
    expect(await screen.findByText(/synced 4 users/i)).toBeInTheDocument();
  });

  it("surfaces a watch-history sync failure reported on the bus", async () => {
    syncWatched.mockResolvedValue({ started: true });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /^Run: Sync watch history$/ }),
    );
    emitSse("sync.progress", { kind: "watched", done: 0, total: 3 });
    emitSse("sync.finished", {
      kind: "watched",
      ok: false,
      error: "ConnectError",
    });

    // Scoped: the page renders an alert region per card, so find the one that actually reported it.
    const alerts = await screen.findAllByRole("alert");
    const alert = alerts.find((el) =>
      // Apostrophe-agnostic: the copy uses a typographic ’, and which glyph it is isn't the point.
      /couldn.t finish/i.test(el.textContent ?? ""),
    );
    expect(alert).toBeDefined();
    expect(alert).toHaveTextContent(/ConnectError/);
  });

  it("shows the users bar advancing through fetch then save phases", async () => {
    // Hold the POST open so the bar (driven by sync.isPending) stays mounted while events arrive.
    let resolve!: (v: {
      added: number;
      updated: number;
      total: number;
    }) => void;
    syncUsers.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Run: Sync people from Plex$/,
      }),
    );

    // fetch phase: indeterminate (no aria-valuenow) with a "contacting" line.
    emitSse("sync.progress", { kind: "users", phase: "fetch" });
    const bar = await screen.findByRole("progressbar", {
      name: /syncing users/i,
    });
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText(/contacting plex\.tv/i)).toBeInTheDocument();

    // save phase: determinate.
    emitSse("sync.progress", {
      kind: "users",
      phase: "save",
      done: 3,
      total: 6,
    });
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByText(/saving 3 of 6 users/i)).toBeInTheDocument();

    await act(async () => {
      resolve({ added: 1, updated: 5, total: 6 });
    });
    expect(
      await screen.findByText(/synced 6 users — 1 added, 5 updated/i),
    ).toBeInTheDocument();
  });
});

describe("JobsPage — sync check", () => {
  beforeEach(() => {
    getJobs.mockReset();
    getJobs.mockResolvedValue([]);
    getJobCatalog.mockReset();
    getJobCatalog.mockResolvedValue(CATALOG);
    runJob.mockReset();
    syncWatched.mockReset();
    syncUsers.mockReset();
    FakeEventSource.latest = null;
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  it("explains why rows drift out of sync once you open the job", async () => {
    // The explanation is real content, but it is read once — so it lives behind the row rather than
    // permanently on screen. Collapsed by default is the point of the layout, not a regression.
    renderPage();
    const row = await screen.findByTestId("job-sync.check");
    expect(screen.queryByText(/container restarts mid-write/i)).toBeNull();

    await userEvent.click(
      within(row).getByRole("button", { name: /^Sync check$/ }),
    );
    expect(
      await screen.findByText(/container restarts mid-write/i),
    ).toBeInTheDocument();
  });

  it("previews before it changes anything", async () => {
    // Converge only ever removes visibility, so a live pass is never unsafe — but "press a button,
    // we silently rewrite every library" is the wrong default.
    runJob.mockResolvedValue({
      id: 1,
      kind: "sync.check",
      status: "done",
      detail: "",
      error: null,
      fixed: ["Shortlist_gemnath", "Shortlist_j_fm"],
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Check for drift: Sync check$/,
      }),
    );

    expect(runJob).toHaveBeenCalledWith("sync.check", { dry_run: true });
    expect(await screen.findByText(/Shortlist_gemnath/)).toBeInTheDocument();
    // Only now is the destructive action offered, and it names the count.
    expect(
      await screen.findByRole("button", { name: /fix 2 rows/i }),
    ).toBeInTheDocument();
  });

  it("does not claim everything is in sync when the check never ran", async () => {
    // The queue skips a drain while a run is writing to Plex — exactly when someone presses this.
    // The job comes back `queued` with no result: saying "in sync" would be a lie.
    runJob.mockResolvedValue({
      id: 1,
      kind: "sync.check",
      status: "queued",
      detail: "",
      error: null,
      fixed: [],
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Check for drift: Sync check$/,
      }),
    );

    expect(
      await screen.findByText(/waiting for the current run to finish/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/everything is in sync/i)).toBeNull();
  });

  it("warns loudly and separately when a preview would DELETE something", async () => {
    // Deleting is the one irreversible thing the check does. Folding it into the "N rows" count
    // would hide it in the very preview an operator reads to decide whether to run for real.
    runJob.mockResolvedValue({
      id: 1,
      kind: "sync.check",
      status: "done",
      detail: "",
      error: null,
      fixed: [],
      orphans: ["Shortlist_ghost"],
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Check for drift: Sync check$/,
      }),
    );

    // The count and the noun sit in separate JSX expressions, so match a contiguous fragment.
    expect(await screen.findByText(/this will delete/i)).toBeInTheDocument();
    expect(screen.getByText(/Shortlist_ghost/)).toBeInTheDocument();
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    // Still offered, but the count includes it so the button never understates the damage.
    expect(
      await screen.findByRole("button", { name: /^Fix 1 row$/ }),
    ).toBeInTheDocument();
  });

  it("offers no fix button when nothing drifted", async () => {
    runJob.mockResolvedValue({
      id: 1,
      kind: "sync.check",
      status: "done",
      detail: "",
      error: null,
      fixed: [],
      orphans: [],
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Check for drift: Sync check$/,
      }),
    );

    expect(
      await screen.findByText(/everything is in sync/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Fix \d+ row/i })).toBeNull();
  });

  it("runs the live pass only when the fix button is pressed", async () => {
    runJob
      .mockResolvedValueOnce({
        id: 1,
        kind: "sync.check",
        status: "done",
        detail: "",
        error: null,
        fixed: ["Shortlist_gemnath"],
      })
      .mockResolvedValueOnce({
        id: 2,
        kind: "sync.check",
        status: "done",
        detail: "Checked every row; corrected 1",
        error: null,
        fixed: ["Shortlist_gemnath"],
      });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", {
        name: /^Check for drift: Sync check$/,
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /^Fix 1 row$/ }),
    );

    expect(runJob).toHaveBeenLastCalledWith("sync.check");
    expect(await screen.findByText(/corrected 1/i)).toBeInTheDocument();
  });

  it("shows each job's last outcome on its own card", async () => {
    // The whole point of the restructure: "did the roster sync work?" is answered on the roster
    // sync's card, not by scanning a flat table where every kind's rows are interleaved.
    getJobCatalog.mockResolvedValue([
      entry("sync.users", {
        label: "Sync people from Plex",
        total: 4,
        last: {
          id: 9,
          kind: "sync.users",
          status: "done",
          attempts: 1,
          max_attempts: 3,
          detail: "Synced 7 people from plex.tv",
          error: null,
          created_at: "2026-07-28T10:00:00Z",
          started_at: "2026-07-28T10:00:00Z",
          finished_at: "2026-07-28T10:00:04Z",
        },
      }),
    ]);
    renderPage();

    // Health is on the LINE — you sweep the list, you don't open anything to see it went fine.
    const row = await screen.findByTestId("job-sync.users");
    expect(row).toHaveTextContent(/ago|just now/i);

    // The narrative detail is one click away, not permanently on screen.
    await userEvent.click(
      within(row).getByRole("button", { name: /^Sync people from Plex$/ }),
    );
    expect(row).toHaveTextContent(/synced 7 people from plex\.tv/i);
    expect(row).toHaveTextContent(/4\.0s/);
  });

  it("shows a failed job's error on its card and flags the count", async () => {
    getJobCatalog.mockResolvedValue([
      entry("sync.users", {
        label: "Sync people from Plex",
        total: 3,
        failed: 1,
        last: {
          id: 9,
          kind: "sync.users",
          status: "failed",
          attempts: 3,
          max_attempts: 3,
          detail: "",
          error: "ConnectError: plex.tv unreachable",
          created_at: "2026-07-28T10:00:00Z",
          started_at: null,
          finished_at: null,
        },
      }),
    ]);
    renderPage();

    // A failure is visible WITHOUT opening anything — on the row, and in the page-level chip.
    const row = await screen.findByTestId("job-sync.users");
    expect(row).toHaveTextContent(/Failed/);
    expect(await screen.findByText(/1 failed/i)).toBeInTheDocument();

    // The reason is one click away.
    await userEvent.click(
      within(row).getByRole("button", { name: /^Sync people from Plex$/ }),
    );
    expect(row).toHaveTextContent(/plex\.tv unreachable/i);
  });

  it("opens a job's own history on demand, asking only for that kind", async () => {
    getJobCatalog.mockResolvedValue([
      entry("sync.users", { label: "Sync people from Plex", total: 2 }),
    ]);
    getJobs.mockResolvedValue([
      {
        id: 9,
        kind: "sync.users",
        status: "done",
        attempts: 1,
        max_attempts: 3,
        detail: "Synced 7 people from plex.tv",
        error: null,
        created_at: "2026-07-28T10:00:00Z",
        started_at: null,
        finished_at: null,
      },
    ]);
    renderPage();

    // Not fetched until it's opened — a page of rows must not fire one history request each.
    const row = await screen.findByTestId("job-sync.users");
    expect(getJobs).not.toHaveBeenCalled();

    await userEvent.click(
      within(row).getByRole("button", { name: /^Sync people from Plex$/ }),
    );
    expect(getJobs).not.toHaveBeenCalled(); // expanding the row is still not the history

    await userEvent.click(
      within(row).getByRole("button", { name: /previous runs/i }),
    );

    expect(getJobs).toHaveBeenCalledWith("sync.users", 50);
    expect(
      await screen.findByText(/synced 7 people from plex\.tv/i),
    ).toBeInTheDocument();
  });

  it("shows an automatic job with no run button, and says what queues it", async () => {
    getJobCatalog.mockResolvedValue([
      entry("user.cleanup", {
        label: "Remove a disabled person's rows",
        manual: false,
        trigger: "Queued when you disable someone.",
      }),
    ]);
    renderPage();

    // Automatic jobs get their own group, away from the ones you act on.
    expect(await screen.findByText(/^Automatic$/)).toBeInTheDocument();
    const row = await screen.findByTestId("job-user.cleanup");
    // A button that queued a row-deleting job at no particular target must not exist.
    expect(
      within(row).queryByRole("button", { name: /^Run:/ }),
    ).not.toBeInTheDocument();

    await userEvent.click(
      within(row).getByRole("button", {
        name: /^Remove a disabled person's rows$/,
      }),
    );
    expect(row).toHaveTextContent(/queued when you disable someone/i);
  });

  it("reserves no space under a row until that job has something to report", async () => {
    // The live slot sits in a PADDED wrapper. Passing an element whose children are all conditional
    // makes it always truthy, so every row carried a strip of dead space before anything had run.
    syncUsers.mockResolvedValue({ added: 1, updated: 0, total: 7 });
    renderPage();

    const row = await screen.findByTestId("job-sync.users");
    expect(within(row).queryByTestId("job-live")).not.toBeInTheDocument();

    await userEvent.click(
      within(row).getByRole("button", { name: /^Run: Sync people from Plex$/ }),
    );
    expect(await within(row).findByTestId("job-live")).toBeInTheDocument();
  });

  it("keeps the run list and the cross-job activity feed as separate areas", async () => {
    // The two questions are different: "is the roster sync healthy?" is per-job, "what has my
    // server been doing?" is chronological. The old page answered only the first.
    getJobs.mockResolvedValue([
      {
        id: 9,
        kind: "privacy.sync",
        status: "done",
        attempts: 1,
        max_attempts: 3,
        detail: "Share filters merged for every account",
        error: null,
        created_at: "2026-07-28T10:00:00Z",
        started_at: null,
        finished_at: null,
      },
    ]);
    renderPage();

    // The Jobs area does NOT fetch the cross-job feed.
    await screen.findByTestId("job-sync.users");
    expect(getJobs).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /^Activity$/ }));

    expect(getJobs).toHaveBeenCalledWith(undefined, 100);
    // The feed names the JOB, not the raw kind — `privacy.sync` means nothing to an operator.
    expect(await screen.findByText("Privacy sync")).toBeInTheDocument();
    expect(
      screen.getByText(/share filters merged for every account/i),
    ).toBeInTheDocument();
    // Switching areas swaps the content rather than adding to the scroll.
    expect(screen.queryByTestId("job-sync.users")).not.toBeInTheDocument();
  });
});

describe("JobsPage — the three views of background work", () => {
  beforeEach(() => {
    getJobs.mockReset();
    getJobs.mockResolvedValue([]);
    getJobCatalog.mockReset();
    getJobCatalog.mockResolvedValue(CATALOG);
    getSchedule.mockReset();
    getSchedule.mockResolvedValue({
      jobs: [
        {
          type: "job",
          kind: "backup.take",
          label: "Back up the database",
          description: "Copy the database to /config/backups.",
          setting: "backup.cron",
          cron: "0 3 * * *",
          optional: false,
          writes_plex: false,
          using_default: true,
          next_run: "2026-07-31T03:00:00Z",
        },
      ],
      rows: [],
    });
    FakeEventSource.latest = null;
  });

  it("offers Jobs, Timeline and Activity — the schedule is not its own page", async () => {
    renderPage();

    for (const label of ["Jobs", "Timeline", "Activity"]) {
      expect(
        await screen.findByRole("button", { name: label }),
      ).toBeInTheDocument();
    }
  });

  it("shows the schedule on the Timeline tab", async () => {
    // "What background work exists" and "when does it run" were two pages that each listed every job,
    // so neither could answer a whole question on its own.
    renderPage("/jobs?tab=timeline");

    expect(await screen.findByText(/Back up the database/)).toBeInTheDocument();
    expect(
      screen.getByText(/in the order they fire/i),
    ).toBeInTheDocument();
  });

  it("switching to Timeline loads the schedule, not the job catalogue alone", async () => {
    renderPage();
    await screen.findByRole("button", { name: "Timeline" });
    expect(getSchedule).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Timeline" }));

    expect(await screen.findByText(/Back up the database/)).toBeInTheDocument();
  });
});
