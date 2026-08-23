import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Engagement } from "@/components/dashboard/engagement";
import type * as ApiModule from "@/lib/api";
import type { EngagementReport } from "@/lib/types";

const { getEngagement } = vi.hoisted(() => ({ getEngagement: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, getEngagement: () => getEngagement() } };
});

const REPORT: EngagementReport = {
  window: "30",
  observed: true,
  people: [
    {
      username: "alex",
      display_name: "Alex",
      total: 4,
      picks: [
        { title: "Bounced Off", row: "Picked", media_type: "movie", outcome: "bounced",
          percent: 2, watched_at: "2026-08-23T10:00:00+00:00", finished_at: null,
          observed_at: "2026-08-23T10:00:00+00:00" },
        { title: "Gave Up", row: "Picked", media_type: "movie", outcome: "dropped",
          percent: 40, watched_at: "2026-08-22T10:00:00+00:00", finished_at: null,
          observed_at: "2026-08-22T10:00:00+00:00" },
        { title: "Seen Out", row: "Picked", media_type: "movie", outcome: "finished",
          percent: 100, watched_at: "2026-08-21T10:00:00+00:00",
          finished_at: "2026-08-21T12:00:00+00:00", observed_at: "2026-08-21T10:00:00+00:00" },
        // The state every pre-tracking pick is in, and every show: credited, progress unknown.
        { title: "Unobserved", row: "Picked", media_type: "show", outcome: "watching",
          percent: null, watched_at: null, finished_at: null, observed_at: "2026-08-20T10:00:00+00:00" },
      ],
    },
  ],
  losing: [
    { title: "Loses People", media_type: "movie", started: 3, finished: 0, stops_at: 12 },
  ],
  stop_points: [
    { label: "0-10%", count: 1 },
    { label: "10-25%", count: 2 },
    { label: "25-50%", count: 1 },
    { label: "50-75%", count: 0 },
    { label: "75%+", count: 0 },
  ],
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Engagement reportWindow="30" />
    </QueryClientProvider>,
  );
}

describe("Engagement", () => {
  it("labels all four outcomes distinctly", async () => {
    getEngagement.mockResolvedValue(REPORT);
    renderPanel();

    expect(await screen.findByText("Bounced")).toBeInTheDocument();
    expect(screen.getByText("Dropped")).toBeInTheDocument();
    expect(screen.getByText("Finished")).toBeInTheDocument();
    expect(screen.getByText("Watching")).toBeInTheDocument();
  });

  it("shows an em-dash, never 0%, when the progress was not observed", async () => {
    getEngagement.mockResolvedValue(REPORT);
    renderPanel();

    const row = (await screen.findByText("Unobserved")).closest("li")!;
    expect(row).toHaveTextContent("—");
    expect(row).not.toHaveTextContent("0%");
  });

  it("says how many were shown when the list is capped", async () => {
    getEngagement.mockResolvedValue({
      ...REPORT,
      people: [{ ...REPORT.people[0], total: 63 }],
    });
    renderPanel();

    expect(await screen.findByText(/4 of 63 picks/)).toBeInTheDocument();
  });

  it("explains itself rather than rendering zeroes when nothing has been observed", async () => {
    getEngagement.mockResolvedValue({
      window: "30", observed: false, people: REPORT.people, losing: [],
      stop_points: REPORT.stop_points.map((p) => ({ ...p, count: 0 })),
    });
    renderPanel();

    expect(await screen.findByText(/No playback observed yet/i)).toBeInTheDocument();
    // The old gate was `people.length === 0`, which never fires on a server with existing picks —
    // the owner got a wall of "WATCHING · —" rows and five empty bars instead of this sentence.
    expect(screen.queryByText("Bounced")).not.toBeInTheDocument();
  });

  it("says films only, because a series cannot produce a percentage", async () => {
    getEngagement.mockResolvedValue({ ...REPORT, observed: false });
    renderPanel();

    expect(await screen.findByText(/Films only/i)).toBeInTheDocument();
  });

  it("holds back 'picks that lose people' until there is a pattern", async () => {
    getEngagement.mockResolvedValue({ ...REPORT, losing: [] });
    renderPanel();

    expect(await screen.findByText(/not a signal/i)).toBeInTheDocument();
  });
});
