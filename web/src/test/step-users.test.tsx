import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { User } from "@/lib/types";
import { StepUsers } from "@/pages/setup/step-users";

const { getUsers, syncUsers, patchUser, setAllUsersEnabled } = vi.hoisted(
  () => ({
    getUsers: vi.fn(),
    syncUsers: vi.fn(() => Promise.resolve({})),
    patchUser: vi.fn((_id: number, _patch: unknown) => Promise.resolve()),
    setAllUsersEnabled: vi.fn((_enabled: boolean) =>
      Promise.resolve({ updated: 1, cleaned: 0, enabled: true }),
    ),
  }),
);

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      syncUsers: () => syncUsers(),
      patchUser: (id: number, patch: unknown) => patchUser(id, patch),
      setAllUsersEnabled: (enabled: boolean) => setAllUsersEnabled(enabled),
    },
  };
});

const SARAH: User = {
  id: 4,
  username: "sarah",
  slug: "sarah",
  user_type: "shared",
  enabled: true,
  cold_start: false,
  history_depth: 120,
  last_run_at: null,
  request_tag: "",
  hit_rate: null,
};

function renderStep() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <StepUsers />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StepUsers select all/none", () => {
  beforeEach(() => {
    getUsers.mockReset();
    syncUsers.mockClear();
    setAllUsersEnabled.mockReset();
    setAllUsersEnabled.mockResolvedValue({
      updated: 1,
      cleaned: 0,
      enabled: true,
    });
  });

  it("lets you Select all immediately after Select none, without waiting for the request", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, enabled: true }]);
    // "Select none" runs slow server-side Plex cleanup — model it as an in-flight (never-resolving)
    // request so the button state can't rely on it settling.
    setAllUsersEnabled.mockImplementation((enabled: boolean) =>
      enabled === false
        ? new Promise<{ updated: number; cleaned: number; enabled: boolean }>(
            () => {},
          )
        : Promise.resolve({ updated: 1, cleaned: 0, enabled: true }),
    );
    renderStep();

    const selectNone = await screen.findByRole("button", {
      name: /Select none/i,
    });
    await userEvent.click(selectNone);
    expect(setAllUsersEnabled).toHaveBeenCalledWith(false);

    // Everyone is now optimistically off, so Select all must be live even though the disable request
    // is still in flight (the old code gated on isPending and left it dead), and Select none is the no-op.
    const selectAll = screen.getByRole("button", { name: /Select all/i });
    await waitFor(() => expect(selectAll).not.toBeDisabled());
    expect(selectNone).toBeDisabled();

    await userEvent.click(selectAll);
    expect(setAllUsersEnabled).toHaveBeenCalledWith(true);
  });
});


describe("StepUsers — the owner's own line", () => {
  beforeEach(() => {
    getUsers.mockReset();
    syncUsers.mockClear();
    patchUser.mockReset();
    setAllUsersEnabled.mockClear();
  });

  const OWNER: User = {
    ...SARAH,
    id: 9,
    username: "steve",
    slug: "steve",
    user_type: "owner",
  };

  it("tells a returning owner to switch THEMSELVES on when their row is off", async () => {
    // The pre-select only fires when nobody is enabled, so an owner arriving at an already-configured
    // install sees their own switch OFF — and must not be told they're already on.
    getUsers.mockResolvedValue([SARAH, { ...OWNER, enabled: false }]);
    renderStep();

    expect(
      await screen.findByText(/switch yourself on below to get a row of your own/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/switched on like everyone else/i)).toBeNull();
  });

  it("tells a first-time owner they're already on, since the step switched them on", async () => {
    getUsers.mockResolvedValue([SARAH, OWNER]);
    renderStep();

    expect(
      await screen.findByText(/switched on like everyone else/i),
    ).toBeInTheDocument();
  });

  it("always carries the caveat the owner cannot opt out of", async () => {
    getUsers.mockResolvedValue([SARAH, OWNER]);
    renderStep();

    expect(await screen.findByText(/Heads up, server owner/i)).toBeInTheDocument();
    // The sentence is split by an <em>, so match the fragment that lives in one text node.
    expect(
      screen.getByText(/people.s rows from you/i),
    ).toBeInTheDocument();
  });
});
