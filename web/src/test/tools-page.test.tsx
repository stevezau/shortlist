import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ToolsPage } from "@/pages/tools";

const { reconcileWatched, syncWatched, syncUsers } = vi.hoisted(() => ({
  reconcileWatched: vi.fn(),
  syncWatched: vi.fn(),
  syncUsers: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { reconcileWatched, syncWatched, syncUsers },
  };
});

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

describe("ToolsPage — reconcile watched from Plex", () => {
  beforeEach(() => {
    reconcileWatched.mockReset();
    syncWatched.mockReset();
    syncUsers.mockReset();
  });

  it("tells the owner to mount the database when it isn't configured", async () => {
    reconcileWatched.mockResolvedValue({
      configured: false,
      users: 0,
      added: 0,
    });
    renderPage();

    await userEvent.click(
      screen.getByRole("button", { name: /reconcile now/i }),
    );

    expect(
      await screen.findByText(/no plex database is mounted/i),
    ).toBeInTheDocument();
    // Points at where to fix it, rather than claiming nothing was found.
    expect(
      screen.getByRole("link", { name: /settings → connections/i }),
    ).toHaveAttribute("href", "/settings#connections");
  });

  it("reports how many watched titles it added", async () => {
    reconcileWatched.mockResolvedValue({
      configured: true,
      users: 5,
      added: 42,
    });
    renderPage();

    await userEvent.click(
      screen.getByRole("button", { name: /reconcile now/i }),
    );

    expect(
      await screen.findByText(/added 42 watched titles across 5 users/i),
    ).toBeInTheDocument();
  });

  it("says everyone is in sync when the database held nothing new", async () => {
    reconcileWatched.mockResolvedValue({
      configured: true,
      users: 3,
      added: 0,
    });
    renderPage();

    await userEvent.click(
      screen.getByRole("button", { name: /reconcile now/i }),
    );

    expect(await screen.findByText(/already in sync/i)).toBeInTheDocument();
  });

  it("surfaces a read failure with a retry rather than failing silently", async () => {
    reconcileWatched.mockRejectedValue(
      new Error("database disk image is malformed"),
    );
    renderPage();

    await userEvent.click(
      screen.getByRole("button", { name: /reconcile now/i }),
    );

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(
      screen.getByRole("button", { name: /try again/i }),
    ).toBeInTheDocument();
  });
});
