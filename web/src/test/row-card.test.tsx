import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { RowCard } from "@/components/rows/row-card";
import type { Collection, User } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    posterImageUrl: (id: number) => `/api/collections/${id}/poster/image`,
    getSettings: () => Promise.resolve({ "row.size": "15" }),
    getLibraries: () =>
      Promise.resolve([
        { key: "1", title: "Movies", type: "movie" },
        { key: "2", title: "4K Movies", type: "movie" },
      ]),
  },
}));

const USERS: User[] = [];

function collection(patch: Partial<Collection> = {}): Collection {
  return {
    id: 1,
    slug: "hidden-gems",
    name: "Hidden Gems",
    last_run_id: null,
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    freshness: null,
    placement: "both",
    pin_top: false,
    hub_anchor: {},
    ...patch,
  } as Collection;
}

function renderCard(value: Collection) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RowCard collection={value} users={USERS} onEdit={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowCard", () => {
  it("shows a row's own sources and libraries so overrides are visible without opening it", async () => {
    renderCard(
      collection({
        candidate_sources: ["trakt"],
        library_keys: ["2"],
      }),
    );
    expect(await screen.findByText("Sources: Trakt")).toBeTruthy();
    expect(await screen.findByText("Libraries: 4K Movies")).toBeTruthy();
  });

  it("shows no override badges for a row that follows the global defaults", () => {
    renderCard(collection());
    expect(screen.queryByText(/^Sources:/)).toBeNull();
    expect(screen.queryByText(/^Libraries:/)).toBeNull();
  });

  // The poster slot is fixed-width whether or not there's an image, so every card in the list lines
  // up. Rendering the <img> conditionally with nothing in its place shifted posterless rows left.
  it("keeps a poster-sized slot for a row with no poster", () => {
    const { container } = renderCard(collection());
    expect(container.querySelector("img")).toBeNull();
    const slot = container.querySelector('[title^="No poster"]');
    expect(slot).toBeTruthy();
    expect(slot?.className).toContain("h-16");
    expect(slot?.className).toContain("w-11");
  });

  it("shows the image, and no placeholder, for a row that has a poster", () => {
    const { container } = renderCard(
      collection({
        poster: {
          mode: "upload",
          title: "",
          subtitle: "",
          style: "",
          has_image: true,
        },
      }),
    );
    const img = container.querySelector("img");
    expect(img).toBeTruthy();
    expect(img?.className).toContain("h-16");
    expect(container.querySelector('[title^="No poster"]')).toBeNull();
  });
});
