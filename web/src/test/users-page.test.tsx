import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { User, UserPatch } from "@/lib/types";
import { UsersPage } from "@/pages/users";

const { toastSuccess } = vi.hoisted(() => ({ toastSuccess: vi.fn() }));

vi.mock("sonner", () => ({
  toast: {
    success: toastSuccess,
    loading: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  },
}));

const { getUsers, patchUser, removeUser, setAllUsersEnabled, syncUsers } =
  vi.hoisted(() => ({
    getUsers: vi.fn(),
    patchUser: vi.fn(),
    removeUser: vi.fn(),
    syncUsers: vi.fn(() =>
      Promise.resolve({ added: 1, updated: 48, total: 49, queued: false }),
    ),
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
      removeUser: (id: number) => removeUser(id),
      setAllUsersEnabled: (enabled: boolean) => setAllUsersEnabled(enabled),
      syncUsers: () => syncUsers(),
    },
  };
});

const SARAH: User = {
  manage_sharing: true,
  id: 4,
  username: "sarah",
  slug: "sarah",
  user_type: "shared",
  restricted: false,
  enabled: true,
  cold_start: false,
  history_depth: 120,
  last_run_at: null,
  request_tag: "",
  hit_rate: null,
  nickname: "",
  friendly_name: "",
  display_name: "",
  avatar_url: "",
  plex_account_id: 0,
  restriction_profile: "",
  unhidden_rows: 0,
  departed: false,
  preview_titles: [],
  prefs: {},
};

const MIKE: User = { ...SARAH, id: 5, username: "mike", slug: "mike" };

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

  it("tells the owner where they DO see everyone's rows — but only once they're in the list", async () => {
    getUsers.mockResolvedValue([SARAH]);
    const { unmount } = renderPage();
    // A server with no owner row yet (pre-sync) shouldn't explain a caveat nobody has hit.
    expect(await screen.findByText("sarah")).toBeInTheDocument();
    expect(screen.queryByText(/you.ll see everyone else.s rows/i)).toBeNull();
    unmount();

    getUsers.mockResolvedValue([
      SARAH,
      { ...SARAH, id: 5, username: "steve", slug: "steve", user_type: "owner" },
    ]);
    renderPage();

    // `promotedToOwnHome` and `promotedToSharedHome` are separate Plex flags, so a friend's row
    // never reaches the owner's Home. The note used to claim otherwise and send them off to make a
    // Home user for nothing.
    expect(
      await screen.findByText(/you.ll see everyone else.s rows/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Collections/i)).toBeInTheDocument();
    // The note must NOT claim the owner's Home shows everyone — `promotedToOwnHome` and
    // `promotedToSharedHome` are separate flags, so a friend's row never reaches it.
    expect(screen.getByText(/Not your Home screen/i)).toBeInTheDocument();
  });

  it("only enables everyone after confirming", async () => {
    getUsers.mockResolvedValue([SARAH]);
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /Enable all/i }),
    );
    // Nothing happens until the confirm — mirrors the "Disable all" flow.
    expect(setAllUsersEnabled).not.toHaveBeenCalled();
    expect(screen.getByText(/Turn on all 1 users\?/i)).toBeTruthy();

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
    getUsers.mockResolvedValueOnce([SARAH]).mockResolvedValue([
      SARAH,
      {
        ...SARAH,
        id: 9,
        username: "steve",
        slug: "steve",
        user_type: "owner",
      },
    ]);
    renderPage();
    expect(await screen.findByText("sarah")).toBeInTheDocument();
    expect(screen.queryByText("steve")).toBeNull();

    await userEvent.click(
      await screen.findByRole("button", { name: /Sync users/i }),
    );

    await waitFor(() => expect(syncUsers).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("steve")).toBeInTheDocument();
  });

  it("says a sync is queued when a run is holding Plex, rather than looking like nothing happened", async () => {
    // Sync from Plex is a WRITER (it renames collections when a nickname drifts), so it defers to an
    // in-flight run. The page shows no counts, so without this the button would simply stop spinning
    // and the roster would be unchanged — indistinguishable from a broken button.
    getUsers.mockResolvedValue([SARAH]);
    syncUsers.mockResolvedValueOnce({
      added: 0,
      updated: 0,
      total: 0,
      queued: true,
    });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: /Sync/ }));

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        "Sync queued",
        expect.objectContaining({
          description: expect.stringContaining("the moment it finishes"),
        }),
      ),
    );
  });

  it("says plex.tv couldn’t be reached rather than silently doing nothing", async () => {
    getUsers.mockResolvedValue([SARAH]);
    syncUsers.mockRejectedValueOnce(new ApiError(502, "plex.tv timed out"));
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /Sync users/i }),
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
    getUsers.mockResolvedValue([
      { ...SARAH, cold_start: true, history_depth: 0 },
    ]);

    renderPage();

    const badge = await screen.findByText("New viewer");
    // Its cell is the watch-history one, so it reads as "0 titles · New viewer".
    expect(badge.closest("td")).toHaveTextContent(/0 titles/);
  });
});

describe("UsersPage — Plex Home accounts", () => {
  beforeEach(() => {
    getUsers.mockReset();
    patchUser.mockReset();
  });

  /** plex.tv reports `restricted: true` for EVERY Plex Home managed account — with a parental preset
   *  or without. Only `restriction_profile` says which, and the two must not look the same. */
  const managed = (restriction_profile: string): User => ({
    ...SARAH,
    id: 9,
    username: "kid",
    slug: "kid",
    user_type: "managed",
    restricted: true,
    restriction_profile,
    enabled: false,
  });

  it("says an account LEFT rather than just showing it switched off", async () => {
    // `enabled: false` means two unrelated things — the owner turned them off, or Plex no longer has
    // them. Rendered identically, a departed account is an unexplained row with no action attached.
    getUsers.mockResolvedValue([
      { ...SARAH, enabled: false, departed: true },
      { ...MIKE, enabled: false, departed: false },
    ]);
    renderPage();

    expect(await screen.findByText(/left the server/i)).toBeInTheDocument();
    // Exactly one — the manually-disabled account must not be labelled as gone.
    expect(screen.getAllByText(/left the server/i)).toHaveLength(1);
  });

  it("offers Remove only for someone who actually left", async () => {
    // On an active account this control would read as "delete this user", dropping their whole
    // history while the nightly run keeps rebuilding their row.
    getUsers.mockResolvedValue([
      { ...SARAH, enabled: false, departed: true },
      { ...MIKE, enabled: true, departed: false },
    ]);
    renderPage();

    await screen.findByText(/left the server/i);
    expect(screen.getAllByRole("button", { name: /remove/i })).toHaveLength(1);
  });

  it("removes the person and says what it dropped", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, enabled: false, departed: true }]);
    removeUser.mockResolvedValue({
      user_id: SARAH.id,
      picks_deleted: 60,
      runs_deleted: 4,
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /remove/i }),
    );
    const confirm = await screen.findByRole("button", { name: /^remove$/i });
    await userEvent.click(confirm);

    await waitFor(() => expect(removeUser).toHaveBeenCalledWith(SARAH.id));
  });

  it("flags an account the last run measured seeing other people's rows", async () => {
    // The Users list is where an owner scans, so the one account with a live privacy exposure has to
    // be distinguishable HERE — not only after clicking into it. Plex refuses a share filter for a
    // profiled account, so nothing Shortlist does can hide those rows; saying so is all it can do.
    getUsers.mockResolvedValue([{ ...managed("older_kid"), unhidden_rows: 3 }]);
    renderPage();

    expect(await screen.findByText(/sees 3 rows/i)).toBeInTheDocument();
  });

  it("does not flag a profiled account that sees nothing", async () => {
    // `little_kid` genuinely sees no collections. A badge on every profiled account would train the
    // owner to ignore the one that matters.
    getUsers.mockResolvedValue([
      { ...managed("little_kid"), unhidden_rows: 0 },
    ]);
    renderPage();

    await screen.findByText("Younger Kid");
    expect(screen.queryByText(/sees \d+ row/i)).toBeNull();
  });

  it("names the actual restriction profile rather than a bare 'Restricted'", async () => {
    // "Younger Kid" tells the owner what they set and therefore what to change; "Restricted" does not.
    getUsers.mockResolvedValue([managed("little_kid")]);
    renderPage();

    expect(await screen.findByText("Younger Kid")).toBeInTheDocument();
  });

  it("disables the toggle only for an account Plex really hides everything from", async () => {
    getUsers.mockResolvedValue([managed("little_kid")]);
    renderPage();

    await screen.findByText("Younger Kid");
    expect(screen.getByRole("switch")).toBeDisabled();
  });

  it("treats a managed account with NO profile as an ordinary user", async () => {
    // Issue #20: keying on `restricted` badged these as parental-controlled and greyed out their
    // toggle, when Plex hides nothing from them and they need a row (and privacy filters) like anyone.
    getUsers.mockResolvedValue([managed("")]);
    renderPage();

    await screen.findByText("kid");
    expect(screen.getByRole("switch")).toBeEnabled();
    expect(screen.queryByText(/Younger Kid|Older Kid|Teen/)).toBeNull();
  });

  it("can be enabled, which the old gate made impossible", async () => {
    getUsers.mockResolvedValue([managed("")]);
    patchUser.mockResolvedValue({});
    renderPage();

    await screen.findByText("kid");
    await userEvent.click(screen.getByRole("switch"));

    expect(patchUser).toHaveBeenCalledWith(9, { enabled: true });
  });
});

/** The way back to the watching-account tool.
 *
 *  It used to live only on the owner note, which is dismissible — and dismissing "you see everyone's
 *  rows" is how people say "yes, I know", not "hide the tool from me for ever". Once dismissed, the
 *  only route back was remembering the URL.
 */
describe("UsersPage — reaching the watching account", () => {
  beforeEach(() => {
    getUsers.mockReset();
  });

  it("offers it in the header when an owner is registered", async () => {
    getUsers.mockResolvedValue([
      { ...SARAH, id: 1, username: "owner", slug: "owner", user_type: "owner" },
      SARAH,
    ]);

    renderPage();

    const link = await screen.findByRole("link", {
      name: /watching account/i,
    });
    // Deep-links past the explainer to the tool itself.
    expect(link).toHaveAttribute("href", "/watching-account?setup=1");
  });

  it("does not offer it when there is no owner row to act on", async () => {
    // The stock roster is shared users only — nobody here HAS the owner's problem.
    getUsers.mockResolvedValue([SARAH, MIKE]);

    renderPage();

    await screen.findByText(/sarah/i);
    expect(
      screen.queryByRole("link", { name: /watching account/i }),
    ).not.toBeInTheDocument();
  });
})
