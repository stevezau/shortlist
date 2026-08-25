import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

  it("counts finished and not-finished as the distinct things they are", async () => {
    // Both counts survived being swapped or widened: `=== "finished"` inverted, and the not-finished
    // side loosened to `!== "finished"` so a "Still watching" pick joined the red count — the one
    // thing this component's own docstring says it must not do.
    getUserOutcomes.mockResolvedValue([
      // ASYMMETRIC counts — 3 finished against 1 not-finished. Two-and-two made an inverted
      // `=== "finished"` produce the same number, so the swap was invisible.
      pick({ tmdb_id: 1, outcome: "finished" }),
      pick({ tmdb_id: 2, outcome: "finished" }),
      pick({ tmdb_id: 5, outcome: "finished" }),
      pick({ tmdb_id: 3, outcome: "dropped", percent: 40, finished_at: null }),
      pick({ tmdb_id: 4, outcome: "watching", percent: 8, finished_at: null }),
    ]);
    renderIt();

    const summary = (await screen.findByText(/picks played/)).textContent ?? "";
    expect(summary).toMatch(/5\s*picks played/);
    expect(summary).toMatch(/3\s*finished/);
    // ONE, not two: the "Still watching" pick is not an unfinished failure.
    expect(summary).toMatch(/1\s*not finished/);
  });

  it("shows the not-finished count when there is exactly one", async () => {
    // `partial > 0` survived being tightened to `> 1`; the fixtures only used 0 and 2.
    getUserOutcomes.mockResolvedValue([
      pick({ tmdb_id: 1, outcome: "finished" }),
      pick({ tmdb_id: 2, outcome: "bounced", percent: 2, finished_at: null }),
    ]);
    renderIt();

    expect((await screen.findByText(/picks played/)).textContent).toMatch(/1\s*not finished/);
  });

  it("says nothing about a percentage it does not have", async () => {
    // Always null for a series — an episode's progress is not the show's. Dropping the null guard
    // rendered "Gave up part-way · null%".
    getUserOutcomes.mockResolvedValue([
      pick({ media_type: "show", outcome: "dropped", percent: null, finished_at: null }),
    ]);
    renderIt();

    expect(await screen.findByText("Gave up part-way")).toBeTruthy();
    expect(screen.queryByText(/null/)).toBeNull();
    expect(screen.queryByText(/·\s*%/)).toBeNull();
  });

  it("does not call an outcome it does not recognise 'Finished'", async () => {
    // The `?? LABEL.watching` fallback survived being changed to `?? LABEL.finished`, which turns an
    // unknown state into the strongest green claim the page can make.
    getUserOutcomes.mockResolvedValue([
      pick({ outcome: "something-new", percent: null, finished_at: null }),
    ]);
    renderIt();

    expect(await screen.findByText("Still watching")).toBeTruthy();
    expect(screen.queryByText("Finished")).toBeNull();
  });

  it("explains the rule behind 'gave up', which is otherwise invisible", async () => {
    // The harshest claim the page makes, decided by a rule nothing on screen states. Without it the
    // honest reaction to seeing a film you are two nights into is "the tracking is broken".
    getUserOutcomes.mockResolvedValue([
      pick({ outcome: "dropped", percent: 40, finished_at: null }),
    ]);
    renderIt();

    // Behind the same (i) the dashboard uses — a real button, not a `title`, because a hover-only
    // explanation does not exist on a phone.
    await screen.findByText("Gave up part-way");
    await userEvent.click(screen.getByRole("button", { name: /why/i }));
    expect(screen.getByText(/24 hours/).textContent).toMatch(/clock restarts/);
  });

  it("explains that a series is always 'still watching'", async () => {
    getUserOutcomes.mockResolvedValue([
      pick({ media_type: "show", outcome: "watching", percent: null, finished_at: null }),
    ]);
    renderIt();

    await screen.findByText("Still watching");
    await userEvent.click(screen.getByRole("button", { name: /why/i }));
    expect(screen.getByText(/series/)).toBeTruthy();
  });

  it("explains the empty case instead of showing an empty list", async () => {
    renderIt();

    expect(await screen.findByText(/Nothing watched yet/i)).toBeTruthy();
    expect(screen.getByText(/how far they got/i)).toBeTruthy();
  });
});
