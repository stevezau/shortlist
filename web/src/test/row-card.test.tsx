import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { RowCard } from "@/components/rows/row-card";
import type { Collection, User } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
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
  render(
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
});
