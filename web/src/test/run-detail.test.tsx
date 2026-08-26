import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RunDetailPage } from "@/pages/run-detail";
import type { RunLogEntry, RunDetail } from "@/lib/types";

const { getRun, getUsers, getRunLog, listCollections } = vi.hoisted(() => ({
  // The Rows tab names a row from the collections config. Unmocked, that query never settles and no
  // row finishes rendering — which looks exactly like a row needing to be expanded.
  listCollections: vi.fn(),
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
      listCollections: () => listCollections(),
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
    began_at: "2026-07-15T04:18:00Z",
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
        rows_considered: { picked: "due" },
        display_name: "MooHouse",
        error: null,
        reason: null,
        exa_searches: 0,
        has_trace: false,
        llm_tokens_by_step: {},
        duration_ms: 335000,
        llm_tokens: 5030,
        diff: {},
        cost: null,
        picks: [],
        breakdown,
      },
    ],
  } as RunDetail;
}

/** `query` deep-links a tab (`?tab=users`) — the same URL a person's Runs tab links to, so these
 *  tests exercise the real entry point rather than a state the UI can't reach. */
/** Open every closed row card. A person's picks live inside the row that built them, and a row with
 *  siblings starts collapsed, so a test that wants picks opens the row first.
 *
 *  `findAllByRole`, not `queryAllByRole`: the tab strip renders before the run data arrives, so a
 *  query resolves against a page with no rows yet and clicks nothing. The catch covers a run that
 *  genuinely has no rows — a real case, not a failure. */
async function expandRows() {
  let toggles: HTMLElement[];
  try {
    toggles = await screen.findAllByRole("button", { expanded: false });
  } catch {
    return;
  }
  for (const toggle of toggles) {
    await userEvent.click(toggle);
  }
}

function renderDetail(query = "") {
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
    listCollections.mockResolvedValue([
      { slug: "picked", name: "✨ {library_name} Picked for You" },
      { slug: "gems", name: "💎 Hidden Gems" },
    ]);
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

    await expandRows();

    // A row spanning two libraries shows them as TABS — the selected library's picks only, so the
    // page stays short. Movies is selected first; TV Shows appears when you click it.
    expect(
      within(await screen.findByRole("group", { name: /librar/i })).getByRole(
        "button",
        { name: /Movies/ },
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("group", { name: /librar/i })).getByRole(
        "button",
        { name: /TV Shows/ },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/war epic/)).toBeInTheDocument();
    // This page is where "why did it pick that?" gets asked, and it has its OWN pick renderer
    // rather than using PickList — so the provenance line has to be asserted here separately.
    expect(
      screen.getByText(/suggested by TMDB · loosely related/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/survival series/)).not.toBeInTheDocument();

    await userEvent.click(
      within(screen.getByRole("group", { name: /librar/i })).getByRole(
        "button",
        { name: /TV Shows/ },
      ),
    );
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

    await expandRows();

    // Duration is computed from started_at → finished_at (04:18 → 04:24 = 6 minutes).
    expect((await screen.findAllByText("Duration"))[0]).toBeInTheDocument();
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
      // 7, not 1: the Rows-built tile also renders a small integer, and "1" appearing twice made
      // this assertion ambiguous rather than wrong.
      exa_searches: 7,
      exa_cache_hits: 793,
    };
    getRun.mockResolvedValue(r);

    renderDetail("");

    await expandRows();

    expect((await screen.findAllByText("Web searches"))[0]).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument(); // actually searched
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

    await expandRows();

    expect(
      (await screen.findAllByText("all succeeded"))[0],
    ).toBeInTheDocument();
    // No AI this run → those tiles don't render at all (0-value tiles would be noise).
    expect(screen.queryByText("AI tokens")).not.toBeInTheDocument();
    expect(screen.queryByText("Web searches")).not.toBeInTheDocument();
  });

  it("falls back to a plain AI-tokens hint when there's no by-step breakdown", async () => {
    const r = run([]);
    r.stats = { users_ok: 1, users_error: 0, llm_tokens: 9000 }; // legacy run: total but no split
    getRun.mockResolvedValue(r);

    renderDetail("");

    await expandRows();

    expect((await screen.findAllByText("9,000"))[0]).toBeInTheDocument();
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

    await expandRows();

    expect(
      (await screen.findAllByText("Movies Picked for You"))[0],
    ).toBeInTheDocument();
    expect(
      screen.queryByText("TV Shows Picked for You"),
    ).not.toBeInTheDocument();

    await userEvent.click(
      within(screen.getByRole("group", { name: /librar/i })).getByRole(
        "button",
        { name: /TV Shows/ },
      ),
    );
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

    await expandRows();

    // Each row shows as its own group header — not collapsed into one.
    expect(
      (await screen.findAllByText("✨ Picked for You"))[0],
    ).toBeInTheDocument();
    expect(screen.getAllByText("💎 Hidden Gems")[0]).toBeInTheDocument();
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

    await expandRows();

    expect(await screen.findByText(/Finishing up/)).toBeInTheDocument();
    // Named in the header AND in Overview's latest-activity panel — both answer "what is it
    // doing right now", so both carry it.
    expect(
      screen.getAllByText("checking for stranded rows").length,
    ).toBeGreaterThan(0);
  });

  it("counts people mid-run instead of claiming a run 1-of-3 in is finishing up", async () => {
    // Run #10 (2026-08-17): the header read "Finishing up · getting ready — reading your libraries"
    // while the Rows tab beside it read "9 of 46 people done". `preparing` is the newest SERVER
    // line for the whole per-user stretch, so reading back to it reported the index build long
    // after it ended — under a lead-in that said the run was nearly over.
    const base = run([]);
    getRun.mockResolvedValue({
      ...base,
      finished_at: null,
      status: "running",
      // The roster the run declared, and the one person it has reported on — the same two fields
      // the Rows tab counts, so the header cannot disagree with the card beside it.
      stats: {
        ...base.stats,
        expected_users: [
          { slug: "moohouse" },
          { slug: "sarah" },
          { slug: "mike" },
        ],
      },
    });
    getRunLog.mockResolvedValue(
      [
        { user: "moohouse", stage: "queued" },
        { user: "sarah", stage: "queued" },
        { user: "mike", stage: "queued" },
        { user: "Shortlist", stage: "preparing" },
        // Neither of these is a person: the index narrates under the section title and a shared row
        // under `shared_<row>`. Counting log subjects made them two extra people.
        { user: "Movies", stage: "indexed" },
        { user: "shared_popular", stage: "delivering" },
        { user: "moohouse", stage: "done" },
        { user: "sarah", stage: "curating" },
      ].map((line, seq) => ({
        seq,
        ts: "2026-08-17T03:30:00Z",
        run_id: 2,
        counts: {},
        ...line,
      })),
    );

    renderDetail("");

    expect(
      await screen.findByText("building rows — 1 of 3 people done"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Right now/)).toBeInTheDocument();
    expect(screen.queryByText(/Finishing up/)).toBeNull();
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
          rows_considered: { picked: "due" },
          status: "ok",
          error: null,
          reason: null,
          duration_ms: 1000,
          llm_tokens: 0,
          llm_tokens_by_step: {},
          exa_searches: 0,
          has_trace: false,
          diff: {},
          // A legacy run predates per-row cost measurement — "not recorded", never 0s.
          cost: null,
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

    await expandRows();

    expect((await screen.findAllByText("Old Title"))[0]).toBeInTheDocument();
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

    await expandRows();
    await screen.findAllByText("Fresh One");

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
    rows_considered: { picked: "due" },
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

    await expandRows();

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

    await expandRows();

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

    renderDetail("");

    await expandRows();

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

    renderDetail("");

    await expandRows();

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

    await expandRows();

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

    await expandRows();

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
    await expandRows();
    // The tiles only render once a run has finished — wait for one, then assert no alarm.
    expect(
      (await screen.findAllByText("all succeeded"))[0],
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

function failedUser(username: string, error: string) {
  return {
    username,
    slug: username,
    rows_considered: { picked: "due" },
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
    listCollections.mockResolvedValue([
      { slug: "picked", name: "✨ {library_name} Picked for You" },
      { slug: "gems", name: "💎 Hidden Gems" },
    ]);
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
    renderDetail("");
    await expandRows();

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

    await expandRows();

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
    listCollections.mockResolvedValue([
      { slug: "picked", name: "✨ {library_name} Picked for You" },
      { slug: "gems", name: "💎 Hidden Gems" },
    ]);
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

    renderDetail("");

    await expandRows();

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
    listCollections.mockResolvedValue([
      { slug: "picked", name: "✨ {library_name} Picked for You" },
      { slug: "gems", name: "💎 Hidden Gems" },
    ]);
    getRunLog.mockResolvedValue([]);
    FakeEventSource.instances = [];
  });

  it("does not refetch when the stage event belongs to a different run", async () => {
    // Sitting on finished run #2 while another run (#40) streams its own stage events used to
    // refetch #2 on every single one of them — `onRunUserStage` had no run_id guard at all, unlike
    // `onRunFinished` right below it, which correctly checked `event.run_id === runId`.
    getRun.mockResolvedValue(run([]));
    renderDetail("");
    await expandRows();
    await screen.findAllByText("all succeeded");

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
    await expandRows();
    await screen.findAllByText("all succeeded");

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
      // NULL, because status and `began_at` are written in the same commit — a queued run has never
      // had one. Set, this fixture described a state the code cannot produce, and the assertion
      // below would have passed with the queued branch deleted.
      began_at: null,
      finished_at: null,
      users: [],
      stats: {},
    });
    renderDetail();
    await expandRows();

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
    await expandRows();

    expect(await screen.findByText(/still running/i)).toBeInTheDocument();
    expect(screen.queryByText(/waiting to start/i)).toBeNull();
  });
});

describe("RunDetailPage — a run that failed for PEOPLE, not for itself", () => {
  beforeEach(() => {
    getRun.mockReset();
    getUsers.mockReset();
    getUsers.mockResolvedValue([]);
    getRunLog.mockReset();
    getRunLog.mockResolvedValue([]);
    listCollections.mockReset();
    listCollections.mockResolvedValue([]);
  });

  const PLEX_409 =
    "BadRequest: (409) conflict; http://plex:32400/library/sections/2/all?id=578636 <html><head><title>Conflict</title></head></html>";

  /** Run 4 on a real 46-user server: `users_ok: 45, users_error: 1`, `stats.error` null, no
   *  promotion blockers. The page said the run failed and then rendered no banner at all. */
  function runWithFailures(
    failures: { name: string; error: string }[],
    ok = 45,
  ): RunDetail {
    const base = run([]);
    return {
      ...base,
      status: "error",
      error: null,
      promotion_blockers: [],
      stats: {
        users_ok: ok,
        users_error: failures.length,
        titles_requested: 0,
      },
      users: [
        ...base.users,
        ...failures.map((f) => ({
          ...base.users[0]!,
          username: f.name,
          slug: f.name.toLowerCase(),
          display_name: f.name,
          status: "error",
          error: f.error,
        })),
      ],
    } as RunDetail;
  }

  it("names the person and the reason instead of an empty failure", async () => {
    getRun.mockResolvedValue(
      runWithFailures([{ name: "Jarrah", error: PLEX_409 }]),
    );
    renderDetail();

    const alert = await screen.findByTestId("run-failure");
    expect(alert).toHaveTextContent(/1 person didn.t get their rows/i);
    expect(alert).toHaveTextContent(/Jarrah/);
    expect(alert).toHaveTextContent(/409/);
  });

  it("groups people under one shared cause rather than repeating it per person", async () => {
    // A PMS outage fails everyone with the same CLASS of error, but never the same string: the
    // engine stores `f"{type(e).__name__}: {e}"` and a plexapi message embeds that user's own
    // ratingKey and row title. Grouping on the raw text therefore reported one 500 as "4 people
    // didn't get their rows, for 4 different reasons" — the exact opposite of what this banner is
    // for. These three differ byte-for-byte, exactly as they would on a real server.
    getRun.mockResolvedValue(
      runWithFailures([
        {
          name: "Jarrah",
          error:
            "BadRequest: (500) internal_server_error; http://pms:32400/library/sections/2/all?id=771",
        },
        {
          name: "Sam",
          error:
            "BadRequest: (500) internal_server_error; http://pms:32400/library/sections/2/all?id=982",
        },
        {
          name: "Nikki",
          error:
            "BadRequest: (500) internal_server_error; http://pms:32400/library/sections/1/all?id=1043",
        },
      ]),
    );
    renderDetail();

    const alert = await screen.findByTestId("run-failure");
    expect(alert).toHaveTextContent(/3 people didn.t get their rows/i);
    expect(alert).not.toHaveTextContent(/different reasons/i);
    expect(alert).toHaveTextContent(/Jarrah, Sam, Nikki/);
    expect(within(alert).getAllByText(/internal_server_error/i)).toHaveLength(
      1,
    );
  });

  it("says so when the failures had different causes", async () => {
    getRun.mockResolvedValue(
      runWithFailures([
        { name: "Jarrah", error: PLEX_409 },
        { name: "Sam", error: "ConnectionError: No route to host" },
      ]),
    );
    renderDetail();

    const alert = await screen.findByTestId("run-failure");
    expect(alert).toHaveTextContent(
      /2 people didn.t get their rows, for 2 different reasons/i,
    );
  });

  it("still leads with the run-level reason when there is one", async () => {
    // A run-level failure is not a per-person one, and must not be reworded as though it were.
    const base = runWithFailures([{ name: "Jarrah", error: PLEX_409 }]);
    getRun.mockResolvedValue({
      ...base,
      error: "Engine blew up before it started",
    });
    renderDetail();

    const alert = await screen.findByTestId("run-failure");
    expect(alert).toHaveTextContent(/didn.t finish cleanly/i);
    expect(alert).toHaveTextContent(/Engine blew up before it started/);
  });

  it("shows no banner on a healthy run", async () => {
    getRun.mockResolvedValue(run([]));
    renderDetail();

    await screen.findAllByText(/MooHouse/);
    expect(screen.queryByTestId("run-failure")).toBeNull();
  });
});
