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
    ...over,
  };
}

function page(over: Partial<WatchedPage>): WatchedPage {
  return {
    items: [title({})],
    total: 1,
    last_full_sync_at: "2026-08-05T00:00:00+00:00",
    synced_titles: 1284,
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

    expect(await screen.findByText(/1284 titles synced/)).toBeInTheDocument();
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
});
