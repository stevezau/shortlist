import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ToolsPage } from "@/pages/tools";

const { syncWatched, syncUsers, getJobs, runJob } = vi.hoisted(() => ({
  syncWatched: vi.fn(),
  syncUsers: vi.fn(),
  getJobs: vi.fn(),
  runJob: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      syncWatched,
      syncUsers,
      getJobs,
      runJob,
    },
  };
});

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

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ToolsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ToolsPage — sync users and watch history", () => {
  beforeEach(() => {
    syncWatched.mockReset();
    syncUsers.mockReset();
    getJobs.mockReset();
    getJobs.mockResolvedValue([]);
    runJob.mockReset();
    FakeEventSource.latest = null;
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  it("reports real added/updated counts after syncing users", async () => {
    syncUsers.mockResolvedValue({ added: 2, updated: 5, total: 7 });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /sync users/i }),
    );

    expect(
      await screen.findByText(/synced 7 users — 2 added, 5 updated/i),
    ).toBeInTheDocument();
  });

  it("says users are up to date when the sync changed nothing", async () => {
    syncUsers.mockResolvedValue({ added: 0, updated: 0, total: 7 });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /sync users/i }),
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
      await screen.findByRole("button", { name: /sync history/i }),
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
      await screen.findByRole("button", { name: /sync history/i }),
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
      /couldn't finish/i.test(el.textContent ?? ""),
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
      await screen.findByRole("button", { name: /sync users/i }),
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

describe("ToolsPage — sync check", () => {
  beforeEach(() => {
    getJobs.mockReset();
    getJobs.mockResolvedValue([]);
    runJob.mockReset();
    syncWatched.mockReset();
    syncUsers.mockReset();
    FakeEventSource.latest = null;
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  it("explains why rows drift out of sync, not just that they can", async () => {
    renderPage();
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
      await screen.findByRole("button", { name: /check for drift/i }),
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
      await screen.findByRole("button", { name: /check for drift/i }),
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
      await screen.findByRole("button", { name: /check for drift/i }),
    );

    // The count and the noun sit in separate JSX expressions, so match a contiguous fragment.
    expect(await screen.findByText(/this will delete/i)).toBeInTheDocument();
    expect(screen.getByText(/Shortlist_ghost/)).toBeInTheDocument();
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    // Still offered, but the count includes it so the button never understates the damage.
    expect(
      await screen.findByRole("button", { name: /fix 1 row/i }),
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
      await screen.findByRole("button", { name: /check for drift/i }),
    );

    expect(
      await screen.findByText(/everything is in sync/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^fix /i })).toBeNull();
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
      await screen.findByRole("button", { name: /check for drift/i }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /fix 1 row/i }),
    );

    expect(runJob).toHaveBeenLastCalledWith("sync.check");
    expect(await screen.findByText(/corrected 1/i)).toBeInTheDocument();
  });

  it("lists what the jobs actually did, below the buttons", async () => {
    // "I pressed it — did it work?" is one question, so the history lives on the same page as the
    // triggers. Per-status rendering is covered in jobs-table.test.tsx.
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

    expect(await screen.findByText(/background jobs/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/share filters merged for every account/i),
    ).toBeInTheDocument();
  });
});
