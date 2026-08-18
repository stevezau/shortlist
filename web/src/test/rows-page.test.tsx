import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { Collection } from "@/lib/types";
import { RowsPage } from "@/pages/rows";

const { getUsers, listCollections } = vi.hoisted(() => ({
  getUsers: vi.fn(),
  listCollections: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      listCollections: () => listCollections(),
      getSettings: () => Promise.resolve({}),
      getLibraries: () => Promise.resolve([]),
    },
  };
});

const SUBSET_ROW: Collection = {
  id: 1,
  slug: "hidden-gems",
  name: "Hidden Gems",
  last_run_id: null,
  build: "per_person",
  audience: "subset",
  audience_user_ids: [4],
  enabled: true,
  schedule: "30 3 * * *",
  size: 15,
  media: "both",
  sort_order: 0,
  name_template: "",
  fallback_name: "",
  min_watchers: 2,
  request_tag: "",
  candidate_sources: [],
  library_keys: [],
  watched_pct: null,
  rewatch: false,
  unstarted_only: false,
  refresh_days: null,
  recency: null,
  recent_count: null,
  max_seeds: null,
  cold_start: null,
  req_min_rating: null,
  req_min_votes: null,
  req_min_demand: null,
  req_min_year: null,
  req_max_year: null,
  req_auto_send: null,
  req_auto_min_demand: null,
  req_auto_min_rating: null,
  req_max_per_row: null,
  req_radarr_quality_profile_id: null,
  req_radarr_root_folder: null,
  req_sonarr_quality_profile_id: null,
  req_sonarr_root_folder: null,
  seed_window: 1,
  pick_order: "best",
  placement: "both",
  placement_friends: "both",
  pin_top: false,
  hub_anchor: {},
  poster: { mode: "", title: "", subtitle: "", style: "", has_image: false },
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RowsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowsPage", () => {
  beforeEach(() => {
    getUsers.mockReset();
    listCollections.mockReset();
  });

  it("never says a row reaches 'No one yet' just because the user list failed to load", async () => {
    getUsers.mockRejectedValue(new ApiError(500, "Couldn’t load your users."));
    listCollections.mockResolvedValue([SUBSET_ROW]);
    renderPage();

    // `usersQuery.data ?? []` used to swallow the failure and report a real audience as "No one yet",
    // and would have offered an empty audience list in the editor.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Couldn’t load your users/i,
    );
    expect(screen.queryByText(/No one yet/i)).toBeNull();
    expect(screen.getByRole("button", { name: /Add a row/i })).toBeDisabled();
  });

  it("names the audience once the users are known", async () => {
    getUsers.mockResolvedValue([
      {
        id: 4,
        username: "sarah",
        slug: "sarah",
        user_type: "shared",
        restricted: false,
        enabled: true,
        cold_start: false,
        history_depth: 10,
        last_run_at: null,
        request_tag: "",
        hit_rate: null,
      },
    ]);
    listCollections.mockResolvedValue([SUBSET_ROW]);
    renderPage();

    expect(await screen.findByText(/sarah · 15 titles/i)).toBeTruthy();
  });
});
