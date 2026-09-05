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

  it("links the co-managing-tool guide even with shelf ordering switched off", async () => {
    // This is the ONLY place a "Wherever Plex puts them" owner is pointed at it. The
    // shelf-contention notification carries the same advice, but that notification only fires
    // while Shortlist is ordering the shelf — and switching that off is the fix it recommends.
    //
    // Changed by the audit: the advice used to be printed here in full — a 458-character
    // paragraph with a fork recommendation, a GitHub URL and a Docker image name, shown to every
    // owner whether or not they run any of those tools. It is a link now; the guide holds the
    // detail, unabridged.
    renderSection({ "rows.manage_shelf_order": false });

    const link = await screen.findByRole("link", {
      name: /alongside Shortlist/i,
    });
    expect(link).toHaveAttribute("href", DOCS_SHELF_CONTENTION_URL);
    expect(link).toHaveAttribute("target", "_blank");
    // The apparatus goes; the WARNING stays, because it is the half that matters and the guide
    // does not carry it. Hedged, as it always was — Shortlist clears the re-promotion every run.
    expect(
      screen.getByText(/other people’s rows on/i, { exact: false }),
    ).toBeInTheDocument();
    expect(screen.getByText(/between runs/i)).toBeInTheDocument();
    expect(screen.queryByText(/no longer actively released/i)).toBeNull();
    expect(screen.queryByText(/bitr8\/agregarr-dev/i)).toBeNull();
  });
  it("won't let the global default anchor to a collection that has no shelf position", async () => {
    // Same trap as the per-row picker (issue #106), reachable from Settings too.
    renderSection({
      "rows.hub_anchor": { "2": { anchor: "Archive 2019", before: false } },
    });
    expect(await screen.findByText("TV Shows")).toBeTruthy();

    const anchor = await screen.findByLabelText("Collection");
    const offShelf = Array.from(anchor.querySelectorAll("option")).find(
      (o) => o.textContent === "Archive 2019",
    );
    expect(offShelf?.disabled).toBe(true);
    expect(
      screen.getByText(/isn’t on any of this library’s Plex shelves/),
    ).toBeTruthy();
  });
});
