import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import type { AccountPrivacy, Collection, PrivacyStatus, RowSources, User, UserPatch } from "@/lib/types";
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

const {
  getUsers,
  getRequestRowSources,
  getPrivacyStatus,
  listCollections,
  patchUser,
  removeUser,
  setAllUsersEnabled,
  syncUsers,
} = vi.hoisted(() => ({
  getUsers: vi.fn(),
  getRequestRowSources: vi.fn(),
  getPrivacyStatus: vi.fn(),
  listCollections: vi.fn(),
  patchUser: vi.fn(),
  removeUser: vi.fn(),
  syncUsers: vi.fn(() =>
    Promise.resolve({ added: 1, updated: 48, total: 49, queued: false }),
  ),
  setAllUsersEnabled: vi.fn((_enabled: boolean) =>
    Promise.resolve({ updated: 1, cleaned: 0, enabled: true }),
  ),
}));

/** GET /api/requests/row-sources, reduced to what the Requests column reads: who is linked to
 *  Overseerr and which sources are connected at all. */
function sources(
  people: { user_id: number; linked: boolean; ready: number }[],
  states: Partial<Record<"overseerr" | "radarr" | "sonarr", "connected" | "unreachable" | "off">> = {},
): RowSources {
  return {
    complete: true,
    problems: [],
    overseerr: "connected",
    radarr: "off",
    sonarr: "off",
    ...states,
    seerr_requests: 0,
    seerr_requesters: 0,
    seerr_linked: people.filter((p) => p.linked).length,
    servers: [],
    tagged_movies: 0,
    tagged_shows: 0,
    tags: [],
    people: people.map((p) => ({ ...p, display_name: "" })),
  };
}

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      getRequestRowSources: (pattern: string) => getRequestRowSources(pattern),
      getPrivacyStatus: () => getPrivacyStatus(),
      listCollections: () => listCollections(),
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
  requested_by_tag: "",
  picks_watched_30d: null,
  last_pick_watched_at: null,
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

/** GET /api/privacy/status with one account per entry, matched to a person by `user_id`. */
function privacy(accounts: { user_id: number; state: string; missing?: string[] }[]): PrivacyStatus {
  return {
    accounts: accounts.map(
      ({ user_id, state, missing = [] }) =>
        ({
          account_id: 500 + user_id,
          display_name: "",
          hides: [],
          manage_sharing: true,
          missing,
          other_conditions: [],
          restriction_profile: state === "refused_by_plex" ? "older_kid" : "",
          should_hide: missing,
          slug: "",
          state,
          user: "",
          user_id,
          user_type: "shared",
        }) satisfies AccountPrivacy,
    ),
    enforcement: {} as PrivacyStatus["enforcement"],
    error: null,
    read_at: "2026-10-03T02:30:00Z",
    rows_error: null,
    rows_on_plex: [],
    snapshots_kept: 0,
    summary: "clean",
  };
}

function row(id: number, patch: Partial<Collection> = {}): Collection {
  return {
    id,
    slug: `row-${id}`,
    name: `Row ${id}`,
    enabled: true,
    audience: "everyone",
    audience_user_ids: [],
    ...patch,
  } as Collection;
}

beforeEach(() => {
  getPrivacyStatus.mockReset();
  getPrivacyStatus.mockResolvedValue(privacy([]));
  listCollections.mockReset();
  listCollections.mockResolvedValue([]);
});

function renderPage(client?: QueryClient) {
  client ??= new QueryClient({
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
    // Most tests here are not about the hit-rate column; a long-running install is the state that
    // leaves every other assertion unchanged.
    getRequestRowSources.mockReset();
    getRequestRowSources.mockResolvedValue(sources([]));
  });

  it("finds people by display or Plex name and filters their current status", async () => {
    getUsers.mockResolvedValue([SARAH, { ...MIKE, display_name: "Michael", prefs: { paused: true } }]);
    renderPage();
    await screen.findByText("sarah");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search users" }), "mike");
    expect(screen.getByRole("link", { name: "Michael" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "sarah" })).not.toBeInTheDocument();
    await userEvent.clear(screen.getByRole("searchbox", { name: "Search users" }));
    await userEvent.click(screen.getByRole("button", { name: /^Paused/ }));
    expect(screen.getByRole("link", { name: "Michael" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "sarah" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^On/ }));
    expect(screen.getByRole("link", { name: "sarah" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Michael" })).not.toBeInTheDocument();
  });

  it("shows request and viewing context directly without repeating the account type", async () => {
    getUsers.mockResolvedValue([SARAH]);
    getRequestRowSources.mockResolvedValue(sources([{ user_id: SARAH.id, linked: true, ready: 3 }]));
    renderPage();
    expect(await screen.findByText("Linked")).toBeVisible();
    expect(screen.getByText("3 ready")).toBeVisible();
    expect(screen.getAllByText("Shared", { exact: true })).toHaveLength(1);
    expect(screen.queryByText("Shared account", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText("Requests & results", { exact: true })).not.toBeInTheDocument();
  });

  it("keeps select-visible and the search, filter and sort row before the roster for every screen size", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    renderPage();
    await screen.findByRole("link", { name: "sarah" });
    const controls = screen.getByRole("group", { name: "User list controls" });
    await userEvent.click(within(controls).getByRole("button", { name: "Select people" }));
    const select = within(controls).getByRole("checkbox", { name: "Select visible users" });
    const filters = screen.getByRole("group", { name: "Filter and sort users" });
    expect(within(filters).getByRole("combobox", { name: "Sort users" })).toBeVisible();
    expect(within(filters).getByRole("searchbox", { name: "Search users" })).toBeVisible();
    expect(filters.compareDocumentPosition(screen.getByRole("table")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(controls.compareDocumentPosition(screen.getByRole("table")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.type(screen.getByRole("searchbox", { name: "Search users" }), "sarah");
    await userEvent.click(select);
    expect(screen.getByRole("checkbox", { name: "Select sarah" })).toBeChecked();
    await userEvent.clear(screen.getByRole("searchbox", { name: "Search users" }));
    expect(screen.getByRole("checkbox", { name: "Select mike" })).not.toBeChecked();
    expect(select).toBePartiallyChecked();
  });

  it("gives every selection checkbox a 44px tap target on a coarse pointer, behind the media query only", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Select people" }));
    const COARSE = "[@media(pointer:coarse)]";
    for (const name of ["Select visible users", "Select sarah", "Select mike"]) {
      const box = screen.getByRole("checkbox", { name });
      // The checkbox's own label takes the taps, so a tap anywhere in the 44px box toggles it.
      const label = box.closest("label");
      expect(label, name).not.toBeNull();
      const classes = label!.className.split(" ");
      expect(classes).toContain(`${COARSE}:relative`);
      expect(classes).toContain(`${COARSE}:before:h-11`);
      expect(classes).toContain(`${COARSE}:before:min-w-11`);
      // A mouse sees the layout it always saw.
      expect(classes.filter((c) => c.startsWith("before:"))).toEqual([]);
    }
    // Clicking the label (what a tap in the enlarged area lands on) toggles the box.
    await userEvent.click(screen.getByRole("checkbox", { name: "Select sarah" }).closest("label")!);
    expect(screen.getByRole("checkbox", { name: "Select sarah" })).toBeChecked();
  });

  it("dismisses all-user actions with Escape or an outside click and keeps confirmations separate", async () => {
    getUsers.mockResolvedValue([SARAH]);
    renderPage();
    const trigger = screen.getByRole("button", { name: "All users…" });
    await userEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "All user actions" })).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "All user actions" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("heading", { name: "Users" }));
    expect(screen.queryByRole("dialog", { name: "All user actions" })).not.toBeInTheDocument();
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("button", { name: /^Enable all$/ }));
    expect(screen.queryByRole("dialog", { name: "All user actions" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Turn on all 1 users?" })).toBeVisible();
    expect(setAllUsersEnabled).not.toHaveBeenCalled();
  });

  it("pauses only selected people without turning them off", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    patchUser.mockResolvedValue(SARAH);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Select people" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select sarah" }));
    await userEvent.click(screen.getByRole("button", { name: "Pause rebuilding" }));
    await waitFor(() => expect(patchUser).toHaveBeenCalledWith(SARAH.id, { prefs: { paused: true } }));
    expect(patchUser).toHaveBeenCalledTimes(1);
    expect(setAllUsersEnabled).not.toHaveBeenCalled();
  });

  it("keeps failed people selected after a partially saved batch", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    patchUser.mockImplementation((id: number) => id === SARAH.id ? Promise.resolve(SARAH) : Promise.reject(new Error("Unavailable")));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Select people" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select visible users" }));
    await userEvent.click(screen.getByRole("button", { name: "Pause rebuilding" }));
    expect(await screen.findByText(/1 person couldn’t be updated/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select mike" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select sarah" })).not.toBeChecked();
  });

  it("shows picks watched in the last 30 days, with the last watch in the tooltip", async () => {
    const lastWatched = new Date(Date.now() - 5 * 86_400_000).toISOString();
    getUsers.mockResolvedValue([
      { ...SARAH, picks_watched_30d: 3, last_pick_watched_at: lastWatched },
    ]);

    renderPage();

    const count = await screen.findByTitle("Last watched a pick 5d ago");
    expect(count).toHaveTextContent(/^3$/);
  });

  it("shows 0 in 30 days, not a dash, when picks exist but none were watched lately", async () => {
    getUsers.mockResolvedValue([
      { ...SARAH, picks_watched_30d: 0, last_pick_watched_at: null },
    ]);

    renderPage();

    const count = await screen.findByTitle("Hasn’t watched a pick yet");
    expect(count).toHaveTextContent(/^0$/);
  });

  it("shows a dash for a person who has never had a pick", async () => {
    getUsers.mockResolvedValue([SARAH]);

    renderPage();

    expect(await screen.findByText("sarah")).toBeInTheDocument();
    expect(await screen.findByTitle("Hasn’t had a pick yet")).toHaveTextContent(/^—$/);
    expect(screen.queryByTitle(/Hasn’t watched a pick yet/)).toBeNull();
  });

  it("labels the state column Status", async () => {
    getUsers.mockResolvedValue([SARAH]);

    renderPage();

    expect(await screen.findByRole("columnheader", { name: "Status" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Rebuilding" })).toBeNull();
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

    await userEvent.click(screen.getByRole("button", { name: "All users…" }));

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

    await userEvent.click(screen.getByRole("button", { name: "All users…" }));

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
      await screen.findByRole("button", { name: /Add people/i }),
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

    await userEvent.click(await screen.findByRole("button", { name: /Add people/ }));

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
      await screen.findByRole("button", { name: /Add people/i }),
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
    // A requests-only person with thin history is a cold start too, and their Your requests row is
    // delivered regardless — so the tooltip must not say they get "no row at all".
    expect(badge).toHaveAttribute(
      "title",
      expect.stringContaining("A Your requests row is unaffected"),
    );
    expect(badge.getAttribute("title")).not.toContain("no row at all");
  });
});

describe("UsersPage — the Requests column", () => {
  beforeEach(() => {
    getUsers.mockReset();
    patchUser.mockReset();
    getRequestRowSources.mockReset();
  });

  const KID: User = {
    ...SARAH,
    id: 5,
    username: "kid",
    slug: "kid",
    user_type: "managed",
  };

  it("asks for the sources ONCE for the page, not once per person", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE, KID]);
    getRequestRowSources.mockResolvedValue(sources([]));

    renderPage();

    expect(await screen.findByText("sarah")).toBeInTheDocument();
    await waitFor(() => expect(getRequestRowSources).toHaveBeenCalled());
    expect(getRequestRowSources).toHaveBeenCalledTimes(1);
    expect(getRequestRowSources).toHaveBeenCalledWith("");
  });

  it("says who is linked to Overseerr and how many of their requests are ready", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    getRequestRowSources.mockResolvedValue(
      sources([
        { user_id: SARAH.id, linked: true, ready: 2 },
        { user_id: MIKE.id, linked: false, ready: 0 },
      ]),
    );

    renderPage();

    const linked = await screen.findByText("Linked");
    expect(linked.closest("td")).toHaveTextContent("2 ready");
    const none = screen.getByText("No account");
    expect(none.closest("td")).toHaveTextContent(
      "Hasn’t signed in to Overseerr",
    );
  });

  it("does not tell a managed profile to sign in — it can't", async () => {
    getUsers.mockResolvedValue([KID]);
    getRequestRowSources.mockResolvedValue(
      sources([{ user_id: KID.id, linked: false, ready: 0 }]),
    );

    renderPage();

    const badge = await screen.findByText("Can’t use Overseerr");
    expect(badge.closest("td")).toHaveTextContent(
      "Managed profiles can’t sign in to it",
    );
    expect(screen.queryByText("No account")).toBeNull();
  });

  it("shows a person's own request tag beside their Overseerr state", async () => {
    getUsers.mockResolvedValue([{ ...KID, requested_by_tag: "children" }]);
    getRequestRowSources.mockResolvedValue(
      sources([{ user_id: KID.id, linked: false, ready: 0 }]),
    );

    renderPage();

    expect(await screen.findByText("Tag: children")).toBeInTheDocument();
    expect(screen.getByText("Can’t use Overseerr")).toBeInTheDocument();
  });

  it("shows a dash that says why when Overseerr can't be read", async () => {
    getUsers.mockResolvedValue([SARAH]);
    getRequestRowSources.mockRejectedValue(new ApiError(502, "Bad gateway"));

    renderPage();

    expect(await screen.findByText("sarah")).toBeInTheDocument();
    // Nothing came back at all, so nothing says which source is to blame.
    expect(
      await screen.findByTitle("Couldn’t read the request sources"),
    ).toHaveTextContent("—");
    expect(screen.queryByText("No account")).toBeNull();
  });

  it("blames the Arrs, not Overseerr, when the failed read had only Radarr and Sonarr to talk to", async () => {
    // A refetch failed after an earlier success: the last answer still says which sources are
    // configured, so the dash names those rather than an Overseerr that was never connected.
    getUsers.mockResolvedValue([SARAH]);
    getRequestRowSources.mockRejectedValue(new ApiError(502, "Bad gateway"));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    client.setQueryData(
      queryKeys.requestRowSources("", null),
      sources([{ user_id: SARAH.id, linked: false, ready: 0 }], {
        overseerr: "off",
        radarr: "connected",
        sonarr: "unreachable",
      }),
      { updatedAt: 0 }, // stale, so the page refetches on mount — and that refetch fails
    );

    renderPage(client);

    expect(
      await screen.findByTitle("Couldn’t read Radarr/Sonarr"),
    ).toHaveTextContent("—");
    expect(screen.queryByTitle("Couldn’t read Overseerr")).toBeNull();
  });

  it("says Overseerr is down rather than that nobody has an account", async () => {
    // The endpoint never fails: a configured-but-down Overseerr answers 200 with nobody linked. Read
    // literally, that is every shared person lacking an account and every managed one unable to get
    // one — blame for an outage that is not theirs.
    getUsers.mockResolvedValue([SARAH, KID]);
    getRequestRowSources.mockResolvedValue(
      sources(
        [
          { user_id: SARAH.id, linked: false, ready: 0 },
          { user_id: KID.id, linked: false, ready: 0 },
        ],
        { overseerr: "unreachable" },
      ),
    );

    renderPage();

    const dashes = await screen.findAllByLabelText("Couldn’t read Overseerr");
    expect(dashes).toHaveLength(2);
    for (const dash of dashes) expect(dash).toHaveTextContent("—");
    expect(screen.queryByText("No account")).toBeNull();
    expect(screen.queryByText("Can’t use Overseerr")).toBeNull();
  });

  it("says no request source is connected once, at the top, rather than once per person", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    getRequestRowSources.mockResolvedValue(
      sources([{ user_id: SARAH.id, linked: false, ready: 0 }], {
        overseerr: "off",
        radarr: "off",
        sonarr: "off",
      }),
    );

    renderPage();

    expect(
      await screen.findAllByText(/No request source is connected/),
    ).toHaveLength(1);
    expect(screen.getByRole("link", { name: /Set one up in Connections/ })).toHaveAttribute(
      "href",
      "/settings/connections",
    );
    expect(screen.queryByTitle("No request source connected")).toBeNull();
    expect(screen.queryByText("No account")).toBeNull();
  });

  it("does not blame the person for a missing account when only Radarr/Sonarr are connected", async () => {
    // Overseerr is off, so nobody can be "linked" to it; the tag is the only source that applies —
    // and titles credited by the tag still count as ready.
    getUsers.mockResolvedValue([
      { ...SARAH, requested_by_tag: "sarah-asked" },
      MIKE,
    ]);
    getRequestRowSources.mockResolvedValue(
      sources([{ user_id: SARAH.id, linked: false, ready: 3 }], {
        overseerr: "off",
        radarr: "connected",
      }),
    );

    renderPage();

    const tag = await screen.findByText("Tag: sarah-asked");
    expect(tag.closest("td")).toHaveTextContent("3 ready");
    expect(screen.queryByText("No account")).toBeNull();
    // Mike has no tag and nothing ready, so with Overseerr off his line is absent, not a dash.
    expect(screen.queryByTitle("Overseerr isn’t connected")).toBeNull();
    expect(screen.queryByText(/isn’t connected/)).toBeNull();
    const mikeRow = screen.getByText("mike").closest("tr") as HTMLElement;
    expect(within(mikeRow).queryByText("Request status:")).toBeNull();
  });

  it("holds a skeleton in the cell while the sources are still being read", async () => {
    getUsers.mockResolvedValue([SARAH]);
    getRequestRowSources.mockReturnValue(new Promise(() => {}));

    renderPage();

    const name = await screen.findByText("sarah");
    const row = name.closest("tr") as HTMLElement;
    expect(
      within(row).getByTestId("requests-loading"),
    ).toBeInTheDocument();
    expect(within(row).queryByText("No account")).toBeNull();
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

  it("excludes Plex-restricted accounts from Active even when enabled is stored", async () => {
    const ui = userEvent.setup();
    getUsers.mockResolvedValue([SARAH, { ...managed("Younger Kid"), enabled: true }]);
    renderPage();
    await screen.findByRole("link", { name: "kid" });
    await ui.click(screen.getByRole("button", { name: /^On/ }));
    expect(screen.getByRole("link", { name: "sarah" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "kid" })).not.toBeInTheDocument();
    await ui.click(screen.getByRole("button", { name: /^Needs attention/ }));
    expect(screen.getByRole("link", { name: "kid" })).toBeVisible();
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

  it("flags an account that can see other people's rows", async () => {
    // The Users list is where an owner scans, so the one account with a live privacy exposure has to
    // be distinguishable HERE — not only after clicking into it. Plex refuses a share filter for a
    // profiled account, so nothing Shortlist does can hide those rows; saying so is all it can do.
    getUsers.mockResolvedValue([managed("older_kid")]);
    getPrivacyStatus.mockResolvedValue(
      privacy([{ user_id: 9, state: "refused_by_plex", missing: ["shortlist_sarah", "shortlist_mike", "shortlist_jess"] }]),
    );
    renderPage();

    expect(await screen.findByText(/can see 3 rows/i)).toBeInTheDocument();
  });

  it("does not flag a profiled account that sees nothing", async () => {
    // `little_kid` genuinely sees no collections: a run looked through its eyes and saw none. A badge
    // on every profiled account would train the owner to ignore the one that matters.
    getUsers.mockResolvedValue([managed("little_kid")]);
    getPrivacyStatus.mockResolvedValue({
      ...privacy([{ user_id: 9, state: "refused_by_plex", missing: ["shortlist_sarah", "shortlist_mike"] }]),
      enforcement: { unhideable_measured: true, unhideable: {} } as unknown as PrivacyStatus["enforcement"],
    });
    renderPage();

    await screen.findByText("Younger Kid");
    expect(await screen.findByText("Won’t accept hide rules")).toBeInTheDocument();
    expect(screen.queryByText(/can see \d+ row/i)).toBeNull();
  });

  it("names the actual restriction profile rather than a bare 'Restricted'", async () => {
    // "Younger Kid" tells the owner what they set and therefore what to change; "Restricted" does not.
    getUsers.mockResolvedValue([managed("little_kid")]);
    renderPage();

    expect(await screen.findByText("Younger Kid")).toBeInTheDocument();
  });

  it("gates the toggle only for an account Plex really hides everything from", async () => {
    getUsers.mockResolvedValue([managed("little_kid")]);
    renderPage();

    await screen.findByText("Younger Kid");
    const toggle = screen.getByRole("switch");
    // NOT the native `disabled` attribute. It drops the control out of the tab order and takes its
    // explanation with it, which is how this shipped saying nothing at all to a keyboard or a
    // screen reader — the reason was in a `title`, reachable only by hovering a mouse over it.
    expect(toggle).not.toBeDisabled();
    expect(toggle).toHaveAttribute("aria-disabled", "true");
  });

  it("says WHY that toggle is gated, in text assistive tech can reach", async () => {
    getUsers.mockResolvedValue([managed("little_kid")]);
    renderPage();

    await screen.findByText("Younger Kid");
    const reasonId =
      screen.getByRole("switch").getAttribute("aria-describedby") ?? "";
    expect(reasonId).not.toBe("");
    expect(document.getElementById(reasonId)?.textContent).toMatch(
      /Younger Kid restriction profile.*Settings → Users & Sharing/s,
    );
  });

  it("ignores a click on the gated toggle instead of sending a patch Plex would defeat", async () => {
    // `aria-disabled` is advisory: Radix still fires the change event, so the guard has to be real.
    getUsers.mockResolvedValue([managed("little_kid")]);
    renderPage();

    await screen.findByText("Younger Kid");
    await userEvent.click(screen.getByRole("switch"));

    expect(patchUser).not.toHaveBeenCalled();
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
});

/** The Users card (design refresh, app-users): one state vocabulary, a Privacy column read from the
 *  same live reading as the Privacy page, a Rows column, and bulk actions behind a selection mode. */
describe("UsersPage — one state vocabulary and the privacy column", () => {
  beforeEach(() => {
    getUsers.mockReset();
    getRequestRowSources.mockReset();
    getRequestRowSources.mockResolvedValue(sources([]));
  });

  const KID: User = {
    ...SARAH,
    id: 9,
    username: "kid",
    slug: "kid",
    user_type: "managed",
    restricted: true,
    restriction_profile: "older_kid",
    enabled: true,
  };

  it("says On, Paused or Off for every person", async () => {
    getUsers.mockResolvedValue([
      SARAH,
      { ...MIKE, prefs: { paused: true } },
      { ...SARAH, id: 6, username: "jess", slug: "jess", enabled: false },
    ]);
    renderPage();

    const stateOf = async (name: string) =>
      within((await screen.findByRole("link", { name })).closest("tr") as HTMLElement).getByTestId("user-state");
    expect(await stateOf("sarah")).toHaveTextContent(/^On$/);
    expect(await stateOf("mike")).toHaveTextContent(/^Paused$/);
    expect(await stateOf("jess")).toHaveTextContent(/^Off$/);
    expect(screen.queryByText("Active")).toBeNull();
  });

  it("shows a restriction profile as its own neutral pill, never as the state and never red", async () => {
    getUsers.mockResolvedValue([KID]);
    renderPage();

    const profile = await screen.findByText("Older Kid");
    const pill = profile.closest("[data-testid='restricted-pill']") as HTMLElement;
    expect(pill).toHaveTextContent("Restricted · Older Kid");
    expect(pill.className).not.toMatch(/destructive/);
    // Plex refuses hide rules for a profiled account, so no row is built for it: the state is Off,
    // the same answer its switch gives.
    const tr = profile.closest("tr") as HTMLElement;
    expect(within(tr).getByTestId("user-state")).toHaveTextContent(/^Off$/);
  });

  it("filters by All, Needs attention, On, Paused and Off, with counts", async () => {
    getUsers.mockResolvedValue([SARAH, { ...MIKE, prefs: { paused: true } }, KID]);
    renderPage();
    await screen.findByRole("link", { name: "sarah" });

    const filters = screen.getByRole("group", { name: "Show" });
    expect(within(filters).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "All3",
      "Needs attention1",
      "On1",
      "Paused1",
      "Off1",
    ]);

    await userEvent.click(within(filters).getByRole("button", { name: /^Off/ }));
    expect(screen.getByRole("link", { name: "kid" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "sarah" })).toBeNull();
  });

  it("reads each person's privacy from the live plex.tv reading, in the Privacy page's words", async () => {
    getUsers.mockResolvedValue([SARAH, KID]);
    getPrivacyStatus.mockResolvedValue(
      privacy([
        { user_id: SARAH.id, state: "hiding" },
        { user_id: KID.id, state: "refused_by_plex" },
      ]),
    );
    renderPage();

    const sarahRow = (await screen.findByRole("link", { name: "sarah" })).closest("tr") as HTMLElement;
    expect(await within(sarahRow).findByText("Hiding every row")).toBeInTheDocument();
    const kidRow = screen.getByRole("link", { name: "kid" }).closest("tr") as HTMLElement;
    expect(within(kidRow).getByText("Won’t accept hide rules")).toBeInTheDocument();
  });

  it("puts how many rows a person can see under the privacy state that explains it", async () => {
    getUsers.mockResolvedValue([KID]);
    getPrivacyStatus.mockResolvedValue(
      privacy([{ user_id: KID.id, state: "refused_by_plex", missing: ["shortlist_sarah", "shortlist_mike", "shortlist_jess"] }]),
    );
    renderPage();

    const link = await screen.findByRole("link", { name: /kid can see 3 rows that aren’t theirs/ });
    expect(link).toHaveTextContent("Fix in Plex");
    expect(link).toHaveAttribute("href", `/users/${KID.id}`);
    const cell = link.closest("td") as HTMLElement;
    expect(cell).toHaveTextContent("Can see 3 rows · Fix in Plex");
    expect(cell.className).toMatch(/bg-warning/);
  });

  it("counts rows the way the Dashboard and Privacy do, not the run's per-library collections", async () => {
    // The run measured five COLLECTIONS (each row once per library); the live reading says three ROWS,
    // the figure the Dashboard and the Privacy page's "Hides 0 of 3 rows" print. One account, one number.
    getUsers.mockResolvedValue([{ ...KID, unhidden_rows: 5 }]);
    getPrivacyStatus.mockResolvedValue(
      privacy([{ user_id: KID.id, state: "refused_by_plex", missing: ["shortlist_sarah", "shortlist_mike", "shortlist_jess"] }]),
    );
    renderPage();

    const kidRow = (await screen.findByRole("link", { name: "kid" })).closest("tr") as HTMLElement;
    expect(await within(kidRow).findByText("Can see 3 rows")).toBeInTheDocument();
    expect(within(kidRow).queryByText(/5 rows/)).toBeNull();
  });

  it("says the privacy reading failed rather than leaving the column blank", async () => {
    getUsers.mockResolvedValue([SARAH]);
    getPrivacyStatus.mockRejectedValue(new ApiError(502, "plex.tv timed out"));
    renderPage();

    const sarahRow = (await screen.findByRole("link", { name: "sarah" })).closest("tr") as HTMLElement;
    expect(await within(sarahRow).findByText("Couldn’t read")).toBeInTheDocument();
  });

  it("counts the rows each person is in, and none for someone who is off", async () => {
    getUsers.mockResolvedValue([SARAH, { ...MIKE, enabled: false }]);
    listCollections.mockResolvedValue([
      row(1),
      row(2, { audience: "subset", audience_user_ids: [SARAH.id] }),
      row(3, { audience: "subset", audience_user_ids: [MIKE.id] }),
      row(4, { enabled: false }),
    ]);
    renderPage();

    const sarahRow = (await screen.findByRole("link", { name: "sarah" })).closest("tr") as HTMLElement;
    await waitFor(() => expect(within(sarahRow).getByTestId("user-rows")).toHaveTextContent(/^2$/));
    const mikeRow = screen.getByRole("link", { name: "mike" }).closest("tr") as HTMLElement;
    expect(within(mikeRow).getByTestId("user-rows")).toHaveTextContent(/^—$/);
  });

  it("keeps the bulk controls out of the way until you choose to select people", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    renderPage();
    await screen.findByRole("link", { name: "sarah" });

    expect(screen.queryByRole("checkbox", { name: "Select sarah" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Select people" }));
    expect(screen.getByRole("checkbox", { name: "Select sarah" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Done selecting" }));
    expect(screen.queryByRole("checkbox", { name: "Select sarah" })).toBeNull();
  });

  it("pulls new people from Plex with the page's one primary action", async () => {
    getUsers.mockResolvedValue([SARAH]);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Add people" }));

    await waitFor(() => expect(syncUsers).toHaveBeenCalledTimes(1));
  });

  it("says what pausing really does: rows come off Home, nothing is deleted", async () => {
    // users.py: pausing queues `user.pause.hide`, which demotes the rows off every surface at once —
    // the collections stay. "Rows stay on Plex exactly as they are" would be wrong.
    getUsers.mockResolvedValue([SARAH]);
    renderPage();

    expect(await screen.findByText(/come off Home and Recommended/)).toBeInTheDocument();
  });
});
