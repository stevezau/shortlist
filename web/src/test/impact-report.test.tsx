import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ImpactReport } from "@/components/dashboard/impact-report";
import type * as ApiModule from "@/lib/api";
import type { EffectivenessReport, ReportWindow } from "@/lib/types";

const { getReport, syncWatched } = vi.hoisted(() => ({
  getReport: vi.fn(),
  syncWatched: vi.fn(() => Promise.resolve({ started: true })),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getReport: (window: ReportWindow) => getReport(window),
      syncWatched: () => syncWatched(),
    },
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

const LANDING = {
  delivered: 10,
  watched: 4,
  rate: 0.4,
  cohort_from: "2026-05-30T00:00:00Z",
  cohort_to: "2026-06-29T00:00:00Z",
  matured_days: 30,
};

const EMPTY = {
  window: "30" as ReportWindow,
  window_days: 30,
  since: "2026-06-29T00:00:00Z",
  watch_sync: { last: null, next: null },
  coverage: {
    users_enabled: 2,
    users_total: 2,
    users_with_picks: 1,
    users_watched: 1,
    users_watched_delta: 1,
    rows_enabled: 1,
  },
  runs: {
    total: 3,
    in_window: 3,
    in_window_delta: 0,
    last_finished: null,
    last_status: "ok",
    errors_last: 0,
  },
  requests: { sent: 2, pending: 1, watched_after_sent: 1 },
  top_titles: [] as EffectivenessReport["top_titles"],
};

const REPORT: EffectivenessReport = {
  overall: {
    delivered: 10,
    watched: 4,
    watched_prev: 2,
    watched_delta: 2,
    avg_days_to_watch: 3.5,
    avg_days_to_watch_delta: -0.8,
    landing: LANDING,
  },
  ...EMPTY,
  top_titles: [
    { tmdb_id: 1, media_type: "movie", title: "Dune: Part Two", watchers: 3 },
  ],
  trend: [{ week: "2026-28", watched: 4 }],
  per_user: [{ username: "sarah", slug: "sarah", delivered: 6, watched: 3 }],
  per_row: [
    {
      slug: "picked",
      section_key: "10",
      library: "Movies",
      name: "✨ Movies Picked for You",
      delivered: 10,
      watched: 4,
    },
    {
      slug: "faves",
      section_key: "20",
      library: "TV Shows",
      name: "My Faves",
      delivered: 6,
      watched: 3,
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
  beforeEach(() => {
    getReport.mockReset();
    getReport.mockResolvedValue(REPORT);
  });

  it("shows the headline metrics, breakdowns, requests, and recent-watches feed", async () => {
    renderReport();

    expect(await screen.findByText("Watched")).toBeTruthy();
    expect(screen.getByText("People watching")).toBeTruthy();
    expect(screen.getByText("1 of 2")).toBeTruthy();
    expect(screen.getByText(/sent ·/i)).toBeTruthy(); // requests impact
    expect(
      screen.getByRole("link", { name: /full send log/i }),
    ).toHaveAttribute("href", "/requests?tab=sent"); // deep-links to the send-log tab
    expect(screen.getAllByText("sarah").length).toBeGreaterThan(0);
    // Counts, labelled — never "3 of 6". They are two different sets (watched-in-window vs
    // delivered-in-window), so a fraction makes "4 of 0" reachable when delivery paused.
    // Counts, labelled — never "3 of 6". Two different sets (watched-in-window vs
    // delivered-in-window), so a fraction makes "4 of 0" reachable when delivery paused.
    // "delivered", not "sent": the Requests card uses "sent" for Sonarr asks on this same page.
    // More than one line matches (a person and a row), so assert on count rather than uniqueness.
    expect(screen.getAllByText(/· 6 delivered/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/3 of 6/)).toBeNull();
    expect(screen.getAllByText("Dune: Part Two").length).toBeGreaterThan(0); // top titles + recent
    // By row is split per library: a {library_name} row reads its library in the name; a plain-named
    // row ("My Faves") carries a library badge instead.
    expect(
      screen.getAllByText("✨ Movies Picked for You").length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("My Faves")).toBeTruthy();
    expect(screen.getByText("TV Shows")).toBeTruthy(); // the library badge on the plain-named row
  });

  it("defaults to the 30-day window and refetches when it changes", async () => {
    renderReport();
    await screen.findByText("Watched");

    expect(getReport).toHaveBeenCalledWith("30");

    await userEvent.click(screen.getByRole("button", { name: "90 days" }));

    expect(getReport).toHaveBeenCalledWith("90");
  });

  it("states the landing rate over its matured cohort, not over every pick ever", async () => {
    renderReport();

    expect(await screen.findByText("Landing rate")).toBeTruthy();
    expect(screen.getByText("40%")).toBeTruthy();
    expect(screen.getByText(/4 of 10 picks delivered/i)).toBeTruthy();
    // The caveat is the point — without it "40%" is just another number with no meaning.
    expect(
      screen.getByText(/old enough to have had their full 30 days/i),
    ).toBeTruthy();
  });

  it("says so plainly when no pick is old enough to have a landing rate yet", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      overall: {
        ...REPORT.overall,
        landing: { ...LANDING, delivered: 0, watched: 0, rate: null },
      },
    });
    renderReport();

    expect(await screen.findByText(/Not enough time has passed/i)).toBeTruthy();
  });

  it("hides deleted rows behind a disclosure, and keeps their numbers", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      per_row: [
        ...REPORT.per_row,
        {
          slug: "zz-claude-test",
          section_key: "10",
          library: "Movies",
          name: "zz-claude-test",
          deleted: true,
          delivered: 5,
          watched: 0,
        },
      ],
    });
    renderReport();

    // Collapsed by default: a row you deleted is history, not something to scroll past.
    const toggle = await screen.findByRole("button", {
      name: /Show 1 deleted row/i,
    });
    expect(screen.queryByText("zz-claude-test")).toBeNull();

    await userEvent.click(toggle);

    expect(screen.getByText("zz-claude-test")).toBeTruthy();
    expect(screen.getByText(/still count in the totals above/i)).toBeTruthy();
  });

  it("folds away people with nothing watched in the window", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      per_user: [
        { username: "sarah", slug: "sarah", delivered: 6, watched: 3 },
        { username: "mike", slug: "mike", delivered: 80, watched: 0 },
        { username: "amy", slug: "amy", delivered: 95, watched: 0 },
      ],
    });
    renderReport();

    await screen.findAllByText("sarah");
    // A wall of empty bars says nothing; it is one click away, not deleted.
    expect(screen.queryByText("mike")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", {
        name: /2 people with none in this window/i,
      }),
    );

    expect(screen.getByText("mike")).toBeTruthy();
    expect(screen.getByText("amy")).toBeTruthy();
  });

  it("explains the empty state before anything is delivered", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      overall: {
        ...REPORT.overall,
        delivered: 0,
        watched: 0,
        landing: { ...LANDING, delivered: 0, watched: 0, rate: null },
      },
      runs: { ...EMPTY.runs, total: 0, in_window: 0 },
      requests: { sent: 0, pending: 0, watched_after_sent: 0 },
      trend: [],
      per_user: [],
      per_row: [],
      recent: [],
    });
    renderReport();

    expect(await screen.findByText(/No picks delivered yet/i)).toBeTruthy();
  });

  it("distinguishes an empty window from an empty install", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      overall: {
        ...REPORT.overall,
        delivered: 0,
        watched: 0,
        landing: { ...LANDING, delivered: 0, watched: 0, rate: null },
      },
      per_user: [],
      per_row: [],
      recent: [],
    });
    renderReport();

    // 3 runs exist, so "nothing yet" would be a lie — the window is just too short.
    expect(
      await screen.findByText(
        /Nothing delivered or watched in the last 30 days/i,
      ),
    ).toBeTruthy();
  });
});
