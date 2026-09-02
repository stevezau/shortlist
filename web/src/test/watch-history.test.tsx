import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  WatchHistory,
  watchDepth,
} from "@/components/user-detail/watch-history";
import type * as ApiModule from "@/lib/api";
import type { WatchedFilters, WatchedPage, WatchedTitle } from "@/lib/types";

const { getUserWatched } = vi.hoisted(() => ({
  getUserWatched: vi.fn((_id: number, _filters: WatchedFilters) =>
    Promise.resolve({} as WatchedPage),
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUserWatched: (id: number, filters: WatchedFilters) =>
        getUserWatched(id, filters),
    },
  };
});

function lib(name: string, media_type: string) {
  return { name, media_type };
}

function title(over: Partial<WatchedTitle>): WatchedTitle {
  return {
    title: "Teacup",
    tmdb_id: 1,
    media_type: "show",
    watched_at: "2026-08-03T00:00:00+00:00",
    year: 2024,
    watch_count: 1,
    viewed_leaf_count: 3,
    leaf_count: 8,
    user_rating: null,
    libraries: ["TV Shows"],
    ...over,
  };
}

function page(over: Partial<WatchedPage>): WatchedPage {
  return {
    items: [title({})],
    total: 1,
    libraries: [lib("TV Shows", "show")],
    last_full_sync_at: "2026-08-05T00:00:00+00:00",
    synced_titles: 1284,
    // Ratings on, nobody has rated anything — the default state for nearly every real person, and
    // the one the pre-existing assertions here were written against.
    dislike_threshold: 2,
    ratings_trusted: true,
    rated_count: 0,
    ...over,
  };
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WatchHistory userId={7} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getUserWatched.mockResolvedValue(page({}));
});

describe("watchDepth", () => {
  it("reports how far through a show they are", () => {
    expect(watchDepth(title({ viewed_leaf_count: 3, leaf_count: 8 }))).toBe(
      "3 of 8 episodes",
    );
  });

  it("says finished once every episode is seen", () => {
    expect(watchDepth(title({ viewed_leaf_count: 8, leaf_count: 8 }))).toBe(
      "finished",
    );
  });

  it("says nothing when Plex reports no episode totals", () => {
    // NOT "0 of 0" — "we don't know how many episodes there are" is a different claim from
    // "they've watched none of it", and only the second would justify a progress figure.
    expect(
      watchDepth(title({ viewed_leaf_count: null, leaf_count: null })),
    ).toBeNull();
  });

  it("counts rewatches for a movie but stays quiet on a single watch", () => {
    const movie = {
      media_type: "movie",
      viewed_leaf_count: null,
      leaf_count: null,
    } as const;
    expect(watchDepth(title({ ...movie, watch_count: 2 }))).toBe("watched 2×");
    expect(watchDepth(title({ ...movie, watch_count: 1 }))).toBeNull();
  });
});

describe("WatchHistory", () => {
  it("sends the typed search to the server rather than filtering what is loaded", async () => {
    renderPanel();
    await screen.findByText("Teacup");

    await userEvent.type(
      screen.getByLabelText(/search watched titles/i),
      "bear",
    );

    // The whole point of the rewrite: search is a server query over the FULL cached set, so the
    // request has to carry the term. A client-side filter would never call this again.
    await waitFor(() =>
      expect(getUserWatched).toHaveBeenCalledWith(7, {
        q: "bear",
        mediaType: "",
        library: "",
        limit: 25,
      }),
    );
  });

  it("sends the media filter", async () => {
    renderPanel();
    await screen.findByText("Teacup");

    await userEvent.click(screen.getByRole("button", { name: "Movies" }));

    await waitFor(() =>
      expect(getUserWatched).toHaveBeenCalledWith(7, {
        q: "",
        mediaType: "movie",
        library: "",
        limit: 25,
      }),
    );
  });

  it("resets paging when the filter changes", async () => {
    getUserWatched.mockResolvedValue(page({ total: 100 }));
    renderPanel();
    await userEvent.click(
      await screen.findByRole("button", { name: /show 50 more/i }),
    );
    await waitFor(() =>
      expect(getUserWatched).toHaveBeenCalledWith(7, {
        q: "",
        mediaType: "",
        library: "",
        limit: 75,
      }),
    );

    await userEvent.click(screen.getByRole("button", { name: "Shows" }));

    // Back to one page — asking for 75 rows of a fresh search would page past results nobody has
    // scrolled to yet.
    await waitFor(() =>
      expect(getUserWatched).toHaveBeenCalledWith(7, {
        q: "",
        mediaType: "show",
        library: "",
        limit: 25,
      }),
    );
  });

  it("shows how far through a show they are, next to the title", async () => {
    renderPanel();

    expect(await screen.findByText(/3 of 8 episodes/)).toBeInTheDocument();
  });

  it("says how complete the cached set is", async () => {
    renderPanel();

    expect(
      await screen.findByText(/1284 library copies synced/),
    ).toBeInTheDocument();
    expect(screen.getByText(/last full sync/)).toBeInTheDocument();
  });

  it("warns when the set has never been fully synced", async () => {
    // This is the state behind "I watched that, why was it recommended?" — the answer is that
    // Shortlist has not read their history yet, and the panel has to say so.
    getUserWatched.mockResolvedValue(page({ last_full_sync_at: null }));
    renderPanel();

    expect(await screen.findByText(/never fully synced/)).toBeInTheDocument();
  });

  it("distinguishes an empty search from an empty history", async () => {
    getUserWatched.mockResolvedValue(page({ items: [], total: 0 }));
    renderPanel();

    expect(await screen.findByText(/no watch history/i)).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/search watched titles/i), "zz");

    expect(
      await screen.findByText(/nothing matches that/i),
    ).toBeInTheDocument();
  });

  it("offers no Show more once everything is on screen", async () => {
    renderPanel();
    await screen.findByText("Teacup");

    expect(
      screen.queryByRole("button", { name: /show 50 more/i }),
    ).not.toBeInTheDocument();
  });

  it("shows what they rated a title, beside the watch", async () => {
    getUserWatched.mockResolvedValue(
      page({ items: [title({ user_rating: 8 })], rated_count: 1 }),
    );
    renderPanel();

    expect(await screen.findByText(/rated 4 out of 5/)).toBeInTheDocument();
  });

  it("says a low-rated title has stopped shaping their picks", async () => {
    getUserWatched.mockResolvedValue(
      page({ items: [title({ user_rating: 2 })], rated_count: 1 }),
    );
    renderPanel();

    expect(await screen.findByText(/not seeding/)).toBeInTheDocument();
  });

  it("states the threshold once they have rated anything", async () => {
    getUserWatched.mockResolvedValue(
      page({ items: [title({ user_rating: 10 })], rated_count: 1 }),
    );
    renderPanel();

    expect(
      await screen.findByText(/at or below 1 star stops being used/),
    ).toBeInTheDocument();
  });

  it("says nothing about ratings when nobody has rated anything", async () => {
    // Most people. An explanation of a feature that is doing nothing for them is just noise.
    renderPanel();
    await screen.findByText("Teacup");

    expect(screen.queryByText(/stops being used/)).not.toBeInTheDocument();
  });

  it("warns when another tool is writing the ratings on this account", async () => {
    // The case that would otherwise read as a broken feature: a column of visible stars that change
    // nothing. Measured on a real server — see the fixture header.
    getUserWatched.mockResolvedValue(
      page({
        items: [title({ user_rating: 7.9 })],
        rated_count: 1455,
        ratings_trusted: false,
      }),
    );
    renderPanel();

    expect(
      await screen.findByText(/another tool is writing plex ratings/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/ignores all 1455 of them/i)).toBeInTheDocument();
  });
});

describe("the library filter", () => {
  const twoLibraries = () =>
    page({
      items: [title({ libraries: ["4K TV", "TV Shows"] })],
      libraries: [
        lib("4K Movies", "movie"),
        lib("4K TV", "show"),
        lib("Movies", "movie"),
        lib("TV Shows", "show"),
      ],
    });

  it("names the libraries a title was found in, under its name", async () => {
    // Two names is a title stored twice — the row this page used to render as two rows, each with
    // its own Block button doing the same global thing.
    getUserWatched.mockResolvedValue(twoLibraries());
    renderPanel();

    expect(await screen.findByText("4K TV · TV Shows")).toBeInTheDocument();
  });

  it("shows one name, with no trailing separator, for a title in one library", async () => {
    // On a server where the type HAS two libraries, a title in only one of them still names it —
    // that is how you tell it apart from the duplicated rows around it.
    getUserWatched.mockResolvedValue(
      page({
        items: [title({ media_type: "movie", libraries: ["Movies"] })],
        libraries: [lib("4K Movies", "movie"), lib("Movies", "movie")],
      }),
    );
    renderPanel();

    expect(await screen.findByText("Movies")).toBeInTheDocument();
    expect(screen.queryByText(/Movies ·/)).not.toBeInTheDocument();
  });

  it("draws no library line where naming it would say nothing", async () => {
    // One movie library means "Movies" under a film repeats the "Movie ·" on the right of the same
    // row. Same judgement as the toolbar's, applied per row.
    getUserWatched.mockResolvedValue(
      page({
        items: [title({ media_type: "movie", libraries: ["Movies"] })],
        libraries: [lib("Movies", "movie"), lib("TV Shows", "show")],
      }),
    );
    const { container } = renderPanel();
    await screen.findByText("Teacup");

    expect(container.querySelectorAll("li span.block")).toHaveLength(0);
  });

  it("still names both libraries on a row that is genuinely in two", async () => {
    // Even if nothing else on the server is duplicated, THIS row is — and that is the whole point.
    getUserWatched.mockResolvedValue(
      page({
        items: [
          title({ media_type: "movie", libraries: ["4K Movies", "Movies"] }),
        ],
        libraries: [lib("4K Movies", "movie"), lib("Movies", "movie")],
      }),
    );
    renderPanel();

    expect(await screen.findByText("4K Movies · Movies")).toBeInTheDocument();
  });

  it("renders no library line at all for a watch cached before the name was recorded", async () => {
    // Its name arrives on that person's next sync. Asserting on the ELEMENT, not on its text: an
    // empty `libraries` renders an empty string either way, so a text assertion here passes just as
    // happily against a component that emits a blank line taking up space under every title.
    getUserWatched.mockResolvedValue(
      page({ items: [title({ libraries: [] })], libraries: [] }),
    );
    const { container } = renderPanel();
    await screen.findByText("Teacup");

    expect(container.querySelectorAll("li span.block")).toHaveLength(0);
  });

  it("sends the chosen library to the server", async () => {
    getUserWatched.mockResolvedValue(twoLibraries());
    renderPanel();
    await screen.findByText("Teacup");

    await userEvent.selectOptions(
      screen.getByLabelText(/filter by library/i),
      "4K Movies",
    );

    await waitFor(() =>
      expect(getUserWatched).toHaveBeenCalledWith(7, {
        q: "",
        mediaType: "",
        library: "4K Movies",
        limit: 25,
      }),
    );
  });

  it("offers every library the person watched in, not just the ones on screen", async () => {
    // The list is unnarrowed by the current filter on purpose — narrowing it would empty the control
    // that did the narrowing, stranding the person on one library.
    getUserWatched.mockResolvedValue(twoLibraries());
    renderPanel();

    const select = await screen.findByLabelText(/filter by library/i);
    expect(
      Array.from(select.querySelectorAll("option")).map((o) => o.textContent),
    ).toEqual(["All libraries", "4K Movies", "4K TV", "Movies", "TV Shows"]);
  });

  it("hides the filter on a server with one library", async () => {
    // It could only ever say "All libraries".
    getUserWatched.mockResolvedValue(
      page({ libraries: [lib("TV Shows", "show")] }),
    );
    renderPanel();
    await screen.findByText("Teacup");

    expect(screen.queryByLabelText(/filter by library/i)).not.toBeInTheDocument();
  });

  it("hides the filter when every type has exactly one library", async () => {
    // The common server, and the regression this rule exists for: libraries named "Movies" and
    // "TV Shows" beside buttons named "Movies" and "Shows" is the same choice offered twice.
    getUserWatched.mockResolvedValue(
      page({ libraries: [lib("Movies", "movie"), lib("TV Shows", "show")] }),
    );
    renderPanel();
    await screen.findByText("Teacup");

    expect(screen.queryByLabelText(/filter by library/i)).not.toBeInTheDocument();
  });

  it("shows the filter as soon as one type holds two libraries", async () => {
    getUserWatched.mockResolvedValue(
      page({
        libraries: [
          lib("4K Movies", "movie"),
          lib("Movies", "movie"),
          lib("TV Shows", "show"),
        ],
      }),
    );
    renderPanel();

    const select = await screen.findByLabelText(/filter by library/i);
    expect(
      Array.from(select.querySelectorAll("option")).map((o) => o.textContent),
    ).toEqual(["All libraries", "4K Movies", "Movies", "TV Shows"]);
  });

  it("offers only the selected type's libraries", async () => {
    // "4K Movies" under a Shows filter can only ever return nothing.
    getUserWatched.mockResolvedValue(
      page({
        libraries: [
          lib("4K Movies", "movie"),
          lib("Movies", "movie"),
          lib("Anime", "show"),
          lib("TV Shows", "show"),
        ],
      }),
    );
    renderPanel();
    await screen.findByText("Teacup");

    await userEvent.click(screen.getByRole("button", { name: "Shows" }));

    const select = await screen.findByLabelText(/filter by library/i);
    await waitFor(() =>
      expect(
        Array.from(select.querySelectorAll("option")).map((o) => o.textContent),
      ).toEqual(["All libraries", "Anime", "TV Shows"]),
    );
  });

  it("caps the dropdown width, whatever a library is called", async () => {
    // A native <select> sizes to its WIDEST option, so one long library name blows out the toolbar:
    // "4K HDR Remux Collection — Director's Cuts and Extended Editions" measured 470px and ran
    // 212px off a 320px phone. jsdom does no layout, so this asserts the CAP is still declared —
    // the measurement itself lives in the commit that added it.
    getUserWatched.mockResolvedValue(
      page({
        libraries: [
          lib("Movies", "movie"),
          lib("4K HDR Remux Collection — Director's Cuts and Extended Editions", "movie"),
        ],
      }),
    );
    renderPanel();

    const select = await screen.findByLabelText(/filter by library/i);
    expect(select.className).toMatch(/max-w-/);
    expect(select.className).toMatch(/truncate/);
    expect(select.className).toMatch(/min-w-0/);
  });

  it("resets paging when the library changes", async () => {
    getUserWatched.mockResolvedValue(page({ ...twoLibraries(), total: 100 }));
    renderPanel();
    await userEvent.click(
      await screen.findByRole("button", { name: /show 50 more/i }),
    );

    await userEvent.selectOptions(
      screen.getByLabelText(/filter by library/i),
      "Movies",
    );

    await waitFor(() =>
      expect(getUserWatched).toHaveBeenCalledWith(7, {
        q: "",
        mediaType: "",
        library: "Movies",
        limit: 25,
      }),
    );
  });

  it("explains an impossible type-and-library combination rather than looking broken", async () => {
    getUserWatched.mockResolvedValue(
      page({
        items: [],
        total: 0,
        libraries: [lib("4K Movies", "movie"), lib("Movies", "movie")],
      }),
    );
    renderPanel();
    await userEvent.selectOptions(
      await screen.findByLabelText(/filter by library/i),
      "4K Movies",
    );
    await userEvent.click(screen.getByRole("button", { name: "Shows" }));

    expect(
      await screen.findByText("No shows watched in 4K Movies."),
    ).toBeInTheDocument();
  });
});
