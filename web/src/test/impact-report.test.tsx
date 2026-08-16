import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ImpactReport } from "@/components/dashboard/impact-report";
import type * as ApiModule from "@/lib/api";
import type {
  DeletedRowHistory,
  EffectivenessReport,
  ReportWindow,
} from "@/lib/types";

type ClearedRows = { cleared: number; picks: number; slugs: string[] };

const { getReport, syncWatched, getDeletedRows, clearDeletedRows } = vi.hoisted(
  () => ({
    getReport: vi.fn(),
    syncWatched: vi.fn(() => Promise.resolve({ started: true })),
    getDeletedRows: vi.fn<() => Promise<DeletedRowHistory[]>>(() =>
      Promise.resolve([]),
    ),
    clearDeletedRows: vi.fn<(slug?: string) => Promise<ClearedRows>>(() =>
      Promise.resolve({ cleared: 1, picks: 5, slugs: ["zz-claude-test"] }),
    ),
  }),
);

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getReport: (window: ReportWindow) => getReport(window),
      syncWatched: () => syncWatched(),
      getDeletedRows: () => getDeletedRows(),
      clearDeletedRows: (slug?: string) => clearDeletedRows(slug),
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
  finished: 3,
  finished_rate: 0.3,
  rate: 0.4,
  cohort_from: "2026-05-30T00:00:00Z",
  cohort_to: "2026-06-29T00:00:00Z",
  matured_days: 30,
};

const EMPTY = {
  window: "30" as ReportWindow,
  window_days: 30,
  since: "2026-06-29T00:00:00Z",
  first_pick: "2026-01-01T00:00:00Z",
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
    finished: 3,
    avg_days_to_watch: 3.5,
    avg_days_to_watch_delta: -0.8,
    landing: LANDING,
  },
  ...EMPTY,
  top_titles: [
    { tmdb_id: 1, media_type: "movie", title: "Dune: Part Two", watchers: 3 },
  ],
  trend: [{ week: "2026-28", watched: 4, finished: 3 }],
  per_user: [
    {
      username: "sarah",
      display_name: "sarah",
      slug: "sarah",
      delivered: 6,
      watched: 3,
      finished: 1,
    },
  ],
  per_row: [
    {
      slug: "picked",
      section_key: "10",
      library: "Movies",
      name: "✨ Movies Picked for You",
      deleted: false,
      delivered: 10,
      watched: 4,
      finished: 4,
    },
    {
      slug: "faves",
      section_key: "20",
      library: "TV Shows",
      name: "My Faves",
      deleted: false,
      delivered: 6,
      watched: 3,
      finished: 0,
    },
  ],
  recent: [
    {
      username: "sarah",
      display_name: "sarah",
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
    getDeletedRows.mockReset();
    getDeletedRows.mockResolvedValue([]);
    clearDeletedRows.mockClear();
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

  it("shows Finished beside Watched, never instead of it", async () => {
    // A series is credited as watched on its FIRST episode, so a lone watched count says nothing
    // about whether anyone saw a thing out. Both numbers have to be on the page for that to read.
    renderReport();

    expect(await screen.findByText("Watched")).toBeTruthy();
    // The tile, not just the word: `parentElement` is the tile body, so this asserts the label is
    // showing ITS number and not merely that a 3 exists somewhere on a page full of numbers.
    expect(screen.getByText("Finished").parentElement?.textContent).toContain(
      "3",
    );
    expect(screen.getByText("Watched").parentElement?.textContent).toContain(
      "4",
    );
  });

  it("qualifies each person and row line with what was actually finished", async () => {
    // The line that makes the difference visible: the TV row landed 3 and finished none of them,
    // while the movie row finished all 4 it landed. Before the split these rendered identically.
    // Asserted against the rendered text because the count and its word are separate elements.
    renderReport();
    await screen.findByText("Watched");

    const page = document.body.textContent ?? "";
    expect(page).toContain("4 watched · 4 finished");
    expect(page).toContain("3 watched · 0 finished");
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

    expect(await screen.findByText("Picks that get watched")).toBeTruthy();
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

    expect(await screen.findByText(/Not enough time yet/i)).toBeTruthy();
    // Two rewrites' worth of lessons, both pinned. "try a longer window" was advice that cannot
    // work — no window reaches picks that do not exist. And stating the CUTOFF ("needs picks
    // delivered before 12 Jul") read as though it wanted old picks, when what it needs is for the
    // picks it has to get older. It must say when a score arrives instead.
    expect(screen.queryByText(/longer window/i)).toBeNull();
    expect(screen.queryByText(/needs picks delivered before/i)).toBeNull();
    expect(screen.getByText(/starts showing a score around/i)).toBeTruthy();
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

  it("can delete a deleted row's history for good, and says what that costs", async () => {
    getDeletedRows.mockResolvedValue([
      {
        slug: "zz-claude-test",
        picks: 5,
        first_seen: "2026-07-01T00:00:00Z",
        last_seen: "2026-07-02T00:00:00Z",
      },
    ]);
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

    await userEvent.click(
      await screen.findByRole("button", { name: /Show 1 deleted row/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Delete their history/i }),
    );

    // The count is the point of the confirm: "5 picks" is what makes the totals dropping expected
    // rather than a bug the owner reports later.
    expect(screen.getByRole("alert")).toHaveTextContent(/5 picks in total/);
    // Clearing is never windowed, so the all-time total can exceed the lines above. Unexplained, that
    // difference reads as a bug — on the real server it was "20 picks" over a visible 5 + 5 + 5.
    expect(screen.getByRole("alert")).toHaveTextContent(
      /lines above show only the last 30 days/i,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/can.t be undone/i);
    // The warning has to name the other places these picks are counted, not just this page.
    expect(screen.getByRole("alert")).toHaveTextContent(/each person.s page/i);

    await userEvent.click(
      screen.getByRole("button", { name: /Delete the history/i }),
    );

    // No slug: the server recomputes which rows are gone, so a stale list can't purge a live row.
    expect(clearDeletedRows).toHaveBeenCalledWith(undefined);
  });

  it("deletes nothing when the confirm is dismissed", async () => {
    getDeletedRows.mockResolvedValue([
      { slug: "zz-claude-test", picks: 5, first_seen: null, last_seen: null },
    ]);
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

    await userEvent.click(
      await screen.findByRole("button", { name: /Show 1 deleted row/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Delete their history/i }),
    );
    await userEvent.click(screen.getByRole("button", { name: /Keep it/i }));

    expect(clearDeletedRows).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reads out a week's count on hover, and names the latest one before you hover anything", async () => {
    // The count used to hang off a native `title` on the BAR, so a quiet week's target was a ~3px
    // sliver at the bottom of an 80px box and most of the column hit nothing at all.
    getReport.mockResolvedValue({
      ...REPORT,
      trend: [
        { week: "2026-27", watched: 9 },
        { week: "2026-28", watched: 2 },
        { week: "2026-32", watched: 5 },
      ],
    });
    renderReport();

    // Nothing hovered: the readout names the LATEST week, so a touch screen — which has no hover to
    // give — still gets a number rather than a blank line.
    const readout = await screen.findByText(/watched in the week of/i);
    expect(readout).toHaveTextContent(/5/);
    expect(readout).toHaveTextContent(/latest/i);

    // The whole column is the target, including the empty space above a two-watch bar.
    const columns = screen.getAllByTestId("trend-week");
    expect(columns).toHaveLength(3);
    await userEvent.hover(columns[1] as HTMLElement);
    expect(readout).toHaveTextContent(/2/);
    expect(readout).toHaveTextContent(/Jul/);
    expect(readout).not.toHaveTextContent(/latest/i);
  });

  it("shows a count and a way to see active watchers past the first 10, instead of silently hiding them", async () => {
    // Issue 7.3: `active.slice(0, 10)` used to just drop everyone past the tenth, with no count and
    // no way to see them — unlike the IDLE half of this same list, which already got a disclosure.
    const many = Array.from({ length: 12 }, (_, i) => ({
      username: `user${i}`,
      slug: `user${i}`,
      delivered: 5,
      watched: 12 - i,
    }));
    getReport.mockResolvedValue({ ...REPORT, per_user: many });
    renderReport();

    await screen.findByText("user0");
    expect(screen.getByText("user9")).toBeInTheDocument();
    // The 11th and 12th are not silently dropped...
    expect(screen.queryByText("user10")).toBeNull();
    expect(screen.queryByText("user11")).toBeNull();
    // ...they're named and offered, the same way idle people already were. The label is POSITIONAL
    // ("show 2 more"), not a second claim — "2 more people watched something" reused the section's
    // own verb and read as a separate finding rather than the tail of the list above it.
    const toggle = screen.getByRole("button", {
      name: /Show 2 more people/i,
    });
    await userEvent.click(toggle);
    expect(screen.getByText("user10")).toBeInTheDocument();
    expect(screen.getByText("user11")).toBeInTheDocument();
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

    expect(
      await screen.findByText(/Nothing has reached anyone's rows yet/i),
    ).toBeTruthy();
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
        /Nothing reached a row, and nothing was watched, in the last 30 days/i,
      ),
    ).toBeTruthy();
  });
});

describe("WatchSyncLine — 'Sync now'", () => {
  beforeEach(() => {
    getReport.mockReset();
    getReport.mockResolvedValue(REPORT);
    getDeletedRows.mockReset();
    getDeletedRows.mockResolvedValue([]);
    syncWatched.mockReset();
  });

  it("re-enables after a successful sync instead of sticking on 'Syncing…' forever", async () => {
    // Issue 7.4: `disabled={isPending || isSuccess}` with the label keyed on `isSuccess` meant a
    // SUCCESSFUL sync permanently disabled the button and permanently read "Syncing…" until the page
    // was reloaded — the opposite of what a finished sync should look like.
    let resolveSync!: (v: { started: boolean }) => void;
    syncWatched.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSync = resolve;
        }),
    );
    renderReport();
    await screen.findByText("Watched");

    await userEvent.click(screen.getByRole("button", { name: /Sync now/i }));
    expect(
      await screen.findByRole("button", { name: /Syncing…/i }),
    ).toBeDisabled();

    resolveSync({ started: true });

    const again = await screen.findByRole("button", { name: /Sync now/i });
    expect(again).toBeEnabled();
  });

  it("shows an error and re-enables the button when the sync can't be started", async () => {
    // The old version had no isError branch at all — a failed POST looked identical to nothing
    // having happened.
    syncWatched.mockRejectedValueOnce(new Error("boom"));
    renderReport();
    await screen.findByText("Watched");

    await userEvent.click(screen.getByRole("button", { name: /Sync now/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Couldn.t start the sync/i,
    );
    expect(screen.getByRole("button", { name: /Try again/i })).toBeEnabled();
  });
});

describe("the window selector on a young install", () => {
  beforeEach(() => {
    getReport.mockReset();
    getDeletedRows.mockReset();
    getDeletedRows.mockResolvedValue([]);
  });

  it("explains why the numbers don't move when every window covers all the data", async () => {
    // 3 runs' worth of picks a few days old: 7 / 30 / 90 / all return identical figures, so pressing
    // a button changes nothing on screen and reads as a broken control. This line is the difference
    // between "it's broken" and "there isn't older data yet".
    getReport.mockResolvedValue({
      ...REPORT,
      since: "2026-06-29T00:00:00Z",
      first_pick: "2026-07-28T00:00:00Z", // newer than the window start
    });
    renderReport();

    expect(
      await screen.findByText(/only been recording since/i),
    ).toBeInTheDocument();
  });

  it("says nothing once there IS history older than the window", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      since: "2026-06-29T00:00:00Z",
      first_pick: "2026-01-01T00:00:00Z", // older than the window start
    });
    renderReport();

    await screen.findByText("Watched");
    expect(screen.queryByText(/only been recording since/i)).toBeNull();
  });

  it("says nothing on the all-time window, which cannot be narrower than the data", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      window: "all" as ReportWindow,
      since: null,
      first_pick: "2026-07-28T00:00:00Z",
    });
    renderReport();

    await screen.findByText("Watched");
    expect(screen.queryByText(/only been recording since/i)).toBeNull();
  });
});

describe("ImpactReport — recently watched", () => {
  beforeEach(() => {
    getReport.mockReset();
    syncWatched.mockClear();
    getDeletedRows.mockResolvedValue([]);
  });

  it("caps the list and offers the rest, instead of dropping them silently", async () => {
    // The server sends up to 20 (`report_service._recent_watches`) and the page sliced to 12, so
    // eight were dropped with no count and no disclosure — a bounded feed reading as a full history.
    const many = Array.from({ length: 20 }, (_, i) => ({
      username: `user${i}`,
      display_name: `User ${i}`,
      title: `Title ${i}`,
      media_type: "movie" as const,
      row: "✨ Picked for You",
      library: "Movies",
      seed_title: "Arrival",
      watched_at: new Date(Date.now() - i * 3600_000).toISOString(),
    }));
    getReport.mockResolvedValue({ ...REPORT, recent: many });
    renderReport();

    expect(await screen.findByText("Title 0")).toBeInTheDocument();
    expect(screen.getByText("Title 11")).toBeInTheDocument();
    // The 13th onwards are folded, not discarded...
    expect(screen.queryByText("Title 12")).toBeNull();
    expect(screen.queryByText("Title 19")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /Show 8 more/i }));

    expect(screen.getByText("Title 12")).toBeInTheDocument();
    expect(screen.getByText("Title 19")).toBeInTheDocument();
  });

  it("says how many watches the feed holds, so it doesn't read as everything", async () => {
    getReport.mockResolvedValue({
      ...REPORT,
      recent: [
        {
          username: "sarah",
          display_name: "Sarah",
          title: "Dune",
          media_type: "movie" as const,
          row: "✨ Picked for You",
          library: "Movies",
          seed_title: "Arrival",
          watched_at: new Date().toISOString(),
        },
      ],
    });
    renderReport();

    expect(await screen.findByText(/newest watch/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show .* more/i })).toBeNull();
  });
});
