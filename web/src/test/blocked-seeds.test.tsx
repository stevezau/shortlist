import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BlockedSeedsList } from "@/components/user-detail/blocked-seeds";
import type * as ApiModule from "@/lib/api";
import type { User, WatchItem } from "@/lib/types";

const { getUserHistory, blockSeed, unblockSeed, searchTitles } = vi.hoisted(
  () => ({
    getUserHistory: vi.fn(),
    blockSeed: vi.fn(),
    unblockSeed: vi.fn(),
    searchTitles: vi.fn(),
  }),
);

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { getUserHistory, blockSeed, unblockSeed, searchTitles },
  };
});

function watch(patch: Partial<WatchItem> & { title: string }): WatchItem {
  return {
    tmdb_id: 1,
    media_type: "movie",
    watched_at: "2026-07-30T10:00:00Z",
    year: 2024,
    season: null,
    episode: null,
    episode_title: null,
    ...patch,
  };
}

function renderList(prefs: Record<string, unknown> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const user = { id: 7, username: "sarah", prefs } as unknown as User;
  render(
    <QueryClientProvider client={client}>
      <BlockedSeedsList user={user} />
    </QueryClientProvider>,
  );
}

describe("BlockedSeedsList — picking from recent watches", () => {
  beforeEach(() => {
    getUserHistory.mockReset();
    blockSeed.mockReset();
    blockSeed.mockResolvedValue({ blocked_seeds: [] });
    unblockSeed.mockReset();
    searchTitles.mockReset();
  });

  it("offers their recent watches so a block doesn't need a TMDB search", async () => {
    // Blocking is nearly always a reaction to something just watched. Making the owner retype a
    // title Shortlist already knows they watched is busywork — and TMDB search can return a
    // different edition than the one in their library.
    getUserHistory.mockResolvedValue([
      watch({ title: "The Sports Thing", tmdb_id: 11 }),
      watch({ title: "A Kids Film", tmdb_id: 12 }),
    ]);

    renderList();

    await userEvent.click(
      await screen.findByRole("button", { name: /The Sports Thing/ }),
    );

    await waitFor(() =>
      expect(blockSeed).toHaveBeenCalledWith(
        7,
        expect.objectContaining({ tmdbId: 11, title: "The Sports Thing" }),
      ),
    );
  });

  it("marks an already-blocked watch instead of hiding it", async () => {
    // Hiding it makes a second click look like it did nothing; disabled-and-ticked shows it worked.
    getUserHistory.mockResolvedValue([
      watch({ title: "Already Gone", tmdb_id: 11 }),
    ]);

    renderList({ blocked_seeds: [{ tmdb_id: 11, title: "Already Gone" }] });

    // By title, not by name: the blocked LIST below renders its own "Unblock Already Gone" button,
    // so matching on the text alone finds that one instead of the chip.
    const chip = await screen.findByTitle("Already blocked");
    expect(chip).toBeDisabled();
    expect(chip).toHaveTextContent("Already Gone");
  });

  it("never offers a watch with no TMDB id — there is nothing a block could key on", async () => {
    getUserHistory.mockResolvedValue([
      watch({ title: "No GUID Here", tmdb_id: null }),
      watch({ title: "Fine One", tmdb_id: 12 }),
    ]);

    renderList();

    expect(
      await screen.findByRole("button", { name: /Fine One/ }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /No GUID Here/ })).toBeNull();
  });

  it("shows a handful, not the whole history, with the rest one click away", async () => {
    getUserHistory.mockResolvedValue(
      Array.from({ length: 20 }, (_, i) =>
        watch({ title: `Film ${i}`, tmdb_id: 100 + i }),
      ),
    );

    renderList();

    // A wall of every title they've ever watched is its own kind of useless.
    expect(await screen.findByRole("button", { name: /Film 0/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Film 15/ })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /Show more/ }));
    expect(screen.getByRole("button", { name: /Film 15/ })).toBeTruthy();
  });
});
