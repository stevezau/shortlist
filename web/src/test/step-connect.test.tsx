import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PlexServer } from "@/lib/types";
import { StepConnect } from "@/pages/setup/step-connect";

const UNREACHABLE = "https://172-16-10-240.hash.plex.direct:32400";

const { getSession, getServers, setupProbe, setupLink } = vi.hoisted(() => ({
  getSession: vi.fn(() =>
    Promise.resolve({ authenticated: true, login_required: true }),
  ),
  getServers: vi.fn(),
  // Never resolves: the mutation stays "pending", so the test can assert it fired without
  // the component trying to render a full ProbeResult.
  setupProbe: vi.fn(
    (_body: { plex_url: string }) => new Promise<never>(() => {}),
  ),
  setupLink: vi.fn((_body: unknown) => Promise.resolve()),
}));

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return {
    ApiError,
    apiErrorMessage: (error: unknown, fallback: string) =>
      error instanceof ApiError ? error.message : fallback,
    api: {
      getSession: () => getSession(),
      getServers: () => getServers(),
      setupProbe: (body: { plex_url: string }) => setupProbe(body),
      setupLink: (body: unknown) => setupLink(body),
    },
  };
});

function serverWith(connections: PlexServer["connections"]): PlexServer {
  return {
    name: "SFlix",
    machine_id: "m1",
    owned: true,
    version: "1.43.3",
    connections,
  };
}

function renderStep() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <StepConnect
          data={{ linked: false }}
          update={vi.fn()}
          next={vi.fn()}
          complete={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StepConnect", () => {
  beforeEach(() => {
    getServers.mockReset();
    setupProbe.mockClear();
  });

  it("lets the owner select an unreachable address, which fills the URL and runs one check", async () => {
    getServers.mockResolvedValue([
      serverWith([{ uri: UNREACHABLE, local: true, relay: false, ok: false }]),
    ]);
    renderStep();

    const addressButton = await screen.findByRole("button", {
      name: /172-16-10-240/i,
    });
    await userEvent.click(addressButton);

    // Clicking populates the editable URL field even though discovery couldn't reach it...
    expect(screen.getByLabelText(/Plex server URL/i)).toHaveValue(UNREACHABLE);
    // ...and runs the real check for that address exactly once.
    await waitFor(() =>
      expect(setupProbe).toHaveBeenCalledWith({ plex_url: UNREACHABLE }),
    );
    expect(setupProbe).toHaveBeenCalledTimes(1);
  });

  it("does not auto-check a manually typed address (not in the discovered list)", async () => {
    // No reachable address, so nothing is preselected and nothing auto-probes on load.
    getServers.mockResolvedValue([
      serverWith([{ uri: UNREACHABLE, local: true, relay: false, ok: false }]),
    ]);
    renderStep();
    await screen.findByRole("button", { name: /172-16-10-240/i });

    await userEvent.type(
      screen.getByLabelText(/Plex server URL/i),
      "http://192.168.5.5:32400",
    );

    expect(setupProbe).not.toHaveBeenCalled(); // typed addresses wait for the Run checks button
  });
});
