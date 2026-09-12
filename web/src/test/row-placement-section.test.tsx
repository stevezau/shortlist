import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowPlacementSection } from "@/components/settings/row-placement-section";
import { DOCS_SHELF_CONTENTION_URL } from "@/lib/support";
import type { Settings } from "@/lib/types";

const { putSettings, getLibraries, getLibraryCollections } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
  getLibraries: vi.fn(),
  getLibraryCollections: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  ApiError: class extends Error {},
  apiErrorMessage: (_e: unknown, fallback: string) => fallback,
  api: {
    putSettings: (values: Settings) => putSettings(values),
    getLibraries: () => getLibraries(),
    getLibraryCollections: (key: string) => getLibraryCollections(key),
  },
}));

function renderSection(settings: Settings = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RowPlacementSection settings={settings} />
    </QueryClientProvider>,
  );
}

describe("RowPlacementSection", () => {
  beforeEach(() => {
    putSettings.mockClear();
    getLibraries.mockResolvedValue([
      { key: "2", title: "TV Shows", type: "show" },
    ]);
    getLibraryCollections.mockResolvedValue([
      { title: "New Series (Unwatched)", on_shelf: true },
      { title: "Trending", on_shelf: true },
      { title: "Archive 2019", on_shelf: false },
    ]);
  });

  it("saves only the master switch — placement itself lives on the row", async () => {
    renderSection();

    await userEvent.click(
      screen.getByLabelText("Let Shortlist order the Recommended shelf"),
    );

    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith({
        "rows.manage_shelf_order": false,
      }),
    );
    // `rows.hub_anchor` is NOT written from here any more. It was a second source of truth for the
    // same decision and contradicted the engine: its "Wherever Plex puts them" wrote no entry, and
    // with no library configured the engine read that as "top of the shelf".
    expect(putSettings.mock.calls.every(([v]) => !("rows.hub_anchor" in v))).toBe(true);
  });

  it("offers no per-library controls at all", async () => {
    renderSection();
    await screen.findByText("Row placement");

    expect(screen.queryByLabelText("Place Shortlist rows")).toBeNull();
    expect(screen.queryByText("TV Shows")).toBeNull();
  });

  it("points at the row editor for where a row actually goes", async () => {
    renderSection({ "rows.manage_shelf_order": true });

    expect(await screen.findByText(/Where it sits/)).toBeTruthy();
  });

  it("says what happens when ordering is off — new rows land at the end of the shelf", async () => {
    renderSection({ "rows.manage_shelf_order": false });

    const note = await screen.findByText(/Shelf ordering is off/);
    expect(note.textContent).toMatch(/end of the shelf/);
  });

  it("keeps the Agregarr warning and its link", async () => {
    renderSection();

    // The warning that matters and is not in the guides: an unmaintained Agregarr re-promotes
    // collections with Plex's defaults, putting other people's rows on the OWNER's own Home.
    expect(
      await screen.findByText(/can put other people’s rows on/),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /How to run one alongside Shortlist/ }),
    ).toHaveAttribute("href", DOCS_SHELF_CONTENTION_URL);
  });
});
