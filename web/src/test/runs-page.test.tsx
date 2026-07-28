import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import { RunsPage } from "@/pages/runs";

const { getRuns, startRun } = vi.hoisted(() => ({
  getRuns: vi.fn(),
  startRun: vi.fn(),
}));

// Only the transport is faked — ApiError and apiErrorMessage stay real, because the whole point is
// that the server's own words reach the screen.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getRuns: () => getRuns(),
      startRun: (body: unknown) => startRun(body),
    },
  };
});

const START_FAILURE =
  "Service temporarily unavailable — try again in a moment.";

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RunsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RunsPage", () => {
  beforeEach(() => {
    getRuns.mockReset();
    startRun.mockReset();
  });

  it("surfaces the server's reason when a run can't start", async () => {
    getRuns.mockResolvedValue([]);
    startRun.mockRejectedValue(new ApiError(503, START_FAILURE));
    renderPage();
    await screen.findByText(/No runs yet/i);

    await userEvent.click(
      screen.getByRole("button", { name: /Run all rows now/i }),
    );

    // The failure used to be swallowed: the button just stopped, as if nothing had happened.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Service temporarily unavailable/i,
    );
  });

  it("says nothing when the run starts", async () => {
    getRuns.mockResolvedValue([]);
    startRun.mockResolvedValue({ run_id: 1 });
    renderPage();
    await screen.findByText(/No runs yet/i);

    await userEvent.click(
      screen.getByRole("button", { name: /Run all rows now/i }),
    );

    expect(startRun).toHaveBeenCalledWith({});
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
