import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RequestCandidate, User } from "@/lib/types";
import { RequestsPage } from "@/pages/requests";

const {
  listRequests,
  sendRequests,
  rejectRequests,
  deleteRequests,
  restoreRequests,
  clearRequests,
  getSettings,
  getUsers,
  getArrStatus,
} = vi.hoisted(() => ({
  listRequests: vi.fn(),
  getUsers: vi.fn((): Promise<unknown[]> => Promise.resolve([])),
  sendRequests: vi.fn((_ids: number[], _dryRun?: boolean) =>
    Promise.resolve({ sent: 1, dry_run: false, outcomes: [] }),
  ),
  rejectRequests: vi.fn((_ids: number[]) => Promise.resolve({ rejected: 1 })),
  deleteRequests: vi.fn((_ids: number[]) => Promise.resolve({ deleted: 1 })),
  restoreRequests: vi.fn((ids: number[]) =>
    Promise.resolve({ restored: ids.length }),
  ),
  clearRequests: vi.fn((ids: number[]) =>
    Promise.resolve({ cleared: ids.length }),
  ),
  getSettings: vi.fn((): Promise<Record<string, unknown>> =>
    Promise.resolve({ "requests.enabled": true }),
  ),
  getArrStatus: vi.fn((): Promise<unknown> =>
    Promise.resolve({ statuses: {}, radarr: "off", sonarr: "off" }),
  ),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    // The names go through: the "Wanted by" filter is the SERVER's, applied before its 500-row cap,
    // so a test that swallowed the argument could not tell it apart from filtering the loaded page.
    listRequests: (wantedBy?: string[]) => listRequests(wantedBy),
    sendRequests: (ids: number[], dryRun?: boolean) =>
      sendRequests(ids, dryRun),
    rejectRequests: (ids: number[]) => rejectRequests(ids),
    deleteRequests: (ids: number[]) => deleteRequests(ids),
    restoreRequests: (ids: number[]) => restoreRequests(ids),
    clearRequests: (ids: number[]) => clearRequests(ids),
    getSettings: () => getSettings(),
    getUsers: () => getUsers(),
    getArrStatus: () => getArrStatus(),
  },
}));

/** A users-list row, for the username → display-name resolution the inbox does client-side. */
function person(username: string, displayName: string): User {
  return {
    manage_sharing: true,
    id: username.length,
    plex_account_id: 0,
    username,
    slug: username,
    nickname: "",
    friendly_name: "",
    display_name: displayName,
    avatar_url: "",
    user_type: "shared",
    restricted: false,
    restriction_profile: "",
    unhidden_rows: 0,
    departed: false,
    enabled: true,
    cold_start: false,
    request_tag: "",
    prefs: {},
    history_depth: 0,
    last_run_at: null,
    hit_rate: null,
    preview_titles: [],
  };
}

function candidate(
  overrides: Partial<RequestCandidate> = {},
): RequestCandidate {
  return {
    id: 1,
    tmdb_id: 100,
    media_type: "movie",
    title: "Dune: Part Two",
    year: 2024,
    imdb_id: "",
  language: "",
    poster_path: "",
    overview: "",
    rating: 8.3,
    vote_count: 5000,
    demand: 4,
    tags: [],
    wanters: [],
    why: [],
    status: "pending",
    detail: "",
    excluded: false,
    arr_slug: null,
    updated_at: null,
    ...overrides,
  };
}

function renderPage(initialEntry = "/requests") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <RequestsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Pick someone in the "Wanted by" box.
 *
 * It was a row of buttons, one per person; with forty sharers that was a wall, so it is now a search
 * box with a list. Tests go through the same two steps a person does: open the box, click the name.
 */
async function pickPerson(name: string) {
  await userEvent.click(screen.getByRole("combobox", { name: /Wanted by/i }));
  await userEvent.click(
    await screen.findByRole("option", { name: new RegExp(`^${name},`) }),
  );
}

/** The bulk toolbar, which acts on the ticked rows. Every action name it carries — Send, Delete,
 *  Reject — also appears on each card's own button group, so an unscoped `getByRole("button", …)`
 *  is ambiguous the moment a second title is on screen. Scope to the group that is under test. */
function toolbar() {
  return within(
    screen.getByRole("group", { name: "Actions for the selected titles" }),
  );
}

/** The action group on one card, which acts on that title alone. */
function rowActions(title: string) {
  return within(screen.getByRole("group", { name: `Actions for ${title}` }));
}

describe("RequestsPage", () => {
  beforeEach(() => {
    listRequests.mockReset();
    sendRequests.mockClear();
    rejectRequests.mockClear();
    deleteRequests.mockClear();
    restoreRequests.mockClear();
    clearRequests.mockClear();
    getSettings.mockResolvedValue({ "requests.enabled": true });
    getUsers.mockResolvedValue([]);
    getArrStatus.mockReset();
    getArrStatus.mockResolvedValue({
      statuses: {},
      radarr: "off",
      sonarr: "off",
    });
  });

  it("shows an empty state when nothing has ever been queued", async () => {
    listRequests.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/Nothing waiting/i)).toBeTruthy();
  });

  it("warns when a waiting title is on the arr's exclusion list, naming the right app", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "Amazing Digital Circus",
        media_type: "show",
        excluded: true,
      }),
    ]);
    renderPage();
    expect(await screen.findByText("Amazing Digital Circus")).toBeTruthy();
    expect(
      screen.getByText(/Sonarr was told never to add this again/i),
    ).toBeTruthy();
    // The Arr's own word for it stays, so the owner can find the setting there.
    expect(screen.getByText(/import exclusion/i)).toBeTruthy();
  });

  it("shows a distinct 'off' empty state when requests are disabled", async () => {
    listRequests.mockResolvedValue([]);
    getSettings.mockResolvedValue({ "requests.enabled": false });
    renderPage();
    // Never implies auto-send is running; points the owner at Settings to turn it on.
    expect(await screen.findByText(/Requests are off/i)).toBeTruthy();
    expect(screen.getByText(/Go to Settings . Requests/i)).toBeTruthy();
  });

  it("files a sent title under the Sonarr/Radarr send log with its outcome and when", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        media_type: "show",
        status: "sent",
        detail: "added to Sonarr",
        updated_at: "2026-07-17T03:31:00Z",
      }),
    ]);
    renderPage();
    // The inbox opens on Waiting; the sent title lives behind the "Sent" tab (labelled with its count).
    expect(await screen.findByText("Dune: Part Two")).toBeTruthy();
    expect(screen.queryByText("Shogun")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Sent (1)" }));
    expect(screen.getByText("Shogun")).toBeTruthy();
    // The log is its own section, and each entry carries the app's answer (the outcome).
    expect(
      screen.getByRole("heading", { name: "Sent to Radarr & Sonarr" }),
    ).toBeTruthy();
    expect(screen.getByText(/added to Sonarr/i)).toBeTruthy();
  });

  it("clears a sent title from the send log (hides it, doesn't un-send)", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        media_type: "show",
        status: "sent",
      }),
    ]);
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: "Sent (1)" }),
    );
    await userEvent.click(screen.getByRole("button", { name: /^Clear$/i }));
    await waitFor(() => expect(clearRequests).toHaveBeenCalledWith([2]));
  });

  it("deep-links a sent show straight to its Sonarr series page via the captured slug", async () => {
    // Sonarr has no id-based URL, so the direct link needs the titleSlug captured at send time.
    getSettings.mockResolvedValueOnce({
      "requests.enabled": true,
      "requests.sonarr.url": "https://tv.stevez0.com",
    });
    listRequests.mockResolvedValue([
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        media_type: "show",
        status: "sent",
        arr_slug: "shogun",
      }),
    ]);
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: "Sent (1)" }),
    );
    const open = screen.getByRole("link", { name: /Open in Sonarr/i });
    expect((open as HTMLAnchorElement).href).toBe(
      "https://tv.stevez0.com/series/shogun",
    );
  });

  it("falls back to the Sonarr home for a legacy sent show with no captured slug", async () => {
    getSettings.mockResolvedValueOnce({
      "requests.enabled": true,
      "requests.sonarr.url": "https://tv.stevez0.com",
    });
    listRequests.mockResolvedValue([
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        media_type: "show",
        status: "sent",
        arr_slug: null, // sent before slugs were recorded — no dead /series/ link
      }),
    ]);
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: "Sent (1)" }),
    );
    const open = screen.getByRole("link", { name: /Open in Sonarr/i });
    expect((open as HTMLAnchorElement).href).toBe("https://tv.stevez0.com/");
  });

  it("deep-links a sent movie to its Radarr page (slug when captured, else TMDB id)", async () => {
    getSettings.mockResolvedValueOnce({
      "requests.enabled": true,
      "requests.radarr.url": "https://movies.stevez0.com",
    });
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 603,
        title: "The Matrix",
        media_type: "movie",
        status: "sent",
        arr_slug: "the-matrix-603",
      }),
    ]);
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: "Sent (1)" }),
    );
    const open = screen.getByRole("link", { name: /Open in Radarr/i });
    expect((open as HTMLAnchorElement).href).toBe(
      "https://movies.stevez0.com/movie/the-matrix-603",
    );
  });

  it("shows the send log on the Sent tab with a findable empty state before the first send", async () => {
    // The Sent tab is always offered so the log is reachable before anything's gone out — it
    // explains itself ("Nothing sent yet") rather than looking broken or missing.
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
    ]);
    renderPage();
    await screen.findByText("Dune: Part Two");
    await userEvent.click(screen.getByRole("button", { name: "Sent" }));
    expect(
      screen.getByRole("heading", { name: "Sent to Radarr & Sonarr" }),
    ).toBeTruthy();
    expect(screen.getByText(/Nothing sent yet/i)).toBeTruthy();
  });

  it("opens straight on the send log when deep-linked with ?tab=sent", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        status: "sent",
        detail: "added to Sonarr",
      }),
    ]);
    renderPage("/requests?tab=sent");
    // The dashboard's "View the full send log" lands here; the sent title is visible without a click.
    expect(await screen.findByText("Shogun")).toBeTruthy();
    expect(screen.queryByText("Dune: Part Two")).toBeNull();
  });

  it("opens on the Rejected tab when deep-linked (accepting the legacy ?tab=dismissed alias)", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Old Reject",
        status: "rejected",
      }),
    ]);
    // `?tab=dismissed` is the old name; it must still land on the renamed Rejected tab.
    renderPage("/requests?tab=dismissed");
    expect(await screen.findByText("Old Reject")).toBeTruthy();
    expect(screen.queryByText("Dune: Part Two")).toBeNull();
    expect(screen.getByRole("button", { name: "Rejected (1)" })).toBeTruthy();
  });

  it("falls back to Waiting when the deep-linked tab has no items to show", async () => {
    // The Rejected tab isn't even offered when nothing's rejected, so a stale `?tab=rejected`
    // link must land on Waiting rather than a blank view — the same guard that self-heals when a
    // selected tab's items age out.
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
    ]);
    renderPage("/requests?tab=rejected");
    expect(await screen.findByText("Dune: Part Two")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Rejected/ })).toBeNull();
  });

  it("splits the waiting queue by library (Movies / Shows) when both are present", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune", media_type: "movie" }),
      candidate({ id: 2, tmdb_id: 200, title: "Shogun", media_type: "show" }),
    ]);
    renderPage();
    await screen.findByText("Dune");
    expect(screen.getByText("Shogun")).toBeTruthy();
    // The media filter appears (with per-type counts) because the queue mixes both.
    await userEvent.click(screen.getByRole("button", { name: "Movies (1)" }));
    expect(screen.getByText("Dune")).toBeTruthy();
    expect(screen.queryByText("Shogun")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Shows (1)" }));
    expect(screen.getByText("Shogun")).toBeTruthy();
    expect(screen.queryByText("Dune")).toBeNull();
  });

  it("re-orders the waiting queue by rating when 'Top rated' is chosen", async () => {
    // Default sort is Recent (newest id first), so [Low(id 2), High(id 1)]; 'Top rated' flips it.
    listRequests.mockResolvedValue([
      candidate({ id: 1, tmdb_id: 100, title: "High Rated", rating: 9.1 }),
      candidate({ id: 2, tmdb_id: 200, title: "Low Rated", rating: 5.2 }),
    ]);
    renderPage();
    await screen.findByText("High Rated");
    await userEvent.selectOptions(screen.getByLabelText("Sort"), "Top rated");
    const high = screen.getByText("High Rated");
    const low = screen.getByText("Low Rated");
    // High is rendered before Low once sorted by rating.
    expect(
      high.compareDocumentPosition(low) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("hides titles below the chosen rating floor", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, tmdb_id: 100, title: "Acclaimed", rating: 9.1 }),
      candidate({ id: 2, tmdb_id: 200, title: "Middling", rating: 5.2 }),
    ]);
    renderPage();
    await screen.findByText("Acclaimed");
    expect(screen.getByText("Middling")).toBeTruthy();
    // Raise the floor to 8+ — the 5.2 title drops out, the 9.1 stays.
    await userEvent.selectOptions(screen.getByLabelText("Rating"), "8+");
    expect(screen.getByText("Acclaimed")).toBeTruthy();
    expect(screen.queryByText("Middling")).toBeNull();
  });

  it("hides thinly-voted titles below the chosen vote floor", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Well Attested",
        vote_count: 4200,
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Barely Rated", vote_count: 12 }),
    ]);
    renderPage();
    await screen.findByText("Well Attested");
    expect(screen.getByText("Barely Rated")).toBeTruthy();
    // A high score on 12 votes is noise — the 500+ floor drops it.
    await userEvent.selectOptions(screen.getByLabelText("Votes"), "500+");
    expect(screen.getByText("Well Attested")).toBeTruthy();
    expect(screen.queryByText("Barely Rated")).toBeNull();
  });

  it("shows the poster from TMDB's image CDN, and a placeholder when there is none", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Has Art", poster_path: "/abc.jpg" }),
      candidate({ id: 2, tmdb_id: 200, title: "No Art", poster_path: "" }),
    ]);
    renderPage();
    await screen.findByText("Has Art");

    // Built from the stored PATH — the host and size bucket are the UI's call, not the database's.
    const posters = Array.from(document.querySelectorAll("img"));
    expect(posters).toHaveLength(1); // only the title that has artwork
    const [poster] = posters;
    expect(poster?.getAttribute("src")).toBe(
      "https://image.tmdb.org/t/p/w154/abc.jpg",
    );
    // Off-screen posters must not be fetched on load — a 40-title inbox would be megabytes.
    expect(poster?.getAttribute("loading")).toBe("lazy");
    // Decorative: the title sits beside it as real text, so it must not be announced twice.
    expect(poster?.getAttribute("alt")).toBe("");
    // The art-less title still renders (a placeholder tile), rather than vanishing or breaking.
    expect(screen.getByText("No Art")).toBeTruthy();
  });

  it("falls back to the placeholder when the poster fails to load", async () => {
    // TMDB's CDN is a third-party host: a restrictive network or an ad-blocker fails the request
    // long after the path looked valid. That must not leave a broken-image icon in every row.
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Has Art", poster_path: "/abc.jpg" }),
    ]);
    renderPage();
    await screen.findByText("Has Art");

    const poster = document.querySelector("img");
    expect(poster).toBeTruthy();
    fireEvent.error(poster as HTMLImageElement);
    await waitFor(() => expect(document.querySelector("img")).toBeNull());
    expect(screen.getByText("Has Art")).toBeTruthy(); // the row itself survives
  });

  it("says the filters emptied the queue rather than showing a blank list", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, tmdb_id: 100, title: "Middling", rating: 5.2 }),
      candidate({ id: 2, tmdb_id: 200, title: "Also Middling", rating: 5.4 }),
    ]);
    renderPage();
    await screen.findByText("Middling");
    await userEvent.selectOptions(screen.getByLabelText("Rating"), "9+");
    expect(screen.queryByText("Middling")).toBeNull();
    // Not a blank panel: it says how many are waiting and how to get them back.
    expect(
      screen.getByText(/No waiting title clears these filters/i),
    ).toBeTruthy();
    // ...and one click restores them.
    await userEvent.click(
      screen.getByRole("button", { name: "Clear filters" }),
    );
    expect(screen.getByText("Middling")).toBeTruthy();
  });

  it("filters the queue down to one person's requests (issue #61)", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["Sarah"],
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Mike Pick", wanters: ["Mike"] }),
      candidate({
        id: 3,
        tmdb_id: 300,
        title: "Shared Pick",
        wanters: ["Sarah", "Mike"],
      }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");
    // Each name carries how many of the titles on this tab they wanted.
    await pickPerson("Sarah");
    expect(screen.getByText("Sarah Pick")).toBeTruthy();
    expect(screen.getByText("Shared Pick")).toBeTruthy(); // Sarah wanted this one too
    expect(screen.queryByText("Mike Pick")).toBeNull();
  });

  it("asks the server for the picked names instead of filtering the loaded page", async () => {
    const loaded = [
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["Sarah"],
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Mike Pick", wanters: ["Mike"] }),
    ];
    // The server's answer for Sarah carries a title the first read never returned — the shape the
    // 500-row cap creates, and the one thing filtering the loaded page could never produce.
    listRequests.mockImplementation((wantedBy?: string[]) =>
      Promise.resolve(
        wantedBy?.length
          ? [
              loaded[0],
              candidate({
                id: 3,
                tmdb_id: 300,
                title: "Buried Sarah Pick",
                wanters: ["Sarah"],
              }),
            ]
          : loaded,
      ),
    );
    renderPage();
    await screen.findByText("Sarah Pick");
    await pickPerson("Sarah");

    expect(await screen.findByText("Buried Sarah Pick")).toBeTruthy();
    expect(listRequests).toHaveBeenCalledWith(["Sarah"]);
    expect(screen.queryByText("Mike Pick")).toBeNull();
    // Her chip is re-counted from that answer — "(1)" beside two of her titles would be a lie.
    expect(screen.getByText("Sarah (2)")).toBeTruthy();
  });

  it("says the page limit applies until a name is picked, then that it doesn't", async () => {
    // 498 rejected titles the Waiting tab never draws, plus two waiting ones: enough rows for the
    // server's 500-row cap to be in play without jsdom rendering five hundred cards.
    const filler = Array.from({ length: 498 }, (_, i) =>
      candidate({
        id: 1000 + i,
        tmdb_id: 1000 + i,
        title: `Old ${i}`,
        status: "rejected",
      }),
    );
    const waiting = [
      candidate({ id: 1, tmdb_id: 1, title: "Sarah Pick", wanters: ["Sarah"] }),
      candidate({ id: 2, tmdb_id: 2, title: "Mike Pick", wanters: ["Mike"] }),
    ];
    listRequests.mockImplementation((wantedBy?: string[]) =>
      Promise.resolve(
        wantedBy?.length
          ? [
              waiting[0],
              candidate({
                id: 3,
                tmdb_id: 3,
                title: "Buried Sarah Pick",
                wanters: ["Sarah"],
              }),
            ]
          : [...waiting, ...filler],
      ),
    );
    renderPage();
    await screen.findByText("Sarah Pick");
    expect(
      screen.getByText(/This page loads the first 500 titles/),
    ).toBeTruthy();

    await pickPerson("Sarah");
    await screen.findByText("Buried Sarah Pick");
    // The limit described the unfiltered read. It does not describe this one, so it must stop
    // being shown — that disclosure is what this change exists to retire.
    expect(
      screen.queryByText(/This page loads the first 500 titles/),
    ).toBeNull();
    expect(
      screen.getByText(/Showing every title on file for the name you picked/),
    ).toBeTruthy();
  });

  it("takes several people at once, showing anything any of them wanted", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["Sarah"],
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Mike Pick", wanters: ["Mike"] }),
      candidate({ id: 3, tmdb_id: 300, title: "Ann Pick", wanters: ["Ann"] }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");
    await pickPerson("Sarah");
    await pickPerson("Mike");
    // Union, not intersection — two people picked means "either of them", or the list would empty.
    expect(screen.getByText("Sarah Pick")).toBeTruthy();
    expect(screen.getByText("Mike Pick")).toBeTruthy();
    expect(screen.queryByText("Ann Pick")).toBeNull();
    // Un-ticking the last name goes back to everyone, rather than showing nothing.
    await pickPerson("Sarah");
    await pickPerson("Mike");
    expect(screen.getByText("Ann Pick")).toBeTruthy();
  });

  it("marks the picked names as pressed and clears them with 'Clear filters'", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["Sarah"],
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Mike Pick", wanters: ["Mike"] }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");
    // Nobody picked yet, so there is no chip and everything is on screen.
    expect(screen.queryByText("Sarah (1)")).toBeNull();

    await pickPerson("Sarah");
    expect(screen.getByText("Sarah (1)")).toBeTruthy(); // she is now a chip
    expect(screen.queryByText("Mike Pick")).toBeNull();

    // The same "Clear filters" control that resets the rating/vote floors also drops the names.
    await userEvent.click(
      screen.getByRole("button", { name: "Clear filters" }),
    );
    expect(screen.getByText("Mike Pick")).toBeTruthy();
    expect(screen.queryByText("Sarah (1)")).toBeNull();
  });

  it("takes a person back off with the x on their chip", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["Sarah"],
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Mike Pick", wanters: ["Mike"] }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");
    await pickPerson("Sarah");
    expect(screen.queryByText("Mike Pick")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: "Stop filtering by Sarah" }),
    );

    expect(await screen.findByText("Mike Pick")).toBeTruthy();
  });

  it("says the people filter emptied the tab rather than showing a blank list", async () => {
    // Sarah has nothing waiting, only something sent — picking her on the Sent tab and switching
    // is impossible (tab changes reset the filter), so drive it from a rating floor instead: the
    // point is that an emptied list explains itself for every tab, not just Waiting.
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sent Low",
        status: "sent",
        rating: 5.2,
      }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Sent Lower",
        status: "sent",
        rating: 5.0,
      }),
    ]);
    renderPage("/requests?tab=sent");
    await screen.findByText("Sent Low");
    await userEvent.selectOptions(screen.getByLabelText("Rating"), "9+");
    // NOT "Nothing sent yet" — two titles are on file and one control brings them back.
    expect(screen.queryByText(/Nothing sent yet/i)).toBeNull();
    expect(
      screen.getByText(/No sent title clears these filters/i),
    ).toBeTruthy();
    expect(screen.getByText(/2 are on this tab in total/i)).toBeTruthy();
  });

  it("offers no 'Wanted by' filter when a single person wanted everything", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, tmdb_id: 100, title: "One", wanters: ["Sarah"] }),
      candidate({ id: 2, tmdb_id: 200, title: "Two", wanters: ["Sarah"] }),
    ]);
    renderPage();
    await screen.findByText("One");
    // Filtering to the only person there is would hide nothing, so the control isn't drawn.
    expect(screen.queryByRole("button", { name: /^Sarah/ })).toBeNull();
  });

  it("drops a name from the filter once nothing on the tab carries it", async () => {
    // Sarah's only title is sent while her name is ticked. Her chip goes with it, so filtering on
    // her would empty the queue with no visible control to undo — the list falls back to everyone.
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["Sarah"],
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Mike Pick", wanters: ["Mike"] }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");
    await pickPerson("Sarah");
    expect(screen.queryByText("Mike Pick")).toBeNull();

    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        status: "sent",
        wanters: ["Sarah"],
      }),
      candidate({ id: 2, tmdb_id: 200, title: "Mike Pick", wanters: ["Mike"] }),
    ]);
    await userEvent.click(
      screen.getByRole("checkbox", { name: /Sarah Pick/i }),
    );
    await userEvent.click(toolbar().getByRole("button", { name: /^Delete/i }));
    await waitFor(() => expect(screen.getByText("Mike Pick")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /^Sarah/ })).toBeNull();
  });

  it("offers no library split when the queue is a single media type", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune", media_type: "movie" }),
      candidate({ id: 2, tmdb_id: 200, title: "Fallout", media_type: "movie" }),
    ]);
    renderPage();
    await screen.findByText("Dune");
    // All movies — a Movies/Shows split would be noise, so it isn't rendered.
    expect(screen.queryByRole("button", { name: /^Movies/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Shows/ })).toBeNull();
  });

  it("keeps rejected titles on their own tab, offered only once something is rejected", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Old Reject",
        status: "rejected",
      }),
    ]);
    renderPage();
    await screen.findByText("Dune: Part Two");
    expect(screen.queryByText("Old Reject")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Rejected (1)" }));
    expect(screen.getByText("Old Reject")).toBeTruthy();
  });

  it("explains where a request came from: which person, which row, and why", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "El Chavo del Ocho",
        wanters: ["Sarah", "Mike"],
        why: [
          {
            user: "Sarah",
            row: "Comedy Classics",
            seed: "Fawlty Towers",
            source: "tmdb_similar",
          },
          {
            user: "Mike",
            row: "Sci-Fi Night",
            seed: "Futurama",
            source: "trakt",
          },
        ],
      }),
    ]);
    renderPage();
    await screen.findByText("El Chavo del Ocho");
    // Each (person, row) is spelled out with the reason, not just a bare "wanted by 2 people".
    expect(screen.getByText("Comedy Classics")).toBeTruthy();
    expect(
      screen.getByText(/because they watched Fawlty Towers/i),
    ).toBeTruthy();
    expect(screen.getByText("Sci-Fi Night")).toBeTruthy();
    expect(screen.getByText(/because they watched Futurama/i)).toBeTruthy();
  });

  it("collapses a long why-list to a few reasons with an expander", async () => {
    const why = Array.from({ length: 6 }, (_, i) => ({
      user: `person${i}`,
      row: "Comedy Classics",
      seed: "Fawlty Towers",
      source: "tmdb_similar",
    }));
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Popular Pick", why }),
    ]);
    renderPage();
    await screen.findByText("Popular Pick");
    // Only the first 3 reasons render; the rest are behind a "+3 more" toggle.
    expect(screen.getByText("person0")).toBeTruthy();
    expect(screen.queryByText("person5")).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: /\+3 more reasons/ }),
    );
    expect(screen.getByText("person5")).toBeTruthy();
  });

  it("shows how a seedless pick was suggested when there is no seed", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "Trending Thing",
        why: [
          {
            user: "Sarah",
            row: "Fresh Picks",
            seed: "",
            source: "tmdb_discover",
          },
        ],
      }),
    ]);
    renderPage();
    await screen.findByText("Trending Thing");
    // With no seed there is no "because they watched"; the row still explains how it was suggested.
    expect(screen.getByText("Fresh Picks")).toBeTruthy();
    expect(screen.getByText(/via /i)).toBeTruthy();
    expect(screen.queryByText(/because they watched/i)).toBeNull();
  });

  it("links each request out to TMDB, IMDb, and Trakt by the right media type", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune", tmdb_id: 438631, media_type: "movie" }),
      candidate({
        id: 2,
        title: "Shogun",
        tmdb_id: 202484,
        media_type: "show",
      }),
    ]);
    renderPage();
    await screen.findByText("Dune");

    const links = screen.getAllByRole("link");
    const href = (name: RegExp, path: string) =>
      links.find(
        (l) =>
          name.test(l.textContent ?? "") &&
          (l as HTMLAnchorElement).href.includes(path),
      );
    // A movie links to /movie/ on TMDB and id_type=movie on Trakt; a show to /tv/ and id_type=show.
    expect(href(/TMDB/, "themoviedb.org/movie/438631")).toBeTruthy();
    expect(
      href(/Trakt/, "trakt.tv/search/tmdb/438631?id_type=movie"),
    ).toBeTruthy();
    expect(href(/TMDB/, "themoviedb.org/tv/202484")).toBeTruthy();
    expect(
      href(/Trakt/, "trakt.tv/search/tmdb/202484?id_type=show"),
    ).toBeTruthy();
    // IMDb is a title search (no stored IMDb id).
    expect(href(/IMDb/, "imdb.com/find")).toBeTruthy();
  });

  it("names who wanted a title, and falls back to the count when none were recorded", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "With Names", wanters: ["Sarah", "Mike"] }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "No Names",
        demand: 3,
        wanters: [],
      }),
    ]);
    renderPage();
    expect(await screen.findByText(/Wanted by Sarah, Mike/)).toBeTruthy();
    expect(screen.getByText(/Wanted by 3 people/)).toBeTruthy();
  });

  it("shows the display name for each wanter, and the username itself when nobody matches", async () => {
    // `wanters` stores the bare Plex username; every other page shows `display_name || username`.
    getUsers.mockResolvedValue([person("sarah_p89", "Sarah")]);
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "Poor Things",
        wanters: ["sarah_p89", "ghost_account"],
        why: [
          {
            user: "sarah_p89",
            row: "Comedy Classics",
            seed: "Fawlty Towers",
            source: "tmdb_similar",
          },
        ],
      }),
    ]);
    renderPage();
    // Known username -> the friendly name; unknown one -> itself, never a blank.
    expect(
      await screen.findByText(/Wanted by Sarah, ghost_account/),
    ).toBeTruthy();
    // The why-line carries the same usernames, so it resolves them the same way.
    expect(screen.getByText("Sarah")).toBeTruthy();
    expect(screen.queryByText(/sarah_p89/)).toBeNull();
  });

  it("labels the 'Wanted by' chips with display names but still filters on the username", async () => {
    getUsers.mockResolvedValue([
      person("sarah_p89", "Sarah"),
      person("m_jones", "Mike"),
    ]);
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["sarah_p89"],
      }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Mike Pick",
        wanters: ["m_jones"],
      }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");
    // The chip reads as the person, not as their login...
    // The option reads as the person, not as their login...
    await userEvent.click(screen.getByRole("combobox", { name: /Wanted by/i }));
    expect(await screen.findByRole("option", { name: /^Sarah,/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /sarah_p89/ })).toBeNull();

    // ...and picking it still narrows the list, because the filter is keyed on the username.
    await userEvent.click(screen.getByRole("option", { name: /^Sarah,/ }));
    expect(screen.getByText("Sarah Pick")).toBeTruthy();
    expect(screen.queryByText("Mike Pick")).toBeNull();
  });

  it("offers people who have nothing on the page at all", async () => {
    // The names used to be inferred from the titles on screen, and the page loads at most 500. So
    // anyone whose requests were all older than that was missing from the picker — and the filter
    // itself would have found them perfectly well, if only they could be picked. The list of people
    // must not be limited by the page you happen to be looking at.
    getUsers.mockResolvedValue([
      person("sarah_p89", "Sarah"),
      person("quiet_one", "Quiet Pete"),
    ]);
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["sarah_p89"],
      }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Sarah Pick Two",
        wanters: ["sarah_p89"],
      }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");

    await userEvent.click(screen.getByRole("combobox", { name: /Wanted by/i }));
    const list = await screen.findByRole("listbox");

    // Pete wanted none of the loaded titles, but he is still there to pick...
    expect(
      within(list).getByRole("option", { name: "Quiet Pete" }),
    ).toBeTruthy();
    // ...and carries NO count, because "0" would read as "has never asked for anything" when it
    // only means "nothing of theirs is on this tab".
    expect(
      within(list).queryByRole("option", { name: /Quiet Pete, 0/ }),
    ).toBeNull();
    expect(
      within(list).getByRole("option", { name: /^Sarah, 2 titles/ }),
    ).toBeTruthy();
  });

  it("finds someone by their Plex login as well as their display name", async () => {
    // Whoever invited these people knows them by the login they typed into Plex, so searching for
    // it has to work even though the list shows the friendlier name.
    getUsers.mockResolvedValue([
      person("sarah_p89", "Sarah"),
      person("m_jones", "Mike"),
    ]);
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        tmdb_id: 100,
        title: "Sarah Pick",
        wanters: ["sarah_p89"],
      }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Mike Pick",
        wanters: ["m_jones"],
      }),
    ]);
    renderPage();
    await screen.findByText("Sarah Pick");

    await userEvent.click(screen.getByRole("combobox", { name: /Wanted by/i }));
    await userEvent.type(
      screen.getByRole("combobox", { name: /Wanted by/i }),
      "p89",
    );

    const list = await screen.findByRole("listbox");
    const options = within(list).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveAccessibleName(/^Sarah,/);
  });

  it("truncates a long wanters list to three names plus a +N more count", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "Popular",
        wanters: ["Sarah", "Mike", "Ann", "Jo", "Lee"],
      }),
    ]);
    renderPage();
    expect(
      await screen.findByText(/Wanted by Sarah, Mike, Ann \+2 more/),
    ).toBeTruthy();
  });

  it("sends the selected title by its id", async () => {
    listRequests.mockResolvedValue([candidate({ id: 7, title: "Fallout" })]);
    renderPage();
    await screen.findByText("Fallout");
    await userEvent.click(screen.getByRole("checkbox", { name: /Fallout/i }));
    await userEvent.click(toolbar().getByRole("button", { name: /Send/i }));
    await waitFor(() => expect(sendRequests).toHaveBeenCalledWith([7], false));
  });

  it("rejects the selected title by its id", async () => {
    listRequests.mockResolvedValue([candidate({ id: 9, title: "Ripley" })]);
    renderPage();
    await screen.findByText("Ripley");
    await userEvent.click(screen.getByRole("checkbox", { name: /Ripley/i }));
    await userEvent.click(toolbar().getByRole("button", { name: /Reject/i }));
    await waitFor(() => expect(rejectRequests).toHaveBeenCalledWith([9]));
    // Delete is the other, non-permanent action — reject must not also hard-delete.
    expect(deleteRequests).not.toHaveBeenCalled();
  });

  it("deletes the selected title by its id (the can-come-back action)", async () => {
    listRequests.mockResolvedValue([candidate({ id: 11, title: "Andor" })]);
    renderPage();
    await screen.findByText("Andor");
    await userEvent.click(screen.getByRole("checkbox", { name: /Andor/i }));
    await userEvent.click(toolbar().getByRole("button", { name: /^Delete/i }));
    await waitFor(() => expect(deleteRequests).toHaveBeenCalledWith([11]));
    // Delete is not a rejection — it leaves no tombstone.
    expect(rejectRequests).not.toHaveBeenCalled();
  });

  it("lets a rejected title come straight back to Waiting via 'Allow again' (restores, not deletes)", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 21, title: "Blocked Show", status: "rejected" }),
    ]);
    renderPage();
    // No pending title to findByText, so wait on the tab itself before interacting.
    await userEvent.click(
      await screen.findByRole("button", { name: "Rejected (1)" }),
    );
    await userEvent.click(screen.getByRole("button", { name: /Allow again/i }));
    // Restore (back to pending) — NOT delete: the item must reappear in Waiting, not vanish.
    await waitFor(() => expect(restoreRequests).toHaveBeenCalledWith([21]));
    expect(deleteRequests).not.toHaveBeenCalled();
  });

  it("restores every rejected title at once with 'Allow all again'", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 21, title: "Blocked One", status: "rejected" }),
      candidate({
        id: 22,
        tmdb_id: 222,
        title: "Blocked Two",
        status: "rejected",
      }),
    ]);
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: "Rejected (2)" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Allow all again/i }),
    );
    // Bulk restore is a set operation — assert both ids reached it, order-independent (the list is
    // sorted for display, so the mutate order follows the sort, not insertion order).
    await waitFor(() => expect(restoreRequests).toHaveBeenCalled());
    expect(
      [...(restoreRequests.mock.calls.at(-1)?.[0] ?? [])].sort((a, b) => a - b),
    ).toEqual([21, 22]);
  });

  it("reads as off — and cannot send — when requests are disabled but candidates are on file", async () => {
    // The "off" state used to depend on the inbox being EMPTY, so stale candidates rendered the
    // full inbox with a live Send button on a feature the owner had turned off.
    getSettings.mockResolvedValue({ "requests.enabled": false });
    listRequests.mockResolvedValue([candidate({ id: 3, title: "Fallout" })]);
    renderPage();

    expect(await screen.findByText(/Requests are off/i)).toBeTruthy();
    expect(screen.getByText("Fallout")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /to Radarr\/Sonarr/i }),
    ).toBeDisabled();
    expect(toolbar().getByRole("button", { name: /Reject/i })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Fallout/i })).toBeDisabled();
  });

  it("keeps the inbox actionable while requests are on", async () => {
    listRequests.mockResolvedValue([candidate({ id: 3, title: "Fallout" })]);
    renderPage();

    expect(await screen.findByText("Fallout")).toBeTruthy();
    expect(screen.queryByText(/Requests are off/i)).toBeNull();
    expect(
      screen.getByRole("checkbox", { name: /Fallout/i }),
    ).not.toBeDisabled();
  });

  /**
   * Discussion #87: judging a pile of unfamiliar titles meant opening a tab per row and then coming
   * back to the bulk toolbar. The synopsis and the per-row buttons are the two halves of that.
   */
  describe("deciding one title at a time", () => {
    it("shows TMDB's synopsis on a waiting title", async () => {
      listRequests.mockResolvedValue([
        candidate({
          id: 1,
          title: "Sinners",
          overview: "Twin brothers return home to a waiting evil.",
        }),
      ]);
      renderPage();
      expect(
        await screen.findByText("Twin brothers return home to a waiting evil."),
      ).toBeTruthy();
    });

    it("draws no synopsis block at all when there is no synopsis", async () => {
      // A pre-0071 row waiting on its next run, or a title TMDB has no text for. An empty paragraph
      // would leave a gap on the card that reads as still-loading.
      listRequests.mockResolvedValue([
        candidate({ id: 1, title: "Sinners", overview: "   " }),
      ]);
      renderPage();
      await screen.findByText("Sinners");
      const card = screen
        .getByRole("group", { name: "Actions for Sinners" })
        .closest("div[class*='rounded-lg']");
      expect(card?.textContent).not.toMatch(/^\s*$/);
      expect(screen.queryByTitle("   ")).toBeNull();
    });

    it("sends one title from its own row without ticking anything", async () => {
      listRequests.mockResolvedValue([
        candidate({ id: 7, title: "Fallout" }),
        candidate({ id: 8, tmdb_id: 200, title: "Andor" }),
      ]);
      renderPage();
      await screen.findByText("Fallout");
      await userEvent.click(
        rowActions("Fallout").getByRole("button", { name: /Send/i }),
      );
      // Exactly this title — not the whole visible page, which is what a mis-wired row button
      // acting on `selectedPending` (empty) or on `pendingShown` would have done.
      await waitFor(() =>
        expect(sendRequests).toHaveBeenCalledWith([7], false),
      );
    });

    it("rejects and deletes one title from its own row, each on the right id", async () => {
      listRequests.mockResolvedValue([
        candidate({ id: 7, title: "Fallout" }),
        candidate({ id: 8, tmdb_id: 200, title: "Andor" }),
      ]);
      renderPage();
      await screen.findByText("Andor");

      await userEvent.click(
        rowActions("Andor").getByRole("button", { name: /Reject/i }),
      );
      await waitFor(() => expect(rejectRequests).toHaveBeenCalledWith([8]));
      expect(deleteRequests).not.toHaveBeenCalled();

      await userEvent.click(
        rowActions("Fallout").getByRole("button", { name: /^Delete/i }),
      );
      await waitFor(() => expect(deleteRequests).toHaveBeenCalledWith([7]));
    });

    it("does not disturb a batch the owner has half-assembled", async () => {
      // The row buttons and the checkboxes are two ways through the same list. Deciding one title
      // must not silently drop the selection someone was building up for the toolbar.
      listRequests.mockResolvedValue([
        candidate({ id: 7, title: "Fallout" }),
        candidate({ id: 8, tmdb_id: 200, title: "Andor" }),
      ]);
      renderPage();
      await screen.findByText("Fallout");
      await userEvent.click(screen.getByRole("checkbox", { name: /Andor/i }));

      await userEvent.click(
        rowActions("Fallout").getByRole("button", { name: /^Delete/i }),
      );
      await waitFor(() => expect(deleteRequests).toHaveBeenCalledWith([7]));
      expect(screen.getByRole("checkbox", { name: /Andor/i })).toBeChecked();
    });

    it("does not tick the row it is acting on", async () => {
      listRequests.mockResolvedValue([candidate({ id: 7, title: "Fallout" })]);
      renderPage();
      await screen.findByText("Fallout");
      await userEvent.click(
        rowActions("Fallout").getByRole("button", { name: /Send/i }),
      );
      expect(
        screen.getByRole("checkbox", { name: /Fallout/i }),
      ).not.toBeChecked();
    });

    it("does not tick the row when the click lands on a look-up link or the why expander", async () => {
      // The card was a <label>, which by spec does nothing for clicks targeted at interactive
      // descendants — so links and the expander never selected the row. Re-creating
      // click-anywhere-to-select on a plain div means re-creating that exclusion by hand, and
      // without it the exact workflow #87 is about (open TMDB to research an unfamiliar title)
      // silently ticks the row, and the next toolbar Reject takes a title nobody chose.
      listRequests.mockResolvedValue([
        candidate({
          id: 7,
          title: "Fallout",
          why: [
            {
              user: "sarah",
              row: "Picked for You",
              seed: "Mad Max",
              source: "tmdb_similar",
            },
            {
              user: "mike",
              row: "Picked for You",
              seed: "The Last of Us",
              source: "tmdb_similar",
            },
            {
              user: "james",
              row: "Sci-Fi",
              seed: "Dune",
              source: "trakt_related",
            },
            // A fourth, because WhyBreakdown only draws the expander past LIMIT = 3.
            {
              user: "kim",
              row: "Sci-Fi",
              seed: "Alien",
              source: "tmdb_similar",
            },
          ],
        }),
      ]);
      renderPage();
      await screen.findByText("Fallout");
      const box = screen.getByRole("checkbox", { name: /Fallout/i });

      await userEvent.click(screen.getByRole("link", { name: /TMDB/i }));
      expect(box).not.toBeChecked();

      await userEvent.click(
        screen.getByRole("button", { name: /more reason/i }),
      );
      expect(box).not.toBeChecked();

      // ...and the plain card surface still selects, which is the whole point of the handler.
      await userEvent.click(screen.getByText("Fallout"));
      expect(box).toBeChecked();
    });

    it("drops the decided title from a batch without clearing the rest", async () => {
      // `busy` goes false when the POST resolves, before the refetched list lands. Leaving the
      // decided id in the selection lets the toolbar act on it inside that window.
      listRequests.mockResolvedValue([
        candidate({ id: 7, title: "Fallout" }),
        candidate({ id: 8, tmdb_id: 200, title: "Andor" }),
      ]);
      renderPage();
      await screen.findByText("Fallout");
      await userEvent.click(screen.getByRole("checkbox", { name: /Fallout/i }));
      await userEvent.click(screen.getByRole("checkbox", { name: /Andor/i }));

      await userEvent.click(
        rowActions("Fallout").getByRole("button", { name: /Send/i }),
      );
      await waitFor(() =>
        expect(sendRequests).toHaveBeenCalledWith([7], false),
      );
      expect(
        screen.getByRole("checkbox", { name: /Fallout/i }),
      ).not.toBeChecked();
      expect(screen.getByRole("checkbox", { name: /Andor/i })).toBeChecked();
    });

    it("still selects the row when the card itself is clicked", async () => {
      // The click-anywhere-to-select affordance is deliberate and survived the <label> removal.
      listRequests.mockResolvedValue([candidate({ id: 7, title: "Fallout" })]);
      renderPage();
      await userEvent.click(await screen.findByText("Fallout"));
      expect(screen.getByRole("checkbox", { name: /Fallout/i })).toBeChecked();
    });

    it("cannot act on a row while requests are off", async () => {
      getSettings.mockResolvedValue({ "requests.enabled": false });
      listRequests.mockResolvedValue([candidate({ id: 7, title: "Fallout" })]);
      renderPage();
      await screen.findByText("Fallout");
      const row = rowActions("Fallout");
      expect(row.getByRole("button", { name: /Send/i })).toBeDisabled();
      expect(row.getByRole("button", { name: /^Delete/i })).toBeDisabled();
      expect(row.getByRole("button", { name: /Reject/i })).toBeDisabled();
    });
  });
});

/**
 * The Arr badge. Three of its four states used to render as the same nothing: a lookup still in
 * flight, an app that never answered, and a title genuinely absent from both. The query also
 * fetched ONCE with no polling and nothing ever invalidated it, so two of those three were states
 * you could sit in indefinitely with no way to tell which you were in.
 */
describe("RequestsPage — what Sonarr/Radarr has", () => {
  beforeEach(() => {
    listRequests.mockReset();
    sendRequests.mockClear();
    getSettings.mockResolvedValue({ "requests.enabled": true });
    getUsers.mockResolvedValue([]);
    getArrStatus.mockReset();
  });

  it("says it is checking while the first lookup is in flight", async () => {
    listRequests.mockResolvedValue([candidate({ id: 1, title: "Dune" })]);
    // Never resolves: the state under test is the one that used to be invisible.
    getArrStatus.mockReturnValue(new Promise(() => {}));
    renderPage();

    await screen.findByText("Dune");
    expect(await screen.findByText(/Checking/i)).toBeInTheDocument();
  });

  it("shows what the app reported once the lookup lands", async () => {
    listRequests.mockResolvedValue([candidate({ id: 1, title: "Dune" })]);
    getArrStatus.mockResolvedValue({
      statuses: { "1": "downloaded" },
      radarr: "ok",
      sonarr: "off",
    });
    renderPage();

    expect(await screen.findByText("Downloaded")).toBeInTheDocument();
    expect(screen.queryByText(/Checking/i)).toBeNull();
  });

  it("names an app it couldn't reach instead of drawing nothing", async () => {
    // The bug: a failed lookup is swallowed server-side so one app being down can't blank the
    // other, which left an unreachable Radarr looking exactly like "Radarr tracks none of these".
    listRequests.mockResolvedValue([candidate({ id: 1, title: "Dune" })]);
    getArrStatus.mockResolvedValue({
      statuses: {},
      radarr: "unreachable",
      sonarr: "ok",
    });
    renderPage();

    expect(await screen.findByText(/Can.t reach Radarr/i)).toBeInTheDocument();
  });

  it("blames only the app that is down, per title", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune", media_type: "movie" }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        media_type: "show",
      }),
    ]);
    getArrStatus.mockResolvedValue({
      statuses: { "2": "downloading" },
      radarr: "unreachable",
      sonarr: "ok",
    });
    renderPage();

    // The film says Radarr is down; the show, served by a healthy Sonarr, just reports its status.
    expect(await screen.findByText(/Can.t reach Radarr/i)).toBeInTheDocument();
    expect(screen.queryByText(/Can.t reach Sonarr/i)).toBeNull();
    expect(screen.getByText("Downloading")).toBeInTheDocument();
  });

  it("re-asks the Arrs the moment something is sent to them", async () => {
    // Nothing invalidated this key at all, so a title you had just sent carried no badge until the
    // next poll — or, before there was a poll, until you reloaded the page.
    listRequests.mockResolvedValue([candidate({ id: 1, title: "Dune" })]);
    getArrStatus.mockResolvedValue({
      statuses: {},
      radarr: "ok",
      sonarr: "off",
    });
    renderPage();

    await screen.findByText("Dune");
    await waitFor(() => expect(getArrStatus).toHaveBeenCalled());
    const before = getArrStatus.mock.calls.length;

    await userEvent.click(screen.getByRole("checkbox", { name: /Dune/i }));
    await userEvent.click(
      screen.getByRole("button", { name: /to Radarr\/Sonarr/i }),
    );

    await waitFor(() =>
      expect(getArrStatus.mock.calls.length).toBeGreaterThan(before),
    );
  });

  describe("the language chip", () => {
    async function renderWith(languageMode: string, language: string) {
      getSettings.mockResolvedValue({
        "requests.enabled": true,
        "requests.language_mode": languageMode,
        "requests.preferred_languages": ["en"],
      });
      listRequests.mockResolvedValue([
        candidate({ title: "Kaiju no Kodomo", language }),
      ]);
      renderPage();
      await screen.findByText("Kaiju no Kodomo");
    }

    it("draws no chip on the default 'any' server", async () => {
      // The chip's job is to explain why a title is being HELD BACK. On "any" nothing is, so a chip
      // on every foreign tile would be pure noise on the shipped default.
      await renderWith("any", "ja");
      expect(screen.queryByText("Japanese")).not.toBeInTheDocument();
    });

    it("draws a chip for a non-preferred language once a mode is on", async () => {
      await renderWith("prefer", "ja");
      expect(await screen.findByText("Japanese")).toBeInTheDocument();
    });

    it("draws no chip for a preferred language", async () => {
      // An "English" chip on every tile of a mostly-English inbox is the noise this avoids.
      await renderWith("prefer", "en");
      expect(screen.queryByText("English")).not.toBeInTheDocument();
    });

    it("draws no chip when the language is unknown", async () => {
      // "" is a title queued before the column existed, or one only a non-TMDB source surfaced.
      await renderWith("prefer", "");
      expect(screen.queryByText("Unknown")).not.toBeInTheDocument();
    });
  });
});
