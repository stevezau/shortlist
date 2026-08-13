import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowPlacementSection } from "@/components/settings/row-placement-section";
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
      { title: "New Series (Unwatched)" },
      { title: "Trending" },
    ]);
  });

  it("anchors a library's rows after a chosen collection and saves the mapping", async () => {
    renderSection();
    expect(await screen.findByText("TV Shows")).toBeTruthy();

    // Choose "after a collection", then pick one from the live dropdown.
    await userEvent.selectOptions(
      screen.getByLabelText("Place Shortlist rows"),
      "after",
    );
    const anchor = await screen.findByLabelText("Collection");
    await userEvent.selectOptions(anchor, "New Series (Unwatched)");

    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toEqual({
        "rows.hub_anchor": {
          "2": { anchor: "New Series (Unwatched)", before: false },
        },
        "rows.manage_shelf_order": true,
      }),
    );
  });

  it("does not persist a library whose mode is set but no collection is chosen yet", async () => {
    renderSection();
    await screen.findByText("TV Shows");
    await userEvent.selectOptions(
      screen.getByLabelText("Place Shortlist rows"),
      "after",
    );

    // The prompt shows, and nothing half-set reaches the backend.
    expect(
      await screen.findByText(/Pick a collection to anchor to/i),
    ).toBeTruthy();
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    for (const [payload] of putSettings.mock.calls) {
      expect(payload["rows.hub_anchor"]).toEqual({});
    }
  });

  it("loads an existing anchor and lets it be cleared back to the Plex default", async () => {
    renderSection({
      "rows.hub_anchor": { "2": { anchor: "Trending", before: true } },
    });
    await screen.findByText("TV Shows");
    // Mode reflects the saved 'before' anchor.
    expect(screen.getByLabelText("Place Shortlist rows")).toHaveValue("before");

    await userEvent.selectOptions(
      screen.getByLabelText("Place Shortlist rows"),
      "default",
    );
    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toEqual({
        "rows.hub_anchor": {},
        "rows.manage_shelf_order": true,
      }),
    );
  });

  it("hides the per-library controls and skips ordering when the master toggle is off", async () => {
    renderSection({ "rows.manage_shelf_order": false });
    await userEvent.click(
      screen.getByLabelText(/Let Shortlist order the Recommended shelf/i),
    );
    // Toggling on reveals the library controls; toggling off hides them.
    expect(await screen.findByText("TV Shows")).toBeTruthy();
    await userEvent.click(
      screen.getByLabelText(/Let Shortlist order the Recommended shelf/i),
    );
    expect(screen.queryByText("TV Shows")).toBeNull();
    expect(screen.getByText(/Shelf ordering is off/i)).toBeTruthy();
    // The off state must actually persist, not just hide the controls.
    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toMatchObject({
        "rows.manage_shelf_order": false,
      }),
    );
  });

  it("names the maintained Agregarr fork even with shelf ordering switched off", async () => {
    // This is the ONLY place a "Wherever Plex puts them" owner sees it. The shelf-contention
    // notification carries the same advice, but that notification only fires while Shortlist is
    // ordering the shelf — and switching that off is the fix it recommends. Owners who take that
    // advice would otherwise never be told the version they run re-promotes rows onto their own
    // Home, which no share filter can cover.
    renderSection({ "rows.manage_shelf_order": false });

    const text = (await screen.findByText(/no longer actively released/i))
      .textContent;
    expect(text).toMatch(/re-promotes collections/i);
    // The claim must stay hedged: converge clears the flag every run, so this is a gap between
    // runs, not a standing leak. Overstating it in copy we ship is what the review caught.
    expect(text).toMatch(/gap between runs/i);
    expect(
      screen.getByRole("link", { name: /bitr8\/agregarr-dev/i }),
    ).toHaveAttribute("href", "https://github.com/bitr8/agregarr-dev");
  });
});
