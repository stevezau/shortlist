import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ImpactReport } from "@/components/dashboard/impact-report";
import type * as ApiModule from "@/lib/api";
import type { EffectivenessReport } from "@/lib/types";

const { getReport, syncWatched } = vi.hoisted(() => ({
  getReport: vi.fn(),
  syncWatched: vi.fn(() => Promise.resolve({ started: true })),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { getReport: () => getReport(), syncWatched: () => syncWatched() },
  };
});

function renderReport() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ImpactReport />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const EMPTY = {
  watch_sync: { last: null, next: null },
  coverage: {
    users_enabled: 2,
    users_total: 2,
    users_with_picks: 1,
    rows_enabled: 1,
  },
  runs: { total: 3, last_finished: null, last_status: "ok", errors_last: 0 },
  requests: { sent: 2, pending: 1, watched_after_sent: 1 },
  top_titles: [] as EffectivenessReport["top_titles"],
};

const REPORT: EffectivenessReport = {
  overall: {
    delivered: 10,
    watched: 4,
    hit_rate: 0.4,
    watched_last_7d: 2,
    avg_days_to_watch: 3.5,
  },
  ...EMPTY,
  top_titles: [
    { tmdb_id: 1, media_type: "movie", title: "Dune: Part Two", watchers: 3 },
  ],
  trend: [{ week: "2026-28", watched: 4 }],
  per_user: [
    {
      username: "sarah",
      slug: "sarah",
      delivered: 6,
      watched: 3,
      hit_rate: 0.5,
    },
  ],
  per_row: [
    {
      slug: "picked",
      section_key: "10",
      library: "Movies",
      name: "✨ Movies Picked for You",
      delivered: 10,
      watched: 4,
      hit_rate: 0.4,
    },
    {
      slug: "faves",
      section_key: "20",
      library: "TV Shows",
      name: "My Faves",
      delivered: 6,
      watched: 3,
      hit_rate: 0.5,
    },
  ],
  recent: [
    {
      username: "sarah",
      title: "Dune: Part Two",
      media_type: "movie",
      row: "✨ Movies Picked for You",
      library: "Movies",
      seed_title: "Arrival",
      watched_at: new Date().toISOString(),
    },
  ],
};

describe("ImpactReport", () => {
  beforeEach(() => getReport.mockReset());

  it("shows the headline metrics, breakdowns, requests, and recent-watches feed", async () => {
    getReport.mockResolvedValue(REPORT);
    renderReport();

    expect(await screen.findByText("Hit rate")).toBeTruthy();
    expect(screen.getAllByText("40%").length).toBeGreaterThan(0); // overall + row hit rate
    expect(screen.getByText(/of 10 delivered/i)).toBeTruthy(); // Watched tile hint
    expect(screen.getByText(/sent to Sonarr\/Radarr/i)).toBeTruthy(); // requests impact
    expect(
      screen.getByRole("link", { name: /full send log/i }),
    ).toHaveAttribute("href", "/requests?tab=sent"); // deep-links to the send-log tab
    expect(screen.getAllByText("sarah").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Dune: Part Two").length).toBeGreaterThan(0); // top titles + recent
    // By row is split per library: a {library_name} row reads its library in the name; a plain-named
    // row ("My Faves") carries a library badge instead.
    expect(
      screen.getAllByText("✨ Movies Picked for You").length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("My Faves")).toBeTruthy();
    expect(screen.getByText("TV Shows")).toBeTruthy(); // the library badge on the plain-named row
  });

  it("explains the empty state before anything is delivered", async () => {
    getReport.mockResolvedValue({
      overall: {
        delivered: 0,
        watched: 0,
        hit_rate: null,
        watched_last_7d: 0,
        avg_days_to_watch: null,
      },
      ...EMPTY,
      requests: { sent: 0, pending: 0, watched_after_sent: 0 },
      trend: [],
      per_user: [],
      per_row: [],
      recent: [],
    });
    renderReport();

    expect(await screen.findByText(/No picks delivered yet/i)).toBeTruthy();
  });
});
