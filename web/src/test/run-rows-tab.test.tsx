import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { RunRowsTab } from "@/components/runs/run-rows-tab";
import type { RunDetail, RunUserResult } from "@/lib/types";

const CONFIG_NAMES = {
  picked: "✨ {library_name} Picked for You",
  because: "🎯 Because you watched {top_seed}",
  popular: "👥 Popular {library_name} on SFLIX",
};

const pick = (rank: number, title: string, reason = "") => ({
  rank,
  title,
  reason,
  seed_title: null,
  sources: [],
  affinity: 1,
  year: null,
  rating: null,
});

function user(overrides: Partial<RunUserResult> = {}): RunUserResult {
  return {
    username: "sarah",
    display_name: "Sarah",
    slug: "sarah",
    status: "ok",
    error: null,
    reason: null,
    duration_ms: 100,
    llm_tokens: 0,
    llm_tokens_by_step: {},
    exa_searches: 0,
    diff: {},
    picks: [],
    breakdown: [],
    has_trace: true,
    rows_considered: { picked: "due", because: "not_due" },
    ...overrides,
  } as unknown as RunUserResult;
}

/** Live run 41: one row selected, delivered to two libraries, for two people. */
function run(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 41,
    trigger: "manual",
    status: "ok",
    started_at: "2026-08-13T06:43:00Z",
    finished_at: "2026-08-13T07:13:00Z",
    dry_run: false,
    stats: {},
    error: null,
    promotion_blockers: [],
    shared_rows: [],
    users: [
      user({
        breakdown: [
          {
            row_slug: "picked",
            row_title: "✨ Movies Picked for You",
            library_key: "1",
            library_title: "Movies",
            added: ["Sicario"],
            removed: [],
            kept: [],
            deleted: [],
            created: false,
            picks: [pick(1, "Sicario", "you finished Wind River")],
          },
          {
            row_slug: "picked",
            row_title: "✨ TV Shows Picked for You",
            library_key: "2",
            library_title: "TV Shows",
            added: ["The Bear"],
            removed: [],
            kept: [],
            deleted: [],
            created: false,
            picks: [pick(1, "The Bear")],
          },
        ],
      }),
      user({ slug: "mike", display_name: "Mike", breakdown: [] }),
    ],
    ...overrides,
  } as unknown as RunDetail;
}

function renderTab(
  detail: RunDetail = run(),
  opts: { focusUser?: string } = {},
) {
  return render(
    <MemoryRouter>
      <RunRowsTab
        run={detail}
        titles={CONFIG_NAMES}
        idBySlug={new Map([["sarah", 1]])}
        focusUser={opts.focusUser}
      />
    </MemoryRouter>,
  );
}

describe("RunRowsTab", () => {
  it("shows only the row the run ran, and names the rest as not in it", () => {
    // The complaint: one row was selected, but the page listed every row that exists — so a scoped
    // run looked like it had touched rows the operator never chose.
    renderTab();

    expect(screen.getByText("✨ Picked for You")).toBeInTheDocument();
    expect(
      screen.queryByText("🎯 Because you watched"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/1 row wasn.t in this run/i)).toBeInTheDocument();
  });

  it("names a multi-library row once, with its libraries beside it", () => {
    // Movies looked like it never ran because the row took its name from whichever delivered title
    // arrived last. The row has ONE name; the libraries are their own field.
    renderTab();

    expect(screen.getByText("Movies · TV Shows")).toBeInTheDocument();
    expect(
      screen.queryByText("✨ TV Shows Picked for You"),
    ).not.toBeInTheDocument();
  });

  it("opens the row a deep-linked person is in, with them selected", () => {
    // `?user=` is built by every "Run #NN" link on a person's page. It outlived the People tab that
    // read it: the link was still constructed, nothing consumed it, and clicking it landed you on
    // the top of a run with forty other people in it.
    renderTab(run(), { focusUser: "sarah" });

    expect(screen.getAllByText("Sarah").length).toBeGreaterThan(1);
    expect(screen.getByText("Sicario")).toBeInTheDocument();
  });

  it("opens a single-row run and shows the picks, not just the names", () => {
    // The regression this replaces: expanding a row gave 46 names, a status and a Trace link, so
    // you could see WHO ran but never what they got without leaving the page. It now renders through
    // the People tab's own panel, so the libraries are TABS and the first one's picks are showing.
    renderTab();

    // Twice on purpose: once in the person list, once in the panel header that names whose result
    // is on screen. That header is also where the "How we picked" button lives — hoisting the button
    // without it left the button floating above the picks, belonging to nothing.
    expect(screen.getAllByText("Sarah")).toHaveLength(2);
    expect(screen.getByText("Sicario")).toBeInTheDocument();
    // The libraries are a segmented control in the reused panel, so only the first one's picks are
    // on screen and the other is one click away.
    const libraries = screen.getByRole("group", { name: /librar/i });
    expect(
      within(libraries).getByRole("button", { name: /TV Shows/ }),
    ).toBeInTheDocument();
  });

  it("gives the person list the search the People tab has, once there are enough people", () => {
    // Reusing `UserTabs` rather than a thinner list of names is what keeps the two tabs one design —
    // and it only offers a search box above ten people, which is the case that needs it.
    renderTab(
      run({
        users: Array.from({ length: 12 }, (_, i) =>
          user({ slug: `p${i}`, display_name: `Person ${i}` }),
        ),
      }),
    );

    expect(
      screen.getByLabelText(/Search users in this run/i),
    ).toBeInTheDocument();
  });

  it("switches the picks panel when another person is chosen", async () => {
    renderTab();
    expect(screen.getByText("Sicario")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Mike"));

    // Mike built nothing for this row, so Sarah's picks must not linger under his name — and his
    // own `picks` are dropped, because the API cannot split them per row.
    expect(screen.queryByText("Sicario")).not.toBeInTheDocument();
  });

  it("gives a shared row the same panel, minus the person picker", async () => {
    renderTab(
      run({
        users: [user({ rows_considered: { picked: "not_due" } })],
        shared_rows: [
          {
            collection_slug: "popular",
            row_title: "👥 Popular Movies on SFLIX",
            status: "ok",
            error: null,
            reason: null,
            duration_ms: 64000,
            llm_tokens: 0,
            llm_tokens_by_step: {},
            exa_searches: 0,
            diff: { added: ["Dune"], removed: [] },
            picks: [pick(1, "Dune")],
            breakdown: [
              {
                row_slug: "popular",
                row_title: "👥 Popular Movies on SFLIX",
                library_key: "1",
                library_title: "Movies",
                added: ["Dune"],
                removed: [],
                kept: [],
                deleted: [],
                created: false,
                picks: [pick(1, "Dune", "11 people watched it")],
              },
            ],
            has_trace: true,
          },
        ],
      } as unknown as Partial<RunDetail>),
    );

    expect(screen.getByText("👥 Popular on SFLIX")).toBeInTheDocument();
    // One library here, so no tab strip — with two it switches instead of stacking, which is what
    // kept a 40-pick row from scrolling for pages.
    expect(screen.getByText("Dune")).toBeInTheDocument();
    // No person picker — there is nobody to choose.
    expect(screen.queryByText("Sarah")).not.toBeInTheDocument();
    // Trace sits on the row, because the row is the thing it traces.
    expect(screen.getByRole("link", { name: /trace/i })).toHaveAttribute(
      "href",
      "/runs/41/trace/row/popular",
    );
  });

  it("explains a shared row that built nothing", () => {
    renderTab(
      run({
        users: [user({ rows_considered: { picked: "not_due" } })],
        shared_rows: [
          {
            collection_slug: "popular",
            row_title: "👥 Popular Movies on SFLIX",
            status: "skipped",
            error: null,
            reason:
              "A shared row needs at least 2 people with overlapping viewing.",
            duration_ms: 0,
            llm_tokens: 0,
            llm_tokens_by_step: {},
            exa_searches: 0,
            diff: {},
            picks: [],
            breakdown: [],
            has_trace: false,
          },
        ],
      } as unknown as Partial<RunDetail>),
    );

    expect(
      screen.getByText(/needs at least 2 people with overlapping viewing/i),
    ).toBeInTheDocument();
  });

  it("puts a failed person's error in the panel instead of an empty pick list", async () => {
    renderTab(
      run({
        users: [
          user({
            slug: "mike",
            display_name: "Mike",
            status: "error",
            error: "TMDB 401 Unauthorized",
            breakdown: [],
          }),
        ],
      }),
    );

    const alert = screen.getByRole("alert");
    expect(
      within(alert).getByText(/TMDB 401 Unauthorized/),
    ).toBeInTheDocument();
  });

  it("says so when a run built no rows at all", () => {
    renderTab(run({ users: [], shared_rows: [] }));

    expect(screen.getByText("This run built no rows")).toBeInTheDocument();
  });
});

describe("RunRowsTab — a run that is still going", () => {
  it("says it is working, not that the run is too old to show rows", () => {
    // A running run has nothing persisted yet, so it lands in the empty state — which used to blame
    // a legacy run for a run that had started seconds earlier.
    renderTab(
      run({
        users: [],
        shared_rows: [],
        finished_at: null,
      } as unknown as Partial<RunDetail>),
    );

    expect(screen.getByText(/Getting ready/i)).toBeInTheDocument();
    // The old copy pointed at a People tab that no longer exists.
    expect(screen.queryByText(/People tab/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/before this view existed/i),
    ).not.toBeInTheDocument();
  });

  it("still explains a finished run that genuinely built nothing", () => {
    renderTab(run({ users: [], shared_rows: [] }));

    expect(screen.getByText("This run built no rows")).toBeInTheDocument();
  });
});

describe("RunRowsTab — per-row cost", () => {
  const rowBreakdown = () => [
    {
      row_slug: "picked",
      row_title: "✨ Picked for You",
      library_key: "1",
      library_title: "Movies",
      added: [],
      removed: [],
      kept: [],
      deleted: [],
      created: false,
      picks: [],
    },
    {
      row_slug: "because",
      row_title: "🎯 Because you watched",
      library_key: "1",
      library_title: "Movies",
      added: [],
      removed: [],
      kept: [],
      deleted: [],
      created: false,
      picks: [],
    },
  ];

  const PERSON_WHOLE_RUN_MS = 442000; // "7m 22s" — must never appear on this row's panel

  /** Two rows sharing one gather/curate pool — the shape `RunUserOut.cost` sends. */
  const runWithPerRowCost = run({
    users: [
      user({
        duration_ms: PERSON_WHOLE_RUN_MS,
        rows_considered: { picked: "due", because: "due" },
        breakdown: rowBreakdown(),
        has_trace: false,
        cost: {
          setup_ms: 421000,
          rows: {
            picked: { duration_ms: 72300, blocked_ms: 300 },
            because: { duration_ms: 9120, blocked_ms: 880 },
          },
          pools: [
            {
              label: "movie · tmdb, llm_web",
              tokens: 15917,
              exa_searches: 3,
              duration_ms: 398000,
              rows: ["picked", "because"],
            },
          ],
        },
      }),
    ],
  });

  /** Two shared pools with DIFFERENT row counts — the shape a server with both a Movies and a TV
   *  Shows library produces. Naming only the first pool's row count here would misstate the second. */
  const runWithTwoSharedPools = run({
    users: [
      user({
        duration_ms: PERSON_WHOLE_RUN_MS,
        rows_considered: { picked: "due", because: "due" },
        breakdown: rowBreakdown(),
        has_trace: false,
        cost: {
          setup_ms: 421000,
          rows: {
            picked: { duration_ms: 72300, blocked_ms: 300 },
            because: { duration_ms: 9120, blocked_ms: 880 },
          },
          pools: [
            {
              label: "movie · tmdb, llm_web",
              tokens: 15917,
              exa_searches: 3,
              duration_ms: 398000,
              rows: ["picked", "because"],
            },
            {
              label: "show · tmdb, llm_web",
              tokens: 4200,
              exa_searches: 1,
              duration_ms: 23000,
              rows: ["picked", "popular"],
            },
          ],
        },
      }),
    ],
  });

  const legacyRun = run({
    users: [
      user({
        duration_ms: PERSON_WHOLE_RUN_MS,
        rows_considered: { picked: "due", because: "due" },
        breakdown: rowBreakdown(),
        has_trace: false,
        cost: null,
      }),
    ],
  });

  it("shows this row's own time, not the person's whole-run total", async () => {
    render(
      <RunRowsTab run={runWithPerRowCost} titles={{}} idBySlug={new Map()} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Picked for You/ }),
    );
    // Row-scoped in both places it appears — the panel header and the person list beside it (see
    // the next test) — so "12s" now legitimately shows up twice; the point is that the person's
    // whole-run total ("7m 22s") shows up nowhere.
    expect(screen.getAllByText(/12s/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/7m 22s/)).not.toBeInTheDocument();
  });

  it("shows a different row-scoped time for the same person under each row", async () => {
    // The regression that started all of this: two rows for the same person read as costing the
    // same amount, because both showed the person's one whole-run duration. The person list next to
    // the panel is the exact spot the original bug report pointed at ("the people names have the
    // same processing time"), so it has to earn its own row-scoped number too, not just the header.
    render(
      <RunRowsTab run={runWithPerRowCost} titles={{}} idBySlug={new Map()} />,
    );
    const pickedToggle = screen.getByRole("button", { name: /Picked for You/ });

    await userEvent.click(pickedToggle);
    expect(screen.getByRole("tab")).toHaveTextContent("1m 12s");

    await userEvent.click(pickedToggle); // collapse before opening the other row
    await userEvent.click(
      screen.getByRole("button", { name: /Because you watched/ }),
    );
    expect(screen.getByRole("tab")).toHaveTextContent("8.2s");
  });

  it("attributes AI tokens to the shared setup, naming both rows that used the pool", async () => {
    render(
      <RunRowsTab run={runWithPerRowCost} titles={{}} idBySlug={new Map()} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Picked for You/ }),
    );
    expect(screen.getByText(/shared setup/i)).toBeInTheDocument();
    expect(screen.getByText(/15,917/)).toBeInTheDocument();
  });

  it("says how many pools were shared, not 'one pool', when more than one contributed", async () => {
    // The bug this guards: the panel used to find only the FIRST pool with more than one row and
    // report that pool's row count, so a server with two shared pools of different sizes (Movies and
    // TV Shows, here 2 rows and 2 rows but drawn from different row sets) read as "one pool" — wrong
    // about the second pool whenever its count differs from the first.
    render(
      <RunRowsTab
        run={runWithTwoSharedPools}
        titles={{}}
        idBySlug={new Map()}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Picked for You/ }),
    );
    expect(screen.getByText(/shared across 2 pools/i)).toBeInTheDocument();
    expect(screen.queryByText(/one pool, shared by/i)).not.toBeInTheDocument();
    // Both pools' tokens are still summed into the one figure shown.
    expect(screen.getByText(/20,117/)).toBeInTheDocument();
  });

  it("says timing was not recorded for a legacy run instead of showing 0s", async () => {
    render(<RunRowsTab run={legacyRun} titles={{}} idBySlug={new Map()} />);
    await userEvent.click(
      screen.getByRole("button", { name: /Picked for You/ }),
    );
    expect(screen.getByText(/not recorded/i)).toBeInTheDocument();
    expect(screen.queryByText(/^0s$/)).not.toBeInTheDocument();
  });
});

describe("RunRowsTab — a shared row that hasn't built yet", () => {
  it("says it is pending and explains why, instead of an empty box", async () => {
    // A shared row builds LAST, after every person, so this is the state it sits in for most of a
    // run. The panel returned nothing, which read as broken rather than as not-started.
    renderTab(
      run({
        users: [],
        shared_rows: [],
        finished_at: null,
        stats: {
          expected_rows: [
            {
              slug: "popular",
              title: CONFIG_NAMES.popular,
              build: "shared",
            },
          ],
        },
      } as unknown as Partial<RunDetail>),
    );

    expect(screen.getByText("👥 Popular on SFLIX")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();

    // Sole row, so it is already open — the panel must explain itself rather than be an empty box.
    expect(
      screen.getByText(/builds once everyone’s own rows are done/i),
    ).toBeInTheDocument();
  });
});
