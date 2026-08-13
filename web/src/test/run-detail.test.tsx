import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RunDetailPage } from "@/pages/run-detail";
import type { RunLogEntry, RunDetail } from "@/lib/types";

const { getRun, getUsers, getRunLog } = vi.hoisted(() => ({
  getRun: vi.fn(),
  getUsers: vi.fn(),
  getRunLog: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getRun: (id: number) => getRun(id),
      getUsers: () => getUsers(),
      getRunLog: (id: number) => getRunLog(id),
    },
  };
});

// useSSE opens an EventSource; jsdom has none, so stub one. Captures registered listeners (rather
// than the setup.ts no-op default) so a test can simulate a server event and assert on the page's
// reaction — used below to prove run-detail only refetches on ITS OWN run's SSE events (issue 7.6).
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners: Record<string, ((event: MessageEvent<string>) => void)[]> = {};
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.instances.push(this);
  }
  addEventListener(
    type: string,
    handler: (event: MessageEvent<string>) => void,
  ) {
    (this.listeners[type] ??= []).push(handler);
  }
  close() {}
  emit(type: string, data: unknown) {
    for (const handler of this.listeners[type] ?? []) {
      handler({ data: JSON.stringify(data) } as MessageEvent<string>);
    }
  }
}
vi.stubGlobal("EventSource", FakeEventSource);

function run(breakdown: RunDetail["users"][number]["breakdown"]): RunDetail {
  return {
    id: 2,
    shared_rows: [],
    trigger: "manual",
    status: "ok",
    started_at: "2026-07-15T04:18:00Z",
    finished_at: "2026-07-15T04:24:00Z",
    dry_run: false,
    stats: { users_ok: 1, users_error: 0, titles_requested: 0 },
    error: null,
    promotion_blockers: [],
    users: [
      {
        username: "MooHouse",
        slug: "moohouse",
        status: "ok",
        rows_considered: {},
        display_name: "MooHouse",
        error: null,
        reason: null,
        exa_searches: 0,
        has_trace: false,
        llm_tokens_by_step: {},
        duration_ms: 335000,
        llm_tokens: 5030,
        diff: {},
        picks: [],
        breakdown,
      },
    ],
  } as RunDetail;
}

/** `query` deep-links a tab (`?tab=users`) — the same URL a person's Runs tab links to, so these
 *  tests exercise the real entry point rather than a state the UI can't reach. */
function renderDetail(query = "?tab=users") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/runs/2${query}`]}>
        <Routes>
          <Route path="/runs/:id" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RunDetailPage — grouped by library", () => {
  beforeEach(() => {
    getRun.mockReset();
    getUsers.mockReset();
    getRunLog.mockReset();
    getUsers.mockResolvedValue([]);
    getRunLog.mockResolvedValue([]);
  });

  it("shows each library as its own group with its own picks, not one merged list", async () => {
    getRun.mockResolvedValue(
      run([
        {
          row_slug: "picked",
          row_title: "✨ Picked for You",
          library_key: "1",
          library_title: "Movies",
          added: ["Saving Private Ryan"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Saving Private Ryan",
              reason: "war epic",
              seed_title: "Pressure",
              sources: ["tmdb_similar"],
              affinity: 0.42,
            },
          ],
        },
        {
          row_slug: "picked",
          row_title: "✨ Picked for You",
          library_key: "2",
          library_title: "TV Shows",
          added: ["Deadliest Catch"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Deadliest Catch",
              reason: "survival series",
              seed_title: "Gold Rush",
              sources: [],
              affinity: null,
            },
          ],
        },
      ]),
    );

    renderDetail();

    // A row spanning two libraries shows them as TABS — the selected library's picks only, so the
    // page stays short. Movies is selected first; TV Shows appears when you click it.
    expect(
      await screen.findByRole("button", { name: /Movies/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /TV Shows/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/war epic/)).toBeInTheDocument();
    // This page is where "why did it pick that?" gets asked, and it has its OWN pick renderer
    // rather than using PickList — so the provenance line has to be asserted here separately.
    expect(
      screen.getByText(/suggested by TMDB · loosely related/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/survival series/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /TV Shows/ }));
    expect(screen.getByText(/survival series/)).toBeInTheDocument();
    expect(screen.queryByText(/war epic/)).not.toBeInTheDocument();
  });

  it("shows the finished-run stats as at-a-glance tiles", async () => {
    const r = run([]);
    r.stats = {
      users_ok: 2,
      users_error: 1,
      titles_added: 5,
      titles_removed: 3,
      titles_requested: 4,
      llm_tokens: 377428,
      llm_tokens_by_step: { curate: 251295, llm_web: 126133 },
      exa_searches: 46,
    };
    getRun.mockResolvedValue(r);

    renderDetail("");

    // Duration is computed from started_at → finished_at (04:18 → 04:24 = 6 minutes).
    expect(await screen.findByText("Duration")).toBeInTheDocument();
    expect(screen.getByText("6m 0s")).toBeInTheDocument();
    expect(screen.getByText("People")).toBeInTheDocument();
    expect(screen.getByText("1 failed")).toBeInTheDocument(); // 1 of the 3 users errored
    expect(screen.getByText("+5 / −3")).toBeInTheDocument();
    expect(screen.getByText("377,428")).toBeInTheDocument();
    expect(screen.getByText(/final picks 251,295/)).toBeInTheDocument();
    expect(screen.getByText("Web searches")).toBeInTheDocument();
    expect(screen.getByText("46")).toBeInTheDocument();
  });

  it("shows cache hits so a fully-cached run doesn't look like the source did nothing", async () => {
    // The bug that misread SFLIX run 1: a warm shared cache means almost nothing is billed, so the
    // tile read a bare "1" and looked broken. It must show what the cache served, not just the bill.
    const r = run([]);
    r.stats = {
      users_ok: 47,
      users_error: 0,
      titles_requested: 0,
      llm_tokens: 691422,
      exa_searches: 1,
      exa_cache_hits: 793,
    };
    getRun.mockResolvedValue(r);

    renderDetail("");

    expect(await screen.findByText("Web searches")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument(); // actually searched
    expect(screen.getByText(/793 from cache/)).toBeInTheDocument();
  });

  it("hides the AI/Exa tiles and reads 'all succeeded' on a clean AI-free run", async () => {
    const r = run([]);
    r.stats = {
      users_ok: 3,
      users_error: 0,
      titles_added: 0,
      titles_removed: 0,
      titles_requested: 0,
      llm_tokens: 0,
      exa_searches: 0,
    };
    getRun.mockResolvedValue(r);

    renderDetail("");

    expect(await screen.findByText("all succeeded")).toBeInTheDocument();
    // No AI this run → those tiles don't render at all (0-value tiles would be noise).
    expect(screen.queryByText("AI tokens")).not.toBeInTheDocument();
    expect(screen.queryByText("Web searches")).not.toBeInTheDocument();
  });

  it("falls back to a plain AI-tokens hint when there's no by-step breakdown", async () => {
    const r = run([]);
    r.stats = { users_ok: 1, users_error: 0, llm_tokens: 9000 }; // legacy run: total but no split
    getRun.mockResolvedValue(r);

    renderDetail("");

    expect(await screen.findByText("9,000")).toBeInTheDocument();
    expect(screen.getByText("curate + AI sources")).toBeInTheDocument();
  });

  it("renders the row title for the SELECTED library, not the first one", async () => {
    // A `{library_name}` title renders differently per library. The header must follow the tab —
    // it used to stay stuck on the first library's title even after switching tabs.
    getRun.mockResolvedValue(
      run([
        {
          row_slug: "picked",
          row_title: "Movies Picked for You",
          library_key: "1",
          library_title: "Movies",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Heat",
              reason: "crime",
              seed_title: "",
              sources: [],
              affinity: null,
            },
          ],
        },
        {
          row_slug: "picked",
          row_title: "TV Shows Picked for You",
          library_key: "2",
          library_title: "TV Shows",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Fargo",
              reason: "crime",
              seed_title: "",
              sources: [],
              affinity: null,
            },
          ],
        },
      ]),
    );

    renderDetail();

    expect(
      await screen.findByText("Movies Picked for You"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("TV Shows Picked for You"),
    ).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /TV Shows/ }));
    expect(screen.getByText("TV Shows Picked for You")).toBeInTheDocument();
    expect(screen.queryByText("Movies Picked for You")).not.toBeInTheDocument();
  });

  it("groups entries by row, so two different rows render as separate groups", async () => {
    getRun.mockResolvedValue(
      run([
        {
          row_slug: "picked",
          row_title: "✨ Picked for You",
          library_key: "1",
          library_title: "Movies",
          added: ["A"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "A",
              reason: "a",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
        {
          row_slug: "hidden_gems",
          row_title: "💎 Hidden Gems",
          library_key: "1",
          library_title: "Movies",
          added: ["B"],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "B",
              reason: "b",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
      ]),
    );

    renderDetail();

    // Each row shows as its own group header — not collapsed into one.
    expect(await screen.findByText("✨ Picked for You")).toBeInTheDocument();
    expect(screen.getByText("💎 Hidden Gems")).toBeInTheDocument();
  });

  it("shows the run's activity log, seeded from the server buffer", async () => {
    getRun.mockResolvedValue(run([]));
    getRunLog.mockResolvedValue([
      {
        ts: "2026-07-15T04:18:05Z",
        run_id: 2,
        user: "moohouse",
        stage: "curating",
        counts: { candidates: 120 },
      },
    ]);

    renderDetail("?tab=log");

    // The stage renders with its human label + the count detail.
    expect(await screen.findByText(/curating with AI/)).toBeInTheDocument();
    expect(screen.getByText(/120 candidates/)).toBeInTheDocument();
  });

  it("filters the log down to Plex writes, and says what it hid", async () => {
    getRun.mockResolvedValue(run([]));
    getRunLog.mockResolvedValue([
      {
        seq: 0,
        ts: "2026-07-15T04:18:05Z",
        run_id: 2,
        user: "moohouse",
        stage: "curating",
        counts: {},
      },
      {
        seq: 1,
        ts: "2026-07-15T04:18:06Z",
        run_id: 2,
        user: "Shortlist",
        stage: "filters",
        counts: { done: 2, total: 5 },
      },
    ]);

    renderDetail("?tab=log");
    await screen.findByText(/curating with AI/);

    await userEvent.click(screen.getByRole("button", { name: "Plex writes" }));

    // Scoped to the log box: the phase timeline above it is always on now, and it names the same
    // phases — so an unscoped query matches both and proves nothing about the filter.
    const log = within(screen.getByRole("log", { name: /Run activity log/i }));
    expect(log.getByText(/merging share filters/)).toBeInTheDocument();
    expect(log.queryByText(/curating with AI/)).toBeNull();
    expect(screen.getByText("1 of 2 lines")).toBeInTheDocument();
  });

  it("explains an empty log rather than implying the feature is broken", async () => {
    // A run from before activity logs were stored has an empty tab through no fault of its own.
    getRun.mockResolvedValue(run([]));
    getRunLog.mockResolvedValue([]);

    renderDetail("?tab=log");

    expect(
      await screen.findByText(/No activity recorded for this run/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Runs from before this was added have no stored log/i),
    ).toBeInTheDocument();
  });

  it("shows an error with retry when the log fetch fails, instead of reading as 'no log'", async () => {
    // Issue 7.7: a failed GET /runs/:id/log rendered exactly the same "No activity recorded" empty
    // state as a run that genuinely has no log — indistinguishable, and with no way to retry.
    getRun.mockResolvedValue(run([]));
    getRunLog.mockRejectedValue(new Error("network error"));

    renderDetail("?tab=log");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No activity recorded for this run/i)).toBeNull();
    expect(
      screen.getByRole("button", { name: /Try again/i }),
    ).toBeInTheDocument();
  });

  it("names the phase a still-running run is actually in", async () => {
    // The complaint this whole tab exists for: every person shows "done" and the run still says
    // running, with nothing anywhere saying what it is doing.
    getRun.mockResolvedValue({
      ...run([]),
      finished_at: null,
      status: "running",
    });
    getRunLog.mockResolvedValue([
      {
        seq: 0,
        ts: "2026-07-15T04:18:05Z",
        run_id: 2,
        user: "Shortlist",
        stage: "converging",
        counts: {},
      },
    ]);

    renderDetail("");

    expect(await screen.findByText(/Finishing up/)).toBeInTheDocument();
    // Named in the header AND in Overview's latest-activity panel — both answer "what is it
    // doing right now", so both carry it.
    expect(
      screen.getAllByText("checking for stranded rows").length,
    ).toBeGreaterThan(0);
  });

  it("falls back to the flat pick list for legacy runs with no breakdown", async () => {
    // A legacy run has no per-library breakdown, but its picks still render as a plain list.
    getRun.mockResolvedValue({
      ...run([]),
      users: [
        {
          username: "MooHouse",
          display_name: "MooHouse",
          slug: "moohouse",
          rows_considered: {},
          status: "ok",
          error: null,
          reason: null,
          duration_ms: 1000,
          llm_tokens: 0,
          llm_tokens_by_step: {},
          exa_searches: 0,
          has_trace: false,
          diff: {},
          picks: [
            {
              rank: 1,
              title: "Old Title",
              reason: "legacy",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
          breakdown: [],
        },
      ],
    } as RunDetail);

    renderDetail();

    expect(await screen.findByText("Old Title")).toBeInTheDocument();
  });

  it("shows a legend and explains rotated-out titles instead of a bare 'removed'", async () => {
    getRun.mockResolvedValue(
      run([
        {
          row_slug: "picked",
          row_title: "✨ Picked for You",
          library_key: "1",
          library_title: "Movies",
          added: ["Fresh One"],
          removed: ["Old One", "Older One"],
          kept: [],
          deleted: [],
          created: false,
          picks: [
            {
              rank: 1,
              title: "Fresh One",
              reason: "new pick",
              seed_title: "X",
              sources: [],
              affinity: null,
            },
          ],
        },
      ]),
    );

    renderDetail();
    await screen.findByText("Fresh One");

    // The key explains every visual cue the results use, so nothing needs a hover to decode.
    expect(screen.getByText(/What changed/i)).toBeInTheDocument();
    expect(screen.getByText("New this run")).toBeInTheDocument();
    expect(screen.getByText("Kept from last run")).toBeInTheDocument();
    expect(screen.getByText("Rotated out for variety")).toBeInTheDocument();
    expect(screen.getByText("Top picks")).toBeInTheDocument();

    // "removed" now reads as rotation with the reason, not a bare scary count.
    expect(screen.getByText(/2 rotated out/)).toBeInTheDocument();
    expect(
      screen.getByText(/made room for the new picks above/i),
    ).toBeInTheDocument();
  });
});

function skippedUser(username: string, i: number) {
  return {
    username,
    slug: username,
    status: "skipped",
    error: null,
    reason: "There are no per-person rows to build.",
    duration_ms: 0,
    llm_tokens: 0,
    diff: {},
    picks: [],
    breakdown: [],
    id: i,
  };
}

describe("RunDetail — a skipped person is not a success", () => {
  it("groups skipped apart from succeeded when a run has all three outcomes", async () => {
    // The same "count says success, row says skipped" bug, one level down: grouping on
    // `error === null` put skipped people under the "Succeeded" heading.
    const r = run([]);
    r.stats = {
      users_ok: 1,
      users_error: 1,
      users_skipped: 1,
      titles_requested: 0,
    };
    r.users = [
      { ...skippedUser("sarah", 1), status: "ok", reason: null },
      {
        ...skippedUser("mike", 2),
        error: "boom",
        status: "error",
        reason: null,
      },
      skippedUser("canary", 3),
    ] as unknown as RunDetail["users"];
    getRun.mockResolvedValue(r);

    renderDetail();

    expect(await screen.findByText(/Succeeded · 1/i)).toBeInTheDocument();
    expect(screen.getByText(/Skipped · 1/i)).toBeInTheDocument();
    expect(screen.getByText(/Failed · 1/i)).toBeInTheDocument();
  });

  it("does not claim 'all succeeded' when everyone was skipped", async () => {
    // The contradiction this fixes: three rows badged "Skipped" under a header reading
    // "3 · all succeeded", because the stats only ever counted error vs non-error.
    const r = run([]);
    r.stats = {
      users_ok: 0,
      users_error: 0,
      users_skipped: 3,
      titles_requested: 0,
    };
    r.users = ["sarah", "mike", "canary"].map((u, i) =>
      skippedUser(u, i),
    ) as unknown as RunDetail["users"];
    getRun.mockResolvedValue(r);

    renderDetail("");

    // The Overview tile's hint — one of the two surfaces that used to say "succeeded".
    expect(
      await screen.findByText(/3 skipped, built nothing/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("all succeeded")).toBeNull();
  });

  it("explains a skip on the person's own panel too", async () => {
    const r = run([]);
    r.stats = {
      users_ok: 0,
      users_error: 0,
      users_skipped: 3,
      titles_requested: 0,
    };
    r.users = ["sarah", "mike", "canary"].map((u, i) =>
      skippedUser(u, i),
    ) as unknown as RunDetail["users"];
    getRun.mockResolvedValue(r);

    renderDetail("?tab=users");

    expect(
      await screen.findByText(/3 skipped — nothing was built/i),
    ).toBeInTheDocument();
    // …and the person panel explains WHY rather than leaving them on "Working on this person…".
    expect(
      await screen.findByText(/no per-person rows to build/i),
    ).toBeInTheDocument();
  });

  it("does not report 'no changes' for a cold-start person whose row was skipped", async () => {
    // A cold-skipped person is status `cold_start` with no picks and no breakdown — which read as
    // "No changes — this person's rows were already up to date" on the one screen whose job is
    // "what changed at 03:31", on a run that had just DELETED their collection.
    const r = run([]);
    r.stats = {
      users_ok: 1,
      users_error: 0,
      users_skipped: 0,
      titles_requested: 0,
    };
    r.users = [
      {
        ...skippedUser("canary", 1),
        status: "cold_start",
        reason:
          "Not enough watch history yet — 0 of 10 titles. The row due in this run is set to build nothing until then, so 1 already on Plex was removed.",
      },
    ] as unknown as RunDetail["users"];
    getRun.mockResolvedValue(r);

    renderDetail("?tab=users");

    expect(
      await screen.findByText(/not enough watch history yet/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/already up to date/i)).toBeNull();
  });
});

describe("RunDetail — shows the display name, not the bare username", () => {
  it("renders display_name (Tautulli/nickname) instead of the Plex login when present", async () => {
    // The runs view showed the raw Plex username even after a friendly name was synced — because
    // the endpoint only emitted `username`. It now carries display_name (nickname → Tautulli →
    // username); the row must render that, keeping `username` only for the avatar + search.
    const r = run([]);
    r.stats = { users_ok: 1, users_error: 0, titles_requested: 0 };
    r.users = [
      {
        ...skippedUser("moohouse", 1),
        status: "ok",
        reason: null,
        display_name: "Joe - Richard's Mate",
      },
    ] as unknown as RunDetail["users"];
    getRun.mockResolvedValue(r);

    renderDetail();

    // The name now appears in both the user sidebar row and the panel header (the sidebar shows
    // even for a single user), so assert it's present rather than unique — the point is the bare
    // Plex login "moohouse" never renders as text (it's kept only for the avatar + search).
    expect(
      (await screen.findAllByText("Joe - Richard's Mate")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("moohouse")).toBeNull();
  });
});

describe("RunDetail — a failed run says why", () => {
  it("explains a refused share filter instead of showing a bare 'Failed'", async () => {
    // The reason was always recorded, but lived only in stats.error which nothing rendered — so a
    // beta user with this exact failure had to read container logs to find out (issue #1).
    const r = run([]);
    r.status = "error";
    r.error = "privacy sync for LisaPlex1234: RuntimeError: plex.tv rejected…";
    r.promotion_blockers = [
      "LisaPlex1234 (plex account 12345): plex.tv rejected the share-filter update for account 12345: HTTP 400",
    ];
    getRun.mockResolvedValue(r);

    renderDetail("");

    expect(
      await screen.findByText(/Plex wouldn’t accept a share filter/i),
    ).toBeInTheDocument();
    // The operator needs the account and the status, not a euphemism.
    expect(screen.getByText(/HTTP 400/)).toBeInTheDocument();
    expect(screen.getByText(/plex account 12345/)).toBeInTheDocument();
    // …and the People tile must not call it a clean sweep.
    expect(screen.queryByText("all succeeded")).toBeNull();
    expect(screen.getByText("built, but not promoted")).toBeInTheDocument();
  });

  it("stays quiet on a clean run", async () => {
    getRun.mockResolvedValue(run([]));
    renderDetail("");
    // The tiles only render once a run has finished — wait for one, then assert no alarm.
    expect(await screen.findByText("all succeeded")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

function failedUser(username: string, error: string) {
  return {
    username,
    slug: username,
    status: "error",
    error,
    reason: null,
    duration_ms: 0,
    llm_tokens: 0,
    diff: {},
    picks: [],
    breakdown: [],
  };
}

describe("RunDetail — 'N people failed with the same problem' (issue 7.1)", () => {
  beforeEach(() => {
    getRun.mockReset();
    getUsers.mockReset();
    getRunLog.mockReset();
    getUsers.mockResolvedValue([]);
    getRunLog.mockResolvedValue([]);
  });

  it("claims commonality for two people who hit the SAME recognised error class", async () => {
    const r = run([]);
    r.stats = { users_ok: 0, users_error: 3, titles_requested: 0 };
    r.users = [
      failedUser("sarah", "HTTP 500 while updating collection 12"),
      failedUser("mike", "HTTP 500 while updating collection 99"),
      failedUser("amy", "connection refused to 10.0.0.5:32400"),
    ] as unknown as RunDetail["users"];
    getRun.mockResolvedValue(r);

    // Explicitly the People tab: `rows` is the default now, and this banner is the People tab's.
    renderDetail("?tab=users");

    // Scoped to the banner itself: the selected (first) failed person's own panel repeats the same
    // friendly sentence below it, so an unscoped query matches both and proves nothing about the
    // banner specifically.
    const commonLine = await screen.findByText(
      /people failed with the same problem/i,
    );
    const banner = commonLine.closest('[role="alert"]') as HTMLElement;
    expect(banner).toHaveTextContent(/2 people failed with the same problem/i);
    expect(banner).toHaveTextContent(/server error \(500\)/i);
  });

  it("does NOT claim commonality for two people with different unrecognised errors", async () => {
    // The bug: errorBucket used to be a pure alias of friendlyError, which returns one generic
    // sentence for anything unrecognised — so these two unrelated failures bucketed together and
    // the page asserted they were "the same problem", which was false.
    const r = run([]);
    r.stats = { users_ok: 0, users_error: 2, titles_requested: 0 };
    r.users = [
      failedUser("sarah", "KeyError: 'ratingKey'"),
      failedUser("mike", "AttributeError: NoneType has no attribute 'guid'"),
    ] as unknown as RunDetail["users"];
    getRun.mockResolvedValue(r);

    renderDetail("");

    await screen.findAllByText(/failed/i);
    expect(screen.queryByText(/failed with the same problem/i)).toBeNull();
  });
});

describe("RunDetail — where the phase breakdown lives", () => {
  beforeEach(() => {
    getRun.mockReset();
    getUsers.mockReset();
    getRunLog.mockReset();
    getUsers.mockResolvedValue([]);
    // Real TAIL_STAGES with a gap between them — the breakdown renders nothing without them.
    getRunLog.mockResolvedValue([
      {
        seq: 1,
        ts: "2026-07-30T03:30:00Z",
        level: "info",
        message: "run · Shortlist · users_done",
        stage: "users_done",
      },
      {
        seq: 2,
        ts: "2026-07-30T03:30:30Z",
        level: "info",
        message: "run · Shortlist · ordering",
        stage: "ordering",
      },
      {
        seq: 3,
        ts: "2026-07-30T03:32:00Z",
        level: "info",
        message: "run · Shortlist · finished",
        stage: "finished",
      },
    ] as unknown as RunLogEntry[]);
  });

  it("keeps the headline tiles on the People tab but not the phase breakdown", async () => {
    // The tiles are the run's summary. "Where the time went" is read off the log's own timings and
    // answers a question you're asking while reading the log — not while scanning people.
    getRun.mockResolvedValue(run([]));

    renderDetail("?tab=users");

    await screen.findByRole("button", { name: /Log/i });
    expect(screen.queryByText(/Where the time went/i)).toBeNull();
  });

  it("shows the phase breakdown on the Log tab, collapsed behind its headline", async () => {
    getRun.mockResolvedValue(run([]));

    renderDetail("?tab=log");

    // The number says what it measures — the tail — rather than competing with the run's own
    // Duration tile for "the" total. 03:30:00 to 03:32:00 is two minutes.
    const headline = await screen.findByText(/after the last person finished/i);
    // Scoped to the card: the log panel below renders the same stage labels for its own lines, so an
    // unscoped query matches those and proves nothing about this card.
    const card = headline.closest("div[class*='rounded']") as HTMLElement;
    // users_done 03:30:00 -> finished 03:32:00. The number is the whole point of the collapsed state,
    // so assert the value, not just that some text is present.
    expect(within(card).getByText("2m")).toBeInTheDocument();
    expect(within(card).queryByText(/ordering rows/i)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /time went/i }));
    expect(within(card).getByText(/ordering rows/i)).toBeInTheDocument();
  });

  it("never shows a marker as a phase — 'all users done' has no duration", async () => {
    // It is a MOMENT, not work: its "duration" is the gap to the next stage, which is ~0 by
    // definition, so it rendered an empty bar labelled "0s" that looked like a bug.
    getRun.mockResolvedValue(run([]));

    renderDetail("?tab=log");

    const headline = await screen.findByText(/after the last person finished/i);
    const card = headline.closest("div[class*='rounded']") as HTMLElement;
    await userEvent.click(screen.getByRole("button", { name: /time went/i }));

    expect(within(card).queryByText(/all users done/i)).toBeNull();
    expect(within(card).queryByText(/run finished/i)).toBeNull();
  });
});

describe("RunDetail — SSE stage events only refetch THIS run (issue 7.6)", () => {
  beforeEach(() => {
    getRun.mockReset();
    getUsers.mockReset();
    getRunLog.mockReset();
    getUsers.mockResolvedValue([]);
    getRunLog.mockResolvedValue([]);
    FakeEventSource.instances = [];
  });

  it("does not refetch when the stage event belongs to a different run", async () => {
    // Sitting on finished run #2 while another run (#40) streams its own stage events used to
    // refetch #2 on every single one of them — `onRunUserStage` had no run_id guard at all, unlike
    // `onRunFinished` right below it, which correctly checked `event.run_id === runId`.
    getRun.mockResolvedValue(run([]));
    renderDetail("");
    await screen.findByText("all succeeded");

    const callsBefore = getRun.mock.calls.length;
    const source = FakeEventSource.instances.at(-1);
    source?.emit("run.user.stage", {
      user: "someoneelse",
      stage: "curating",
      counts: {},
      run_id: 999, // this page is showing run #2
    });

    // Give any (wrongly) queued refetch a chance to fire, then assert it didn't.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(getRun.mock.calls.length).toBe(callsBefore);
  });

  it("does refetch on a stage event for this run", async () => {
    getRun.mockResolvedValue(run([]));
    renderDetail("");
    await screen.findByText("all succeeded");

    const callsBefore = getRun.mock.calls.length;
    const source = FakeEventSource.instances.at(-1);
    source?.emit("run.user.stage", {
      user: "moohouse",
      stage: "curating",
      counts: {},
      run_id: 2, // this page is showing run #2
    });

    await waitFor(() =>
      expect(getRun.mock.calls.length).toBeGreaterThan(callsBefore),
    );
  });
});

describe("RunDetailPage — a queued run has not started", () => {
  it("says it is waiting, not that it started and is still running", async () => {
    // `runs.started_at` is the INSERT default — stamped when the run was ASKED for. The header read
    // "started <time> · still running" directly under a badge saying "Queued", and because the Rows
    // page's Run button navigates straight here, that contradiction was the first thing you saw
    // after pressing it.
    getRun.mockResolvedValue({
      ...run([]),
      status: "queued",
      started_at: "2026-07-15T04:18:00Z",
      finished_at: null,
      users: [],
      stats: {},
    });
    renderDetail();

    expect(await screen.findByText(/waiting to start/i)).toBeInTheDocument();
    expect(screen.queryByText(/still running/i)).toBeNull();
  });

  it("still says 'still running' for a run that genuinely is", async () => {
    getRun.mockResolvedValue({
      ...run([]),
      status: "running",
      finished_at: null,
    });
    renderDetail();

    expect(await screen.findByText(/still running/i)).toBeInTheDocument();
    expect(screen.queryByText(/waiting to start/i)).toBeNull();
  });
});
