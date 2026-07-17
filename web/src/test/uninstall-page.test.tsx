import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { UninstallPage } from "@/pages/uninstall";

const { uninstall } = vi.hoisted(() => ({ uninstall: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { uninstall: (dry: boolean) => uninstall(dry) } };
});

// useSSE opens an EventSource; jsdom has none, so stub a no-op one.
class FakeEventSource {
  addEventListener() {}
  close() {}
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
}
vi.stubGlobal("EventSource", FakeEventSource);

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UninstallPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UninstallPage", () => {
  beforeEach(() => uninstall.mockReset());

  it("gates the destructive action behind the exact confirm phrase", async () => {
    renderPage();
    const button = screen.getByRole("button", {
      name: /uninstall and restore server/i,
    });
    expect(button).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/type/i), "uninstall shortlist");
    expect(button).toBeEnabled();
  });

  it("previews the plan with a dry run", async () => {
    uninstall.mockResolvedValue({
      filters_restored: 48,
      collections_deleted: ["✨ Picked for You"],
      rows_disabled: 1,
      dry_run: true,
      message: "Preview only — nothing was changed.",
    });
    renderPage();

    await userEvent.click(screen.getByRole("button", { name: /preview/i }));

    expect(uninstall).toHaveBeenCalledWith(true);
    expect(await screen.findByText(/Preview only/i)).toBeInTheDocument();
    expect(screen.getByText(/1 row/)).toBeInTheDocument(); // the new rows count is surfaced
  });

  it("shows a completion summary of what it did when the uninstall finishes", async () => {
    uninstall.mockResolvedValue({
      filters_restored: 48,
      collections_deleted: ["a", "b"],
      rows_disabled: 3,
      dry_run: false,
      message: "Your server is as we found it.",
    });
    renderPage();

    await userEvent.type(screen.getByLabelText(/type/i), "uninstall shortlist");
    await userEvent.click(
      screen.getByRole("button", { name: /uninstall and restore server/i }),
    );

    expect(await screen.findByText(/Uninstall complete/i)).toBeInTheDocument();
    // The three counts of what actually happened are surfaced.
    expect(screen.getByText(/48 share filters restored/i)).toBeInTheDocument();
    expect(screen.getByText(/3 rows.*switched off/i)).toBeInTheDocument();
  });
});
