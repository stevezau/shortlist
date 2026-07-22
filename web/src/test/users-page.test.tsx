import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { User, UserPatch } from "@/lib/types";
import { UsersPage } from "@/pages/users";

const { getUsers, patchUser, setAllUsersEnabled, syncUsers } = vi.hoisted(() => ({
  getUsers: vi.fn(),
  patchUser: vi.fn(),
  syncUsers: vi.fn(() => Promise.resolve({ added: 1, updated: 48, total: 49 })),
  setAllUsersEnabled: vi.fn((_enabled: boolean) =>
    Promise.resolve({ updated: 1, cleaned: 0, enabled: true }),
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      patchUser: (id: number, patch: UserPatch) => patchUser(id, patch),
      setAllUsersEnabled: (enabled: boolean) => setAllUsersEnabled(enabled),
      syncUsers: () => syncUsers(),
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

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UsersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UsersPage", () => {
  beforeEach(() => {
    getUsers.mockReset();
    patchUser.mockReset();
    setAllUsersEnabled.mockClear();
  });

  it("warns the owner that their own Home shows everyone's rows — but only once they're in the list", async () => {
    getUsers.mockResolvedValue([SARAH]);
    const { unmount } = renderPage();
    // A server with no owner row yet (pre-sync) shouldn't explain a caveat nobody has hit.
    expect(await screen.findByText("sarah")).toBeInTheDocument();
    expect(
      screen.queryByText(/can’t hide rows from the server owner/i),
    ).toBeNull();
    unmount();

    getUsers.mockResolvedValue([
      SARAH,
      { ...SARAH, id: 5, username: "steve", slug: "steve", user_type: "owner" },
    ]);
    renderPage();

    expect(
      await screen.findByText(/can’t hide rows from the server owner/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/watch on a Plex Home user/i)).toBeInTheDocument();
  });

  it("only enables everyone after confirming", async () => {
    getUsers.mockResolvedValue([SARAH]);
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /Enable all/i }),
    );
    // Nothing happens until the confirm — mirrors the "Disable all" flow.
    expect(setAllUsersEnabled).not.toHaveBeenCalled();
    expect(
      screen.getByText(/Give a Picked-for-You row to all 1 users\?/i),
    ).toBeTruthy();

    // Confirm inside the dialog.
    const dialogConfirm = screen
      .getAllByRole("button", { name: /^Enable all$/i })
      .at(-1)!;
    await userEvent.click(dialogConfirm);

    await waitFor(() => expect(setAllUsersEnabled).toHaveBeenCalledWith(true));
  });

  it("only disables everyone after confirming (it removes rows)", async () => {
    getUsers.mockResolvedValue([SARAH]);
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /Disable all/i }),
    );
    // Nothing happens until the confirm — this wipes rows from Plex.
    expect(setAllUsersEnabled).not.toHaveBeenCalled();
    expect(screen.getByText(/Turn off every user\?/i)).toBeTruthy();

    // Confirm inside the dialog.
    const dialogConfirm = screen
      .getAllByRole("button", { name: /^Disable all$/i })
      .at(-1)!;
    await userEvent.click(dialogConfirm);

    await waitFor(() => expect(setAllUsersEnabled).toHaveBeenCalledWith(false));
  });

  it("says why when turning a user off is rejected, rather than just snapping the switch back", async () => {
    getUsers.mockResolvedValue([SARAH]);
    patchUser.mockRejectedValue(new ApiError(500, "The database is locked."));
    renderPage();

    const toggle = await screen.findByRole("switch", {
      name: /Shortlist row for sarah/i,
    });
    await userEvent.click(toggle);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /database is locked/i,
    );
    expect(patchUser).toHaveBeenCalledWith(4, { enabled: false });
    // The Switch mirrors the server, which still has her enabled.
    await waitFor(() => expect(toggle).toBeChecked());
  });

  it("re-fires the same change when the owner retries", async () => {
    getUsers.mockResolvedValue([SARAH]);
    patchUser.mockRejectedValue(new ApiError(500, "The database is locked."));
    renderPage();

    await userEvent.click(
      await screen.findByRole("switch", { name: /Shortlist row for sarah/i }),
    );
    await screen.findByRole("alert");

    patchUser.mockResolvedValue({ ...SARAH, enabled: false });
    await userEvent.click(screen.getByRole("button", { name: /Try again/i }));

    await waitFor(() => expect(patchUser).toHaveBeenCalledTimes(2));
    expect(patchUser.mock.calls.at(-1)).toEqual([4, { enabled: false }]);
  });
});


describe("UsersPage — pulling the roster again", () => {
  beforeEach(() => {
    getUsers.mockReset();
    syncUsers.mockClear();
  });

  it("re-syncs from plex.tv on demand — the only path to it once setup is done", async () => {
    // Without this the wizard was the sole trigger, so an install that had finished setup could
    // never pick up a newly-invited user OR the owner's own row (issue #1 shipped inert).
    //
    // The whole feature lives in the cache invalidation, not the POST: assert the ROSTER refreshes.
    // Asserting only that syncUsers was called would pass just as happily with the invalidation
    // deleted, or pointed at the wrong query key.
    getUsers
      .mockResolvedValueOnce([SARAH])
      .mockResolvedValue([
        SARAH,
        { ...SARAH, id: 9, username: "steve", slug: "steve", user_type: "owner" },
      ]);
    renderPage();
    expect(await screen.findByText("sarah")).toBeInTheDocument();
    expect(screen.queryByText("steve")).toBeNull();

    await userEvent.click(
      await screen.findByRole("button", { name: /Sync from Plex/i }),
    );

    await waitFor(() => expect(syncUsers).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("steve")).toBeInTheDocument();
  });

  it("says plex.tv couldn’t be reached rather than silently doing nothing", async () => {
    getUsers.mockResolvedValue([SARAH]);
    syncUsers.mockRejectedValueOnce(new ApiError(502, "plex.tv timed out"));
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /Sync from Plex/i }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/plex.tv/i);
  });
});

describe("UsersPage — the Type column", () => {
  beforeEach(() => getUsers.mockReset());

  it("names every account's type, instead of an em dash for the common case", async () => {
    // "owner" for one person and "—" for everyone else read as "unknown"; the answer for most
    // people is simply "Shared", which is the ordinary case.
    getUsers.mockResolvedValue([
      SARAH,
      { ...SARAH, id: 5, username: "kid", user_type: "managed" },
      { ...SARAH, id: 9, username: "steve", user_type: "owner" },
    ]);

    renderPage();

    expect(await screen.findByText("Shared")).toBeInTheDocument();
    expect(screen.getByText("Managed")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
  });

  it("puts 'New viewer' beside the watch history it explains, not under Type", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, cold_start: true, history_depth: 0 }]);

    renderPage();

    const badge = await screen.findByText("New viewer");
    // Its cell is the watch-history one, so it reads as "0 titles · New viewer".
    expect(badge.closest("td")).toHaveTextContent(/0 titles/);
  });
});
