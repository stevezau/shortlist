import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ToolsPage } from "@/pages/tools";

const { syncWatched, syncUsers } = vi.hoisted(() => ({
  syncWatched: vi.fn(),
  syncUsers: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      syncWatched,
      syncUsers,
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

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/couldn't finish/i);
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
