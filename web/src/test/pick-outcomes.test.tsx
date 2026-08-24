import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PickOutcomes } from "@/components/user-detail/pick-outcomes";
import type { UserPickOutcome } from "@/lib/types";

const getUserOutcomes = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { getUserOutcomes: (...a: unknown[]) => getUserOutcomes(...a) },
}));

function pick(over: Partial<UserPickOutcome> = {}): UserPickOutcome {
  return {
    tmdb_id: 1,
    media_type: "movie",
    title: "Dune: Part Two",
    row: "✨ Movies Picked for You",
    outcome: "finished",
    percent: null,
    watched_at: "2026-08-22T10:00:00+00:00",
    finished_at: "2026-08-22T12:00:00+00:00",
    ...over,
  };
}

function renderIt() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <PickOutcomes userId={42} />
    </QueryClientProvider>,
  );
}

describe("PickOutcomes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getUserOutcomes.mockResolvedValue([]);
  });

  it("separates what they finished from what they didn't", async () => {
    // The whole point of the section: "watched" alone cannot tell these apart, and they say opposite
    // things about the recommendation.
    getUserOutcomes.mockResolvedValue([
      pick({ tmdb_id: 1, title: "Seen It Out", outcome: "finished" }),
      pick({ tmdb_id: 2, title: "Gave Up", outcome: "dropped", percent: 40, finished_at: null }),
      pick({ tmdb_id: 3, title: "Barely Started", outcome: "bounced", percent: 2, finished_at: null }),
    ]);
    renderIt();

    expect(await screen.findByText("Finished")).toBeTruthy();
    expect(screen.getByText("Gave up part-way")).toBeTruthy();
    expect(screen.getByText("Barely started")).toBeTruthy();
    // The summary line is split across elements, so read the paragraph's whole text.
    expect(screen.getByText(/picks played/).textContent).toMatch(/3\s*picks played/);
  });

  it("says how far they got, but not on something they finished", async () => {
    // A percentage on a completion is noise — it is 100 by definition, and printing it invites the
    // reader to wonder whether 97 means they missed the end.
    getUserOutcomes.mockResolvedValue([
      pick({ tmdb_id: 1, title: "Gave Up", outcome: "dropped", percent: 40, finished_at: null }),
      pick({ tmdb_id: 2, title: "Seen It Out", outcome: "finished", percent: 100 }),
    ]);
    renderIt();

    expect(await screen.findByText(/· 40%/)).toBeTruthy();
    expect(screen.queryByText(/· 100%/)).toBeNull();
  });

  it("calls an unsettled watch 'still watching', not a failure", async () => {
    // The server refuses to call a watch abandoned while it is open or less than a day old, and this
    // must render that honestly rather than lumping it in with the give-ups.
    getUserOutcomes.mockResolvedValue([
      pick({ outcome: "watching", percent: 8, finished_at: null }),
    ]);
    renderIt();

    expect(await screen.findByText("Still watching")).toBeTruthy();
    expect(screen.queryByText(/gave up/i)).toBeNull();
  });

  it("does not print a not-finished count when everything landed", async () => {
    // A line that says "0 not finished" every day teaches you to stop reading the number on the day
    // it is not zero.
    getUserOutcomes.mockResolvedValue([pick()]);
    renderIt();

    expect((await screen.findByText(/picks played/)).textContent).toMatch(
      /1\s*picks played/,
    );
    expect(screen.queryByText(/not finished/)).toBeNull();
  });

  it("explains the empty case instead of showing an empty list", async () => {
    renderIt();

    expect(await screen.findByText(/Nothing watched yet/i)).toBeTruthy();
    expect(screen.getByText(/how far they got/i)).toBeTruthy();
  });
});
