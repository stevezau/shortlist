import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UserRowsSection } from "@/components/user-detail/user-row-card";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { RowOverridePatch, User, UserRow } from "@/lib/types";
import { makeUser } from "@/test/user-fixtures";

const { getUserRows, setUserRowOverride, getHistoryMix, blockSeed, unblockSeed, getSettings } = vi.hoisted(() => ({
  getUserRows: vi.fn(),
  setUserRowOverride: vi.fn(),
  getHistoryMix: vi.fn(),
  blockSeed: vi.fn(),
  unblockSeed: vi.fn(),
  getSettings: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUserRows: (id: number) => getUserRows(id),
      getHistoryMix: (id: number, collectionId: number) => getHistoryMix(id, collectionId),
      blockSeed: (id: number, seed: unknown) => blockSeed(id, seed),
      unblockSeed: (id: number, tmdbId: number) => unblockSeed(id, tmdbId),
      getSettings: () => getSettings(),
      setUserRowOverride: (
        id: number,
        collectionId: number,
        patch: RowOverridePatch,
      ) => setUserRowOverride(id, collectionId, patch),
    },
  };
});

const USER: User = makeUser({ id: 7, history_depth: 40 });

function row(patch: Partial<UserRow> = {}): UserRow {
  return {
    collection_id: 3,
    slug: "picked",
    name: "Picked for You",
    media: "both",
    library: "",
    section_key: "",
    size: 15,
    recent_count: 10,
    favourite_count: 0,
    older_count: 0,
    is_default: true,
    muted: false,
    override: {
      row_size: null,
      recent_count: null,
      favourite_count: null,
      older_count: null,
    },
    picks: [],
    ...patch,
  };
}

function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UserRowsSection user={USER} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const muteSwitch = () =>
  screen.getByRole("switch", {
    name: /Show Picked for You for this person/i,
  });

describe("UserRowCard", () => {
  beforeEach(() => {
    getUserRows.mockReset();
    setUserRowOverride.mockReset();
    getHistoryMix.mockReset();
    blockSeed.mockReset();
    unblockSeed.mockReset();
    getSettings.mockReset();
    getSettings.mockResolvedValue({ "llm_web.search_provider": "exa", "curator.provider": "none" });
  });

  it("does not leave the card reading 'muted' when the mute is rejected", async () => {
    getUserRows.mockResolvedValue([row({ muted: false })]);
    setUserRowOverride.mockRejectedValue(
      new ApiError(502, "Plex did not accept the change."),
    );
    renderSection();
    await waitFor(() => expect(muteSwitch()).toBeChecked());

    await userEvent.click(muteSwitch());

    // The card is a privacy claim. If the PUT failed, the row is STILL delivered to this person,
    // so the card must not dim, badge itself "muted", or leave the switch off.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /still showing for this person/i,
    );
    expect(screen.queryByText("muted")).toBeNull();
    await waitFor(() => expect(muteSwitch()).toBeChecked());
  });

  it("shows the row as muted once the server has actually accepted it", async () => {
    getUserRows.mockResolvedValueOnce([row({ muted: false })]);
    getUserRows.mockResolvedValue([row({ muted: true })]);
    setUserRowOverride.mockResolvedValue({});
    renderSection();
    await waitFor(() => expect(muteSwitch()).toBeChecked());

    await userEvent.click(muteSwitch());

    expect(await screen.findByText("muted")).toBeTruthy();
    expect(setUserRowOverride).toHaveBeenCalledWith(7, 3, { muted: true });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("auto-saves the customization drawer — there is no Save button to miss", async () => {
    getUserRows.mockResolvedValue([row()]);
    setUserRowOverride.mockResolvedValue({});
    renderSection();

    await userEvent.click(
      await screen.findByRole("button", { name: /Customize for this person/i }),
    );
    expect(screen.queryByRole("button", { name: /^Save$/ })).toBeNull();

    await userEvent.click(
      screen.getByRole("switch", { name: /Custom row size/i }),
    );
    const sizeInput = screen.getByLabelText(/Titles for this person/i);
    await userEvent.clear(sizeInput);
    await userEvent.type(sizeInput, "20");
    await userEvent.tab(); // blur commits the typed size

    // Collapsing the drawer used to throw this away; it now persists on its own.
    //
    // The wait has to be for the TYPED value, not for "saved at all". The drawer auto-saves on a
    // debounce, so turning the switch on already queues a save carrying the row's existing size —
    // and waiting on `toHaveBeenCalled()` resolved against THAT one, then read `.at(-1)` and found
    // 15 where the test wanted 20. It passed whenever the debounce had caught up and failed when
    // the machine was busy. Waiting on the value cannot resolve early.
    await waitFor(() => {
      const latest = setUserRowOverride.mock.calls.at(-1);
      expect(latest?.[2]).toMatchObject({ row_size: 20 });
    });
    const call = setUserRowOverride.mock.calls.at(-1);
    expect(call?.[1]).toBe(3);
    // The drawer must never carry the mute flag — that would let a stale switch value ride along.
    expect(call?.[2]).not.toHaveProperty("muted");
  });

  it("saves a per-person watch-history depth, and clears it back to the row default", async () => {
    // A row starting on its own depth (10). Turning the switch on reveals the box; typing a value
    // saves it as this person's override; turning the switch off sends null to inherit the row again.
    getUserRows.mockResolvedValue([row({ recent_count: 10 })]);
    setUserRowOverride.mockResolvedValue({});
    renderSection();

    await userEvent.click(
      await screen.findByRole("button", { name: /Customize for this person/i }),
    );
    // No box until the owner asks for a custom depth — the row default is in force.
    expect(
      screen.queryByLabelText(/Recent watches for this person/i),
    ).toBeNull();

    await userEvent.click(
      screen.getByRole("switch", { name: /Custom watch-history depth/i }),
    );
    const depth = screen.getByLabelText(/Recent watches for this person/i);
    await userEvent.clear(depth);
    await userEvent.type(depth, "5");
    await userEvent.tab();

    await waitFor(() =>
      expect(setUserRowOverride.mock.calls.at(-1)?.[2]).toMatchObject({
        recent_count: 5,
      }),
    );

    // Switching it back off clears the override (null) so the row's own depth applies again.
    await userEvent.click(
      screen.getByRole("switch", { name: /Custom watch-history depth/i }),
    );
    await waitFor(() =>
      expect(setUserRowOverride.mock.calls.at(-1)?.[2]).toMatchObject({
        recent_count: null,
      }),
    );
  });

  it("opens the depth box pre-filled when the person already has a saved override", async () => {
    // A stored recent_count override must show its value, not silently sit behind the "default" switch.
    getUserRows.mockResolvedValue([
      row({ recent_count: 10, override: { row_size: null, recent_count: 3, favourite_count: null, older_count: null } }),
    ]);
    setUserRowOverride.mockResolvedValue({});
    renderSection();

    await userEvent.click(
      await screen.findByRole("button", { name: /Customize for this person/i }),
    );
    const depth = screen.getByLabelText(
      /Recent watches for this person/i,
    ) as HTMLInputElement;
    expect(depth.value).toBe("3");
  });

  it("offers a retry when a customization auto-save fails, instead of stranding the edit", async () => {
    getUserRows.mockResolvedValue([row()]);
    setUserRowOverride.mockRejectedValue(
      new ApiError(500, "Database is busy."),
    );
    renderSection();

    await userEvent.click(
      await screen.findByRole("button", { name: /Customize for this person/i }),
    );
    await userEvent.click(
      screen.getByRole("switch", { name: /Custom row size/i }),
    );
    const sizeInput = screen.getByLabelText(/Titles for this person/i);
    await userEvent.clear(sizeInput);
    await userEvent.type(sizeInput, "10");
    await userEvent.tab(); // blur commits the typed size

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Database is busy/i,
    );
    setUserRowOverride.mockResolvedValue({});
    await userEvent.click(screen.getByRole("button", { name: /Try again/i }));

    await waitFor(() =>
      expect(setUserRowOverride.mock.calls.at(-1)?.[2]).toMatchObject({
        row_size: 10,
      }),
    );
  });

  // "15 titles" sat directly above a list offering "Show all 10 (+5)" — the configured ceiling
  // beside the delivered count, reading as one number disagreeing with itself (audit, Sep 2026).
  it("says what was delivered out of what the row allows, not just the ceiling", async () => {
    getUserRows.mockResolvedValue([
      row({
        size: 15,
        picks: Array.from({ length: 10 }, (_, i) => ({
          rank: i + 1,
          title: `Title ${i + 1}`,
          reason: "why",
          rating_key: 0,
          media_type: "movie",
          collection_slug: "picked",
          library: "",
          section_key: "",
          seed_title: null,
          sources: [],
          affinity: 1,
          year: null,
          rating: null,
        })),
      }),
    ]);
    renderSection();

    expect(await screen.findByText(/10 of 15 titles/)).toBeInTheDocument();
    expect(screen.queryByText(/^15 titles/)).toBeNull();
  });

  it("says 'up to' when there is nothing delivered to count", async () => {
    // A ceiling is all there is to report before the first run, and "0 of 15" would read as a
    // failure rather than as "not built yet".
    getUserRows.mockResolvedValue([row({ size: 15, picks: [] })]);
    renderSection();

    expect(await screen.findByText(/up to 15 titles/)).toBeInTheDocument();
  });

  it("never prints a delivered count larger than the size it is out of", async () => {
    // `picks` is from the LAST RUN, `size` is the setting as it stands now — so lowering someone's
    // row size from 15 to 5 refetches this card straight away and would read "15 of 5 titles"
    // until the next run rebuilt the row. The ceiling alone is the honest reading there.
    getUserRows.mockResolvedValue([
      row({
        size: 5,
        picks: Array.from({ length: 15 }, (_, i) => ({
          rank: i + 1,
          title: `Title ${i + 1}`,
          reason: "why",
          rating_key: 0,
          media_type: "movie",
          collection_slug: "picked",
          library: "",
          section_key: "",
          seed_title: null,
          sources: [],
          affinity: 1,
          year: null,
          rating: null,
        })),
      }),
    ]);
    renderSection();

    expect(await screen.findByText(/up to 5 titles/)).toBeInTheDocument();
    expect(screen.queryByText(/15 of 5/)).toBeNull();
  });
});

describe("UserRowCard — history mix", () => {
  const MIX = {
    recent: Array.from({ length: 7 }, (_, i) => ({
      title: `Recent ${i + 1}`,
      year: 2024,
      media_type: "movie",
      tmdb_id: 100 + i,
    })),
    favourites: [{ title: "Heat", year: 1995, media_type: "movie", tmdb_id: 949 }],
    older: [{ title: "Alien", year: 1979, media_type: "movie", tmdb_id: 348 }],
    favourite_count: 3,
    older_count: 3,
  };

  beforeEach(() => {
    getUserRows.mockReset();
    setUserRowOverride.mockReset();
    getHistoryMix.mockReset();
    blockSeed.mockReset();
    unblockSeed.mockReset();
    getSettings.mockReset();
    getSettings.mockResolvedValue({ "llm_web.search_provider": "exa", "curator.provider": "none" });
    setUserRowOverride.mockResolvedValue({});
  });

  it("offers no mix button while the effective counts are both zero", async () => {
    getUserRows.mockResolvedValue([row()]);
    renderSection();
    await screen.findByText("Picked for You");
    expect(screen.queryByRole("button", { name: /this week.s mix/i })).toBeNull();
  });

  it("fetches the mix only when asked, and shows three lists with the recent one cut at 5", async () => {
    getUserRows.mockResolvedValue([row({ favourite_count: 3, older_count: 3 })]);
    getHistoryMix.mockResolvedValue(MIX);
    renderSection();

    await userEvent.click(await screen.findByRole("button", { name: /Show this week.s mix/i }));
    expect(await screen.findByText("Heat (1995)")).toBeInTheDocument();
    expect(getHistoryMix).toHaveBeenCalledWith(7, 3);
    expect(screen.getByText("Recent 5 (2024)")).toBeInTheDocument();
    expect(screen.queryByText("Recent 6 (2024)")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "+2 more" }));
    expect(screen.getByText("Recent 7 (2024)")).toBeInTheDocument();
    expect(screen.getByText("Alien (1979)")).toBeInTheDocument();
  });

  it("does not fetch before the button is pressed", async () => {
    getUserRows.mockResolvedValue([row({ favourite_count: 3, older_count: 3 })]);
    renderSection();
    await screen.findByRole("button", { name: /Show this week.s mix/i });
    expect(getHistoryMix).not.toHaveBeenCalled();
  });

  it("blocks a favourite with Don't use, then offers Undo that unblocks it", async () => {
    getUserRows.mockResolvedValue([row({ favourite_count: 3, older_count: 3 })]);
    getHistoryMix.mockResolvedValue(MIX);
    blockSeed.mockResolvedValue({ blocked_seeds: [{ tmdb_id: 949, title: "Heat", media_type: "movie", year: 1995 }] });
    unblockSeed.mockResolvedValue({ blocked_seeds: [] });
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: /Show this week.s mix/i }));

    await userEvent.click(await screen.findByRole("button", { name: /Don.t use Heat/ }));
    expect(blockSeed).toHaveBeenCalledWith(7, expect.objectContaining({ tmdbId: 949, title: "Heat" }));
    await userEvent.click(await screen.findByRole("button", { name: /Undo Heat/ }));
    expect(unblockSeed).toHaveBeenCalledWith(7, 949);
    expect(await screen.findByRole("button", { name: /Don.t use Heat/ })).toBeInTheDocument();
  });

  it("shows a failed block inline", async () => {
    getUserRows.mockResolvedValue([row({ favourite_count: 3, older_count: 3 })]);
    getHistoryMix.mockResolvedValue(MIX);
    blockSeed.mockRejectedValue(new ApiError(500, "Database is busy."));
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: /Show this week.s mix/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Don.t use Heat/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Database is busy/);
  });

  it("shows a failed read inline rather than a blank", async () => {
    getUserRows.mockResolvedValue([row({ favourite_count: 3, older_count: 3 })]);
    getHistoryMix.mockRejectedValue(new ApiError(502, "Plex could not be reached."));
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: /Show this week.s mix/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Plex could not be reached/);
  });

  it("saves a person's own mix, and clears it back to the row default", async () => {
    getUserRows.mockResolvedValue([row({ favourite_count: 3, older_count: 3 })]);
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: /Customize for this person/i }));

    expect(screen.getByRole("radio", { name: /Row default \(A little older\)/ })).toHaveAttribute("aria-checked", "true");
    await userEvent.click(screen.getByRole("radio", { name: "Deep" }));
    await waitFor(() =>
      expect(setUserRowOverride.mock.calls.at(-1)?.[2]).toMatchObject({ favourite_count: 10, older_count: 10 }),
    );

    await userEvent.click(screen.getByRole("radio", { name: /Row default/ }));
    await waitFor(() =>
      expect(setUserRowOverride.mock.calls.at(-1)?.[2]).toMatchObject({ favourite_count: null, older_count: null }),
    );
  });

  it("names the configured search setup under the field", async () => {
    getUserRows.mockResolvedValue([row()]);
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: /Customize for this person/i }));
    expect(await screen.findByText(/one more Exa search, cached a week\.$/)).toBeInTheDocument();
  });
});
