import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import type { RowSources, User, UserPatch } from "@/lib/types";
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
  getReport,
  getRequestRowSources,
  patchUser,
  removeUser,
  setAllUsersEnabled,
  syncUsers,
} = vi.hoisted(() => ({
  getUsers: vi.fn(),
  getReport: vi.fn(),
  getRequestRowSources: vi.fn(),
  patchUser: vi.fn(),
  removeUser: vi.fn(),
  syncUsers: vi.fn(() =>
    Promise.resolve({ added: 1, updated: 48, total: 49, queued: false }),
  ),
  setAllUsersEnabled: vi.fn((_enabled: boolean) =>
    Promise.resolve({ updated: 1, cleaned: 0, enabled: true }),
  ),
}));

/** The report, reduced to the two fields `useHitRatesMatured` reads. `firstPickDaysAgo` decides
 *  whether any pick on the server has had its 30 days yet. */
function report(firstPickDaysAgo: number | null) {
  return {
    first_pick:
      firstPickDaysAgo === null
        ? null
        : new Date(Date.now() - firstPickDaysAgo * 86_400_000).toISOString(),
    overall: { landing: { matured_days: 30 } },
  };
}

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
      getReport: (window: string) => getReport(window),
      getRequestRowSources: (pattern: string) => getRequestRowSources(pattern),
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
    getReport.mockReset();
    // Most tests here are not about the hit-rate column; a long-running install is the state that
    // leaves every other assertion unchanged.
    getReport.mockResolvedValue(report(400));
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
    await userEvent.click(screen.getByRole("button", { name: /^Active/ }));
    expect(screen.getByRole("link", { name: "sarah" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Michael" })).not.toBeInTheDocument();
  });

  it("shows request and viewing context directly without repeating the account type", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, hit_rate: 0.25 }]);
    getRequestRowSources.mockResolvedValue(sources([{ user_id: SARAH.id, linked: true, ready: 3 }]));
    renderPage();
    expect(await screen.findByText("Linked")).toBeVisible();
    expect(screen.getByText("3 ready")).toBeVisible();
    expect(screen.getByText("25%", { exact: true })).toBeVisible();
    expect(screen.getAllByText("Shared", { exact: true })).toHaveLength(1);
    expect(screen.queryByText("Shared account", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText("Requests & results", { exact: true })).not.toBeInTheDocument();
  });

  it("pauses only selected people without turning them off", async () => {
    getUsers.mockResolvedValue([SARAH, MIKE]);
    patchUser.mockResolvedValue(SARAH);
    renderPage();
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
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select visible users" }));
    await userEvent.click(screen.getByRole("button", { name: "Pause rebuilding" }));
    expect(await screen.findByText(/1 person couldn’t be updated/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select mike" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select sarah" })).not.toBeChecked();
  });

  // `hit_rate` is watched-over-delivered across all time, so on a fresh install it is 0 for
  // everyone and the column read "0%" down the page — which says "nobody watches any of this" when
  // the truth is that no pick has had time to be watched. The dashboard already withholds its own
  // landing rate on exactly this rule.
  it("shows an em dash rather than 0% until picks have had their 30 days", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, hit_rate: 0 }]);
    getReport.mockResolvedValue(report(3));

    renderPage();

    expect(await screen.findByText("sarah")).toBeInTheDocument();
    await waitFor(() => expect(getReport).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("0%")).toBeNull());
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows a real 0% once the earliest picks are old enough for it to mean something", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, hit_rate: 0 }]);
    getReport.mockResolvedValue(report(45));

    renderPage();

    expect(await screen.findByText("sarah")).toBeInTheDocument();
    expect(await screen.findByText("0%")).toBeInTheDocument();
  });

  it("never hides a rate somebody has actually earned, however new the install", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, hit_rate: 0.5 }]);
    getReport.mockResolvedValue(report(1));

    renderPage();

    expect(await screen.findByText("50%")).toBeInTheDocument();
    expect(screen.queryByText(/Picks watched \(too early\)/)).not.toBeInTheDocument();
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
    getReport.mockReset();
    getReport.mockResolvedValue(report(400));
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
      queryKeys.requestRowSources(""),
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

  it("shows a dash that says so when no request source is connected", async () => {
    getUsers.mockResolvedValue([SARAH]);
    getRequestRowSources.mockResolvedValue(
      sources([{ user_id: SARAH.id, linked: false, ready: 0 }], {
        overseerr: "off",
        radarr: "off",
        sonarr: "off",
      }),
    );

    renderPage();

    expect(
      await screen.findByTitle("No request source connected"),
    ).toHaveTextContent("—");
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
    // Only Mike, with no tag and nothing ready, gets the dash.
    const dashes = screen.getAllByTitle("Overseerr isn’t connected");
    expect(dashes).toHaveLength(1);
    expect(dashes[0]).toHaveTextContent("—");
    expect(dashes[0]).toHaveAttribute("aria-label", "Overseerr isn’t connected");
    expect(dashes[0]?.closest("tr")).toHaveTextContent("mike");
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
    await ui.click(screen.getByRole("button", { name: /^Active/ }));
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
