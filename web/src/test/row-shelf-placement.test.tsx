import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import type { HubAnchorMap } from "@/lib/types";

const { getLibraries, getLibraryCollections } = vi.hoisted(() => ({
  getLibraries: vi.fn(),
  getLibraryCollections: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getLibraries: () => getLibraries(),
    getLibraryCollections: (key: string) => getLibraryCollections(key),
  },
}));

/** Controlled harness that records the latest hub_anchor the control emits. */
function Harness({
  start,
  onChange,
}: {
  start: HubAnchorMap;
  onChange: (m: HubAnchorMap) => void;
}) {
  const [value, setValue] = useState<HubAnchorMap>(start);
  return (
    <RowShelfPlacement
      value={value}
      libraryKeys={[]}
      media="both"
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

function renderControl(start: HubAnchorMap = {}) {
  const latest = { value: start };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <Harness start={start} onChange={(m) => (latest.value = m)} />
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
      await screen.findByLabelText("Collection"),
      "New Series",
    );
    await waitFor(() =>
      expect(latest.value).toEqual({
        "2": { anchor: "New Series", before: true },
      }),
    );

    await userEvent.selectOptions(screen.getByLabelText("Position"), "default");
    await waitFor(() => expect(latest.value).toEqual({}));
  });
});
