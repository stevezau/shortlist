import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RequestCandidate } from "@/lib/types";
import { RequestsPage } from "@/pages/requests";

const {
  listRequests,
  sendRequests,
  rejectRequests,
  deleteRequests,
  restoreRequests,
  clearRequests,
  getSettings,
} = vi.hoisted(() => ({
  listRequests: vi.fn(),
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
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    listRequests: () => listRequests(),
    sendRequests: (ids: number[], dryRun?: boolean) =>
      sendRequests(ids, dryRun),
    rejectRequests: (ids: number[]) => rejectRequests(ids),
    deleteRequests: (ids: number[]) => deleteRequests(ids),
    restoreRequests: (ids: number[]) => restoreRequests(ids),
    clearRequests: (ids: number[]) => clearRequests(ids),
    getSettings: () => getSettings(),
  },
}));

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
      <MemoryRouter
        initialEntries={[initialEntry]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <RequestsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
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
    expect(screen.getByText(/Sonarr.s exclusion list/i)).toBeTruthy();
  });

  it("shows a distinct 'off' empty state when requests are disabled", async () => {
    listRequests.mockResolvedValue([]);
    getSettings.mockResolvedValue({ "requests.enabled": false });
    renderPage();
    // Never implies auto-send is running; points the owner at Settings to turn it on.
    expect(await screen.findByText(/Requests are off/i)).toBeTruthy();
    expect(screen.getByText(/Enable in Settings/i)).toBeTruthy();
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
    await userEvent.click(screen.getByRole("button", { name: "Top rated" }));
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
    await userEvent.click(screen.getByRole("button", { name: "8+" }));
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
    await userEvent.click(screen.getByRole("button", { name: "500+" }));
    expect(screen.getByText("Well Attested")).toBeTruthy();
    expect(screen.queryByText("Barely Rated")).toBeNull();
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
    expect(screen.getByText(/wanted by 3 people/)).toBeTruthy();
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
    await userEvent.click(screen.getByRole("button", { name: /Send/i }));
    await waitFor(() => expect(sendRequests).toHaveBeenCalledWith([7], false));
  });

  it("rejects the selected title by its id", async () => {
    listRequests.mockResolvedValue([candidate({ id: 9, title: "Ripley" })]);
    renderPage();
    await screen.findByText("Ripley");
    await userEvent.click(screen.getByRole("checkbox", { name: /Ripley/i }));
    await userEvent.click(screen.getByRole("button", { name: /Reject/i }));
    await waitFor(() => expect(rejectRequests).toHaveBeenCalledWith([9]));
    // Delete is the other, non-permanent action — reject must not also hard-delete.
    expect(deleteRequests).not.toHaveBeenCalled();
  });

  it("deletes the selected title by its id (the can-come-back action)", async () => {
    listRequests.mockResolvedValue([candidate({ id: 11, title: "Andor" })]);
    renderPage();
    await screen.findByText("Andor");
    await userEvent.click(screen.getByRole("checkbox", { name: /Andor/i }));
    await userEvent.click(screen.getByRole("button", { name: /^Delete/i }));
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
      screen.getByRole("button", { name: /to Sonarr\/Radarr/i }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: /Reject/i })).toBeDisabled();
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
});
