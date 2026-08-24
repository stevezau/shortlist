import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NeedsALook } from "@/components/dashboard/engagement";
import type { EffectivenessReport, EngagementReport } from "@/lib/types";

const getEngagement = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { getEngagement: (...a: unknown[]) => getEngagement(...a) },
}));

const ENGAGEMENT: EngagementReport = {
  window: "30",
  observed: true,
  people: [
    {
      username: "alex",
      display_name: "Alex",
      total: 2,
      picks: [
        {
          title: "Bailed Early",
          row: "Picked",
          media_type: "movie",
          outcome: "bounced",
          percent: 3,
          watched_at: "2026-08-22T10:00:00+00:00",
          finished_at: null,
          observed_at: "2026-08-22T10:00:00+00:00",
        },
        {
          title: "Saw It Out",
          row: "Picked",
          media_type: "movie",
          outcome: "finished",
          percent: 100,
          watched_at: "2026-08-21T10:00:00+00:00",
          finished_at: "2026-08-21T12:00:00+00:00",
          observed_at: "2026-08-21T10:00:00+00:00",
        },
      ],
    },
    {
      username: "quinn",
      display_name: "Quinn",
      total: 1,
      picks: [
        {
          title: "Most Of It",
          row: "Picked",
          media_type: "movie",
          outcome: "dropped",
          percent: 70,
          watched_at: "2026-08-20T10:00:00+00:00",
          finished_at: null,
          observed_at: "2026-08-20T10:00:00+00:00",
        },
      ],
    },
  ],
  losing: [],
  stop_points: [],
};

function report(over: Partial<EffectivenessReport> = {}): EffectivenessReport {
  return {
    coverage: { users_with_picks: 10, users_watched: 4 },
    per_row: [],
    requests: { sent: 0, pending: 0, watched_after_sent: 0 },
    ...over,
  } as unknown as EffectivenessReport;
}

function renderPanel(r = report()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <NeedsALook report={r} reportWindow="30" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getEngagement.mockReset();
  getEngagement.mockResolvedValue(ENGAGEMENT);
});

describe("NeedsALook", () => {
  it("leads with the people who got picks and watched nothing", async () => {
    // The biggest silent failure on a server: rows delivered, nobody looked. Nothing else on the
    // dashboard says it — every other figure counts what people DID.
    renderPanel();

    expect(
      await screen.findByText(/got picks and watched none/),
    ).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
  });

  it("says so plainly when more than half the server ignored their row", async () => {
    // "6 of 10" and "9 of 10" are different problems: the second is not a picking problem at all.
    renderPanel(
      report({ coverage: { users_with_picks: 10, users_watched: 1 } } as never),
    );

    expect(await screen.findByText(/More than half/)).toBeInTheDocument();
  });

  it("names a row that delivered and landed nothing", async () => {
    renderPanel(
      report({
        per_row: [
          {
            slug: "dud",
            library: "Movies",
            name: "Dud Row",
            delivered: 400,
            watched: 0,
            finished: 0,
            deleted: false,
          },
          {
            slug: "ok",
            library: "Movies",
            name: "Fine Row",
            delivered: 400,
            watched: 9,
            finished: 9,
            deleted: false,
          },
        ],
      } as never),
    );

    expect(await screen.findByText("Dud Row")).toBeInTheDocument();
    expect(screen.queryByText("Fine Row")).not.toBeInTheDocument();
  });

  it("ignores a row too small to conclude anything from", async () => {
    // Three picks and no watches is a quiet week, not a broken row.
    renderPanel(
      report({
        per_row: [
          {
            slug: "tiny",
            library: "Movies",
            name: "Tiny Row",
            delivered: 3,
            watched: 0,
            finished: 0,
            deleted: false,
          },
        ],
      } as never),
    );

    await screen.findByText(/got picks and watched none/);
    expect(screen.queryByText("Tiny Row")).not.toBeInTheDocument();
  });

  it("puts the worst abandonment first", async () => {
    // Bailing at 3% is a worse pick than getting to 70% — the order is the whole point of the list.
    renderPanel();

    await screen.findByText("Bailed Early");
    const items = Array.from(document.querySelectorAll("li")).map(
      (li) => li.textContent ?? "",
    );
    const early = items.findIndex((t) => t.includes("Bailed Early"));
    const late = items.findIndex((t) => t.includes("Most Of It"));
    expect(early).toBeGreaterThan(-1);
    expect(early).toBeLessThan(late);
  });

  it("says nothing is wrong rather than rendering an empty list", async () => {
    getEngagement.mockResolvedValue({ ...ENGAGEMENT, people: [] });
    renderPanel(
      report({ coverage: { users_with_picks: 4, users_watched: 4 } } as never),
    );

    expect(await screen.findByText(/no row came up empty/)).toBeInTheDocument();
    expect(document.querySelectorAll("li").length).toBe(0);
  });

  it("flags titles fetched for people that nobody watched", async () => {
    renderPanel(
      report({
        requests: { sent: 34, pending: 53, watched_after_sent: 0 },
      } as never),
    );

    expect(
      await screen.findByText(/titles were fetched for people and none/),
    ).toBeInTheDocument();
    expect(screen.getByText("34")).toBeInTheDocument();
  });

  it("stays quiet about requests once one has been watched", async () => {
    renderPanel(
      report({
        requests: { sent: 34, pending: 0, watched_after_sent: 1 },
      } as never),
    );

    await screen.findByText(/got picks and watched none/);
    expect(screen.queryByText(/titles were fetched/)).toBeNull();
  });

  it("ignores a handful of requests, which prove nothing either way", async () => {
    renderPanel(
      report({
        requests: { sent: 2, pending: 0, watched_after_sent: 0 },
      } as never),
    );

    await screen.findByText(/got picks and watched none/);
    expect(screen.queryByText(/titles were fetched/)).toBeNull();
  });
});
