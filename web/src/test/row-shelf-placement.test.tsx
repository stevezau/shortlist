import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import type * as ApiModule from "@/lib/api";
import type { HubAnchorMap } from "@/lib/types";

const { getLibraries, getLibraryCollections, listCollections } = vi.hoisted(
  () => ({
    getLibraries: vi.fn(),
    getLibraryCollections: vi.fn(),
    listCollections: vi.fn(),
  }),
);

// Preserve the real module (notably the `ApiError` export QueryBoundary's ErrorState relies on for
// `error instanceof ApiError`); override only the `api` client. A bare `{ api }` mock drops ApiError
// and the error-state render throws.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getLibraries: () => getLibraries(),
      getLibraryCollections: (key: string) => getLibraryCollections(key),
      listCollections: () => listCollections(),
    },
  };
});

/** Controlled harness that records the latest hub_anchor the control emits. */
function Harness({
  start,
  onChange,
  pinnedTop,
  onConsumePin,
}: {
  start: HubAnchorMap;
  onChange: (m: HubAnchorMap) => void;
  pinnedTop?: boolean;
  onConsumePin?: () => void;
}) {
  const [value, setValue] = useState<HubAnchorMap>(start);
  return (
    <RowShelfPlacement
      value={value}
      libraryKeys={[]}
      media="both"
      rowSlug="because"
      pinnedTop={pinnedTop}
      onConsumePin={onConsumePin}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

function renderControl(
  start: HubAnchorMap = {},
  opts: { pinnedTop?: boolean; onConsumePin?: () => void } = {},
) {
  const latest = { value: start };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <Harness start={start} onChange={(m) => (latest.value = m)} {...opts} />
    </QueryClientProvider>,
  );
  return latest;
}

describe("RowShelfPlacement", () => {
  beforeEach(() => {
    getLibraries.mockResolvedValue([
      { key: "2", title: "TV Shows", type: "show" },
    ]);
    getLibraryCollections.mockResolvedValue([
      { title: "New Series" },
      { title: "Trending" },
    ]);
    // The row being edited ("because") plus two siblings — only the siblings may be offered.
    listCollections.mockResolvedValue([
      {
        slug: "because",
        name: "Because you watched",
        media: "both",
        library_keys: [],
      },
      {
        slug: "picked",
        name: "Picked for You",
        media: "both",
        library_keys: [],
      },
      {
        slug: "popular",
        name: "Popular on SFLIX",
        media: "both",
        library_keys: [],
      },
      // Movies-only: it builds nothing in the TV library this control renders, so it must not be
      // offered there — saving it would look fine and then be skipped every run, silently.
      {
        slug: "movie-only",
        name: "Movie Nights",
        media: "movie",
        library_keys: [],
      },
    ]);
  });

  it("defaults each targeted library to inheriting the global setting (no entry)", async () => {
    renderControl();
    expect(await screen.findByText("TV Shows")).toBeTruthy();
    expect(screen.getByLabelText("Position")).toHaveValue("default");
  });

  it("sets a per-row anchor when a collection is chosen, and clears it back to default", async () => {
    const latest = renderControl();
    await screen.findByText("TV Shows");

    await userEvent.selectOptions(screen.getByLabelText("Position"), "before");
    await userEvent.selectOptions(
      await screen.findByLabelText("Before"),
      "coll:New Series",
    );
    await waitFor(() =>
      expect(latest.value).toEqual({
        "2": { anchor: "New Series", row: "", before: true },
      }),
    );

    await userEvent.selectOptions(screen.getByLabelText("Position"), "default");
    await waitFor(() => expect(latest.value).toEqual({}));
  });

  it("offers the OTHER Shortlist rows as anchors, and never the row being edited", async () => {
    // Issue #81. The picker used to offer forty identical-looking "Picked for You" entries — one
    // Plex collection per person, all rendering the same — and placing any of them did nothing. A
    // row is chosen as a ROW, so there is exactly one entry for it whatever the roster size.
    renderControl();
    await screen.findByText("TV Shows");
    await userEvent.selectOptions(screen.getByLabelText("Position"), "after");

    const select = await screen.findByLabelText("After");
    const labels = Array.from(select.querySelectorAll("option")).map(
      (o) => o.textContent,
    );
    expect(labels).toContain("Picked for You");
    expect(labels).toContain("Popular on SFLIX");
    expect(labels).not.toContain("Because you watched");
  });

  it("never offers a row that builds nothing in this library", async () => {
    renderControl();
    await screen.findByText("TV Shows");
    await userEvent.selectOptions(screen.getByLabelText("Position"), "after");

    const select = await screen.findByLabelText("After");
    const labels = Array.from(select.querySelectorAll("option")).map(
      (o) => o.textContent,
    );
    expect(labels).toContain("Picked for You");
    expect(labels).not.toContain("Movie Nights");
  });

  it("saves a row anchor as a slug, not a title", async () => {
    // A title names ONE account's copy of a per-person row, so it can only ever place the row for
    // that one account. The slug names the row itself, and each library resolves it to that row's
    // own collections.
    const latest = renderControl();
    await screen.findByText("TV Shows");

    await userEvent.selectOptions(screen.getByLabelText("Position"), "after");
    await userEvent.selectOptions(
      await screen.findByLabelText("After"),
      "row:picked",
    );

    await waitFor(() =>
      expect(latest.value).toEqual({
        "2": { row: "picked", anchor: "", before: false },
      }),
    );
  });

  it("keeps showing a saved row anchor whose row is gone, rather than reading as unset", async () => {
    const latest = renderControl({
      "2": { row: "deleted-row", before: false },
    });
    await screen.findByText("TV Shows");

    const select = await screen.findByLabelText("After");
    expect(
      Array.from(select.querySelectorAll("option")).map((o) => o.textContent),
    ).toContain("deleted-row (row not found)");
    expect(latest.value).toEqual({
      "2": { row: "deleted-row", before: false },
    });
  });

  it("sets a per-row 'Top' with no collection needed", async () => {
    const latest = renderControl();
    await screen.findByText("TV Shows");

    await userEvent.selectOptions(screen.getByLabelText("Position"), "top");
    await waitFor(() => expect(latest.value).toEqual({ "2": { top: true } }));
    // Top needs no collection dropdown.
    expect(screen.queryByLabelText("Collection")).toBeNull();
  });

  it("carries a legacy row-level pin over into per-library Top, once, and consumes the pin", async () => {
    const onConsumePin = vi.fn();
    const latest = renderControl({}, { pinnedTop: true, onConsumePin });

    await screen.findByText("TV Shows");
    await waitFor(() => expect(latest.value).toEqual({ "2": { top: true } }));
    expect(onConsumePin).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Position")).toHaveValue("top");
  });

  it("does not consume the pin (so pin_top survives) when libraries can't load", async () => {
    getLibraries.mockRejectedValue(new Error("Plex is down"));
    const onConsumePin = vi.fn();
    const latest = renderControl({}, { pinnedTop: true, onConsumePin });

    await waitFor(() => expect(getLibraries).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0)); // let the effect (not) fire
    expect(onConsumePin).not.toHaveBeenCalled(); // pin_top left intact by the editor
    expect(latest.value).toEqual({});
  });

  it("does not re-pin a library the user has moved off Top", async () => {
    const latest = renderControl({}, { pinnedTop: true });
    await screen.findByText("TV Shows");
    await waitFor(() => expect(latest.value).toEqual({ "2": { top: true } }));

    await userEvent.selectOptions(screen.getByLabelText("Position"), "default");
    await new Promise((r) => setTimeout(r, 0)); // give the effect a chance to (wrongly) re-materialize
    expect(latest.value).toEqual({}); // the ref guard keeps it from coming back
  });
});
