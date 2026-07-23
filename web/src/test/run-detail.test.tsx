import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RunDetailPage } from "@/pages/run-detail";
import type { RunDetail } from "@/lib/types";

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

// useSSE opens an EventSource; jsdom has none, so stub a no-op one.
class FakeEventSource {
  addEventListener() {}
  close() {}
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
}
vi.stubGlobal("EventSource", FakeEventSource);

function run(breakdown: RunDetail["users"][number]["breakdown"]): RunDetail {
  return {
    id: 2,
    trigger: "manual",
    status: "ok",
    started_at: "2026-07-15T04:18:00Z",
    finished_at: "2026-07-15T04:24:00Z",
    dry_run: false,
    stats: { users_ok: 1, users_error: 0, titles_requested: 0 },
    users: [
      {
        username: "MooHouse",
        slug: "moohouse",
        status: "ok",
        error: null,
        reason: null,
        duration_ms: 335000,
        llm_tokens: 5030,
        diff: {},
        picks: [],
        breakdown,
      },
    ],
  } as RunDetail;
}

function renderDetail() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/runs/2"]}>
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

    renderDetail();

    // Duration is computed from started_at → finished_at (04:18 → 04:24 = 6 minutes).
    expect(await screen.findByText("Duration")).toBeInTheDocument();
    expect(screen.getByText("6m 0s")).toBeInTheDocument();
    expect(screen.getByText("People")).toBeInTheDocument();
    expect(screen.getByText("1 failed")).toBeInTheDocument(); // 1 of the 3 users errored
    expect(screen.getByText("+5/−3")).toBeInTheDocument();
    expect(screen.getByText("377,428")).toBeInTheDocument();
    expect(screen.getByText(/final picks 251,295/)).toBeInTheDocument();
    expect(screen.getByText("Exa searches")).toBeInTheDocument();
    expect(screen.getByText("46")).toBeInTheDocument();
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

    renderDetail();

    expect(await screen.findByText("all succeeded")).toBeInTheDocument();
    // No AI this run → those tiles don't render at all (0-value tiles would be noise).
    expect(screen.queryByText("AI tokens")).not.toBeInTheDocument();
    expect(screen.queryByText("Exa searches")).not.toBeInTheDocument();
  });

  it("falls back to a plain AI-tokens hint when there's no by-step breakdown", async () => {
    const r = run([]);
    r.stats = { users_ok: 1, users_error: 0, llm_tokens: 9000 }; // legacy run: total but no split
    getRun.mockResolvedValue(r);

    renderDetail();

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
          picks: [{ rank: 1, title: "Heat", reason: "crime", seed_title: "" }],
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
          picks: [{ rank: 1, title: "Fargo", reason: "crime", seed_title: "" }],
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
          picks: [{ rank: 1, title: "A", reason: "a" }],
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
          picks: [{ rank: 1, title: "B", reason: "b" }],
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

    renderDetail();

    // The stage renders with its human label + the count detail.
    expect(await screen.findByText(/curating with AI/)).toBeInTheDocument();
    expect(screen.getByText(/120 candidates/)).toBeInTheDocument();
  });

  it("falls back to the flat pick list for legacy runs with no breakdown", async () => {
    // A legacy run has no per-library breakdown, but its picks still render as a plain list.
    getRun.mockResolvedValue({
      ...run([]),
      users: [
        {
          username: "MooHouse",
          slug: "moohouse",
          status: "ok",
          error: null,
          reason: null,
          duration_ms: 1000,
          llm_tokens: 0,
          diff: {},
          picks: [{ rank: 1, title: "Old Title", reason: "legacy" }],
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

    renderDetail();

    // Both surfaces that used to say "succeeded": the People tile's hint and the list summary.
    expect(
      await screen.findByText(/3 skipped, built nothing/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/3 skipped — nothing was built/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("all succeeded")).toBeNull();
    // …and the person panel explains WHY rather than leaving them on "Working on this person…".
    expect(
      await screen.findByText(/no per-person rows to build/i),
    ).toBeInTheDocument();
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

    expect(await screen.findByText("Joe - Richard's Mate")).toBeInTheDocument();
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

    renderDetail();

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
    renderDetail();
    // The tiles only render once a run has finished — wait for one, then assert no alarm.
    expect(await screen.findByText("all succeeded")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
