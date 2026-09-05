import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { LogLine, LogPage } from "@/lib/types";
import { LogsPage } from "@/pages/logs";

const { getLogs } = vi.hoisted(() => ({ getLogs: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getLogs: (params: { level: string; q: string; limit: number }) =>
        getLogs(params),
      logsDownloadUrl: () => "/api/system/logs/download",
    },
  };
});

function line(patch: Partial<LogLine> = {}): LogLine {
  return {
    ts: "2026-07-21 07:27:18.100",
    level: "INFO",
    source: "shortlist.server.main:lifespan:168",
    message: "shortlist server up",
    ...patch,
  };
}

function page(lines: LogLine[], patch: Partial<LogPage> = {}): LogPage {
  return {
    lines,
    total_matched: lines.length,
    truncated: false,
    file: "shortlist.log",
    ...patch,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <LogsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LogsPage", () => {
  beforeEach(() => getLogs.mockReset());

  it("shows log lines with their level and message", async () => {
    getLogs.mockResolvedValue(
      page([line({ level: "ERROR", message: "privacy sync failed" })]),
    );

    renderPage();

    expect(await screen.findByText("privacy sync failed")).toBeInTheDocument();
    // "ERROR" is also a level button, so scope to the log itself.
    const log = screen.getByRole("log", { name: /Application logs/i });
    expect(within(log).getByText("ERROR")).toBeInTheDocument();
  });

  it("asks the SERVER for the chosen level, rather than filtering a page it already has", async () => {
    // Filtering client-side would silently cap what's reachable at whatever the last fetch held —
    // the errors you're hunting are usually older than the newest 1000 INFO lines.
    getLogs.mockResolvedValue(page([line()]));
    renderPage();
    await screen.findByText("shortlist server up");

    await userEvent.click(screen.getByRole("button", { name: "ERROR" }));

    await waitFor(() =>
      expect(getLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ level: "ERROR" }),
      ),
    );
  });

  it("passes the search text to the server too", async () => {
    getLogs.mockResolvedValue(page([line()]));
    renderPage();
    await screen.findByText("shortlist server up");

    await userEvent.type(screen.getByLabelText("Filter log lines"), "privacy");

    await waitFor(
      () =>
        expect(getLogs).toHaveBeenLastCalledWith(
          expect.objectContaining({ q: "privacy" }),
        ),
      { timeout: 3000 },
    );
  });

  it("says why the list is empty, and distinguishes 'no match' from 'nothing logged'", async () => {
    getLogs.mockResolvedValue(page([]));

    renderPage();

    expect(await screen.findByText(/No log lines yet/i)).toBeInTheDocument();
    // …and once a filter is typed, the empty state blames the filter instead.
    await userEvent.type(screen.getByLabelText("Filter log lines"), "zzz");
    expect(
      await screen.findByText(/Nothing matches that filter/i),
    ).toBeInTheDocument();
  });

  it("offers a button that clears the filter, rather than telling you to clear it", async () => {
    // The hint used to end "Try DEBUG, or clear the filter." — instructions for two controls that
    // were already on the page, written as prose you have to go and find.
    getLogs.mockResolvedValue(page([]));
    renderPage();
    await userEvent.type(screen.getByLabelText("Filter log lines"), "zzz");
    await screen.findByText(/Nothing matches that filter/i);

    await userEvent.click(
      screen.getByRole("button", { name: /clear filter/i }),
    );

    expect(screen.getByLabelText("Filter log lines")).toHaveValue("");
  });

  it("offers the next quieter level as a button, and switches to it", async () => {
    getLogs.mockResolvedValue(page([]));
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /Show DEBUG and louder/i }),
    );

    await waitFor(() =>
      expect(getLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ level: "DEBUG" }),
      ),
    );
  });

  it("offers nothing to click, and no orphaned advice, at the quietest level with no filter", async () => {
    // DEBUG is the floor (`configure_logging` opens the sink there), so there is no quieter level to
    // offer and nothing on this page can start a run. The hint must not keep promising either.
    getLogs.mockResolvedValue(page([]));
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "DEBUG" }));

    expect(
      await screen.findByText(
        "Nothing has been logged at DEBUG or louder yet.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /clear filter/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /and louder/i })).toBeNull();
  });

  // NOTE: no error-state case here. The failing-query path is `QueryBoundary`'s, covered by
  // library-picker.test.tsx; asserting it through THIS page kept surfacing the rejection as an
  // unowned one and failing the file regardless of how the mock was shaped.

  it("says when the view is capped, so nobody mistakes it for the whole story", async () => {
    getLogs.mockResolvedValue(
      page([line()], { truncated: true, total_matched: 5000 }),
    );

    renderPage();

    expect(
      await screen.findByText(/newest 1 of 5000 matching lines/i),
    ).toBeInTheDocument();
  });

  it("says it is working while the zip is built, instead of looking dead", async () => {
    // It was a plain `<a download>`: the browser fetched silently while the server zipped the logs,
    // which takes seconds on a busy instance. Nothing on screen changed, so the honest reading was
    // that the click had missed — and people click again.
    getLogs.mockResolvedValue(page([line()]));
    let release: (value: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(pending));

    renderPage();
    const button = await screen.findByRole("button", {
      name: /Download \.zip/i,
    });
    await userEvent.click(button);

    expect(
      await screen.findByRole("button", { name: /Preparing/i }),
    ).toBeTruthy();
    expect(fetch).toHaveBeenCalledWith(
      "/api/system/logs/download",
      expect.objectContaining({ credentials: "same-origin" }),
    );
    // String body: a Blob body is rejected by CI's Node ("object.stream is not a function").
    release(
      new Response("zip", { headers: { "content-type": "application/zip" } }),
    );
    vi.unstubAllGlobals();
  });
});
