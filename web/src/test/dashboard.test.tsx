/**
 * The dashboard opens on last night's run and its privacy state: a strip of four facts (last run,
 * next run, privacy, Plex), an amber callout when an account cannot be hidden, then the Impact
 * report. With no run yet and nothing delivered it is a first-run panel instead.
 *
 * Reporting only. Every fact restates something an endpoint already decided — `/api/report`,
 * `/api/runs`, `/api/schedule`, `/api/privacy/status` — and an unmeasured thing never reads as clean.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useParams } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { DashboardPage } from "@/pages/dashboard";
import type {
  AccountPrivacy,
  EffectivenessReport,
  PrivacyStatus,
  Run,
  ScheduleResponse,
  User,
} from "@/lib/types";

const { getReport, getRuns, getSchedule, getPrivacyStatus, getUsers, startRun } = vi.hoisted(() => ({
  getReport: vi.fn(),
  getRuns: vi.fn(),
  getSchedule: vi.fn(),
  getPrivacyStatus: vi.fn(),
  getUsers: vi.fn(),
  startRun: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getReport: () => getReport(),
      getRuns: (collection?: string, beforeId?: number, limit?: number) => getRuns(collection, beforeId, limit),
      getSchedule: () => getSchedule(),
      getPrivacyStatus: () => getPrivacyStatus(),
      getUsers: () => getUsers(),
      startRun: (body: unknown) => startRun(body),
    },
  };
});

// The Impact report has its own suite (impact-report.test.tsx); here it only has to be there or not.
vi.mock("@/components/dashboard/impact-report", () => ({
  ImpactReport: () => <p>impact report</p>,
}));

function report(runs: Partial<EffectivenessReport["runs"]> = {}, firstPick: string | null = "2026-09-01T00:00:00Z") {
  return {
    first_pick: firstPick,
    runs: {
      total: 1,
      in_window: 1,
      in_window_delta: null,
      last_finished: "2026-10-03T02:30:07Z",
      last_status: "ok",
      errors_last: 0,
      ...runs,
    },
  } as unknown as EffectivenessReport;
}

// What the browser cache accepts: every object and array field the report body reads.
function cachedReport() {
  return {
    ...report(),
    overall: {},
    coverage: {},
    requests: {},
    watch_sync: {},
    trend: [],
    per_user: [],
    per_row: [],
    top_titles: [],
    recent: [],
  };
}

const NO_RUN = report({ total: 0, in_window: 0, last_finished: null, last_status: null }, null);

function finishedRun(overrides: Partial<Run> = {}): Run {
  return {
    id: 8,
    trigger: "schedule",
    status: "ok",
    dry_run: false,
    started_at: "2026-10-03T02:30:00Z",
    began_at: "2026-10-03T02:30:04Z",
    finished_at: "2026-10-03T02:30:07Z",
    error: null,
    promotion_blockers: [],
    privacy: { can_see_others: [], unreadable_filters: [], filters_not_enforced: [], unchecked: [], write_failed: [], left_alone: [] },
    stats: { users_ok: 3, users_error: 0 },
    ...overrides,
  } as Run;
}

function account(user: string, state: string, overrides: Partial<AccountPrivacy> = {}): AccountPrivacy {
  return {
    account_id: user.length,
    display_name: user,
    hides: [],
    manage_sharing: state !== "left_alone",
    missing: [],
    other_conditions: [],
    restriction_profile: "",
    should_hide: [],
    slug: user,
    state,
    user,
    user_id: user.length,
    user_type: state === "owner" ? "owner" : "shared",
    ...overrides,
  };
}

function privacy(overrides: Partial<PrivacyStatus> = {}): PrivacyStatus {
  return {
    accounts: [],
    enforcement: {} as PrivacyStatus["enforcement"],
    error: null,
    read_at: "2026-10-03T11:46:00Z",
    rows_error: null,
    rows_on_plex: [],
    snapshots_kept: 0,
    summary: "clean",
    ...overrides,
  };
}

const KID_EXPOSED = privacy({
  rows_on_plex: ["shortlist_sarah", "shortlist_mike", "shortlist_jess", "shortlist_kid"],
  summary: "unhideable",
  accounts: [
    account("owner", "owner"),
    account("sarah", "hiding"),
    account("mike", "hiding"),
    account("jess", "hiding"),
    account("kid", "refused_by_plex", {
      restriction_profile: "older_kid",
      should_hide: ["shortlist_sarah", "shortlist_mike", "shortlist_jess"],
      missing: ["shortlist_sarah", "shortlist_mike", "shortlist_jess"],
    }),
  ],
});

const inHours = (hours: number) => new Date(Date.now() + hours * 3_600_000).toISOString();
const clock = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

function schedule(rowsNext: string | null, jobNext: string | null = null): ScheduleResponse {
  return {
    jobs: [
      {
        type: "job",
        kind: "privacy_sync",
        label: "Privacy sync",
        description: "",
        setting: "privacy.sync_cron",
        cron: "*/30 * * * *",
        using_default: true,
        default_cron: "*/30 * * * *",
        optional: false,
        writes_plex: true,
        next_run: jobNext,
      },
    ],
    rows: rowsNext === null ? [] : [{ type: "rows", cron: "30 2 * * *", rows: [], next_run: rowsNext }],
  };
}

function user(username: string, enabled = true): User {
  return { id: username.length, username, display_name: username, slug: username, enabled, departed: false } as User;
}

function RunPage() {
  return <p>run page {useParams().id}</p>;
}

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/runs/:id" element={<RunPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const strip = () => screen.findByRole("region", { name: "Status" });

beforeEach(() => {
  vi.clearAllMocks();
  getReport.mockResolvedValue(report());
  getRuns.mockResolvedValue([finishedRun()]);
  getSchedule.mockResolvedValue(schedule(inHours(5)));
  getPrivacyStatus.mockResolvedValue(privacy());
  getUsers.mockResolvedValue([user("sarah"), user("mike"), user("jess"), user("kid"), user("gone", false)]);
  startRun.mockResolvedValue({ run_id: 42 });
});

describe("the dashboard with no run yet", () => {
  beforeEach(() => {
    getReport.mockResolvedValue(NO_RUN);
    getRuns.mockResolvedValue([]);
  });

  it("offers Run now and Dry run first, and no link off to Runs", async () => {
    renderDashboard();

    expect(await screen.findByRole("heading", { name: "Build everyone’s rows for the first time" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Run now/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Dry run first/ })).toBeInTheDocument();
    const toRuns = screen.queryAllByRole("link").filter((link) => link.getAttribute("href")?.startsWith("/runs"));
    expect(toRuns).toHaveLength(0);
    expect(screen.queryByText("impact report")).toBeNull();
  });

  it("names the people the first run builds for", async () => {
    renderDashboard();

    expect(await screen.findByText(/Reads 4 people’s watch history/)).toBeInTheDocument();
    expect(screen.getByText("sarah, mike, jess and kid")).toBeInTheDocument();
  });

  it("says there is no run yet, and nothing to hide yet, rather than any count", async () => {
    renderDashboard();

    const status = await strip();
    expect(await within(status).findByText("None yet")).toBeInTheDocument();
    expect(await within(status).findByText("Nothing to hide yet")).toBeInTheDocument();
    expect(within(status).queryByText(/hide every row/)).toBeNull();
  });

  it("says 'Not measured' when the rows on Plex could not be read", async () => {
    getPrivacyStatus.mockResolvedValue(privacy({ summary: "rows_unknown", rows_error: "PMS timed out" }));
    renderDashboard();

    expect(await within(await strip()).findByText("Not measured")).toBeInTheDocument();
    expect(within(await strip()).queryByText(/hide every row/)).toBeNull();
  });

  it("starts a real run from Run now and opens it", async () => {
    renderDashboard();

    await userEvent.click(await screen.findByRole("button", { name: /Run now/ }));

    expect(startRun).toHaveBeenCalledWith({});
    expect(await screen.findByText("run page 42")).toBeInTheDocument();
  });

  it("starts a dry run from Dry run first", async () => {
    renderDashboard();

    await userEvent.click(await screen.findByRole("button", { name: /Dry run first/ }));

    expect(startRun).toHaveBeenCalledWith({ dry_run: true });
  });
});

describe("the dashboard's Last run", () => {
  it("reads 'OK · 1 warning', never a plain OK, when the latest run flagged an account", async () => {
    getRuns.mockResolvedValue([
      finishedRun({ privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [], unchecked: [], write_failed: [], left_alone: [] } }),
    ]);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-last-run");
    expect(await within(cell).findByText("OK · 1 warning")).toBeInTheDocument();
    expect(within(cell).queryByText("OK")).toBeNull();
  });

  // Moved here from the Impact report's footer, which repeated this cell: the three tiers the bell
  // draws (`_last_run_problem`) — a failed run, an OK run that failed somebody, a clean one.
  it("counts the people an OK run failed, with an amber dot", async () => {
    getRuns.mockResolvedValue([finishedRun({ privacy: null, stats: { users_ok: 3, users_error: 2 } })]);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-last-run");
    expect(await within(cell).findByText(/· 2 failed/)).toBeInTheDocument();
    const dot = cell.querySelector("span[class*='rounded-full']");
    expect(dot?.className).toMatch(/warning/);
  });

  it("keeps a failed run red even when people had already failed on the way down", async () => {
    getRuns.mockResolvedValue([
      finishedRun({ status: "error", privacy: null, stats: { users_ok: 0, users_error: 3 } }),
    ]);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-last-run");
    expect(await within(cell).findByText("Failed")).toBeInTheDocument();
    expect(cell.querySelector("span[class*='rounded-full']")?.className).toMatch(/destructive/);
  });

  it("names no failures when everyone was built", async () => {
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-last-run");
    expect(await within(cell).findByText("OK")).toBeInTheDocument();
    expect(within(cell).queryByText(/failed/)).toBeNull();
    expect(cell.querySelector("span[class*='rounded-full']")?.className).toMatch(/success/);
  });

  it("reads plain OK when the latest run did not measure privacy", async () => {
    getRuns.mockResolvedValue([finishedRun({ privacy: null })]);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-last-run");
    expect(await within(cell).findByText("OK")).toBeInTheDocument();
    expect(within(cell).queryByText(/warning/)).toBeNull();
  });

  it("says when the latest run was a dry run, which wrote nothing", async () => {
    getRuns.mockResolvedValue([finishedRun({ dry_run: true, privacy: null })]);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-last-run");
    expect(await within(cell).findByText(/dry run/)).toBeInTheDocument();
  });

  it("reads the newest FINISHED run, not one still going", async () => {
    getRuns.mockResolvedValue([
      finishedRun({ id: 9, status: "running", finished_at: null, privacy: null }),
      finishedRun({ id: 8, privacy: { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [], unchecked: [], write_failed: [], left_alone: [] } }),
    ]);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-last-run");
    expect(await within(cell).findByText("OK · 1 warning")).toBeInTheDocument();
    expect(within(cell).getByRole("link")).toHaveAttribute("href", "/runs/8");
  });
});

describe("the dashboard's Privacy", () => {
  it("counts the private accounts and names the exposed one in rows, with the link left to the callout", async () => {
    getPrivacyStatus.mockResolvedValue(KID_EXPOSED);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-privacy");
    expect(await within(cell).findByText("3 of 4 private")).toBeInTheDocument();
    expect(within(cell).getByText(/kid can see 3 rows that aren’t theirs/)).toBeInTheDocument();
    expect(within(cell).queryByRole("link")).toBeNull();
  });

  it("links the cell to Privacy when no callout carries the fix", async () => {
    getPrivacyStatus.mockResolvedValue(
      privacy({
        rows_on_plex: ["shortlist_sarah"],
        accounts: [account("sarah", "hiding"), account("mike", "missing", { missing: ["shortlist_sarah"] })],
      }),
    );
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-privacy");
    expect(await within(cell).findByRole("link", { name: /mike can see/ })).toHaveAttribute("href", "/privacy");
    expect(screen.queryByTestId("privacy-callout")).toBeNull();
  });

  it("calls out an account Plex will not hide rows from, naming its restriction profile", async () => {
    getPrivacyStatus.mockResolvedValue(KID_EXPOSED);
    renderDashboard();

    const callout = await screen.findByTestId("privacy-callout");
    expect(callout).toHaveTextContent("kid’s Plex Restriction Profile (older_kid) blocks hide rules");
    expect(callout).toHaveTextContent("kid can see 3 rows that aren’t theirs");
    expect(within(callout).getByRole("link", { name: /How to fix in Plex/ })).toHaveAttribute("href", "/privacy");
    // Said once: the strip's cell carries the figure but not a second link to the same place.
    expect(screen.getAllByRole("link", { name: /fix in Plex/ })).toHaveLength(1);
  });

  // Before any row exists there is nothing to hide — but an account Plex refuses hide rules for is
  // still one Shortlist cannot hide anything from, and "Every row is hidden before it appears" would be
  // untrue of it the moment its first row lands. True with or without a run behind it.
  const FRESH_WITH_KID = privacy({
    rows_on_plex: [],
    accounts: [
      account("owner", "owner"),
      account("sarah", "hiding"),
      account("kid", "refused_by_plex", { restriction_profile: "older_kid" }),
    ],
  });

  it("names the account that can't be hidden on a server with no run yet", async () => {
    getReport.mockResolvedValue(NO_RUN);
    getRuns.mockResolvedValue([]);
    getPrivacyStatus.mockResolvedValue(FRESH_WITH_KID);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-privacy");
    expect(await within(cell).findByText("Nothing to hide yet")).toBeInTheDocument();
    expect(within(cell).getByText(/1 account can’t be hidden/)).toBeInTheDocument();
    expect(within(cell).queryByText(/Every row is hidden/)).toBeNull();
  });

  it("names the account that can't be hidden after a run that left nothing to hide", async () => {
    getPrivacyStatus.mockResolvedValue(FRESH_WITH_KID);
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-privacy");
    expect(await within(cell).findByText(/1 account can’t be hidden/)).toBeInTheDocument();
    expect(within(cell).queryByText(/Every row is hidden/)).toBeNull();
  });

  it("promises every row is hidden before it appears only when every account can be hidden", async () => {
    getPrivacyStatus.mockResolvedValue(
      privacy({ rows_on_plex: [], accounts: [account("owner", "owner"), account("sarah", "hiding")] }),
    );
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-privacy");
    expect(await within(cell).findByText("Every row is hidden before it appears")).toBeInTheDocument();
  });

  it("has no callout when every account hides", async () => {
    getPrivacyStatus.mockResolvedValue(
      privacy({ rows_on_plex: ["shortlist_sarah"], accounts: [account("sarah", "hiding"), account("mike", "hiding")] }),
    );
    renderDashboard();

    expect(await within(await strip()).findByText("2 of 2 private")).toBeInTheDocument();
    expect(screen.queryByTestId("privacy-callout")).toBeNull();
  });
});

describe("the dashboard's Next run and Plex", () => {
  it("shows when the rows next build, not when some other job next fires", async () => {
    const rows = inHours(5);
    const job = inHours(0.5);
    getSchedule.mockResolvedValue(schedule(rows, job));
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-next-run");
    expect(await within(cell).findByText(new RegExp(clock(rows)))).toBeInTheDocument();
    expect(cell).not.toHaveTextContent(clock(job));
    expect(cell).toHaveTextContent("4 people enabled");
  });

  it("says Plex is connected when both reads answered", async () => {
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-plex");
    expect(await within(cell).findByText("Connected")).toBeInTheDocument();
  });

  it("says plex.tv is unreachable when the sharing read failed", async () => {
    getPrivacyStatus.mockResolvedValue(privacy({ summary: "unreadable", error: "plex.tv timed out" }));
    renderDashboard();

    const cell = await within(await strip()).findByTestId("status-plex");
    expect(await within(cell).findByText("plex.tv unreachable")).toBeInTheDocument();
  });
});

describe("the dashboard after a run", () => {
  it("shows the Impact report under the strip, with Run now as the one primary action", async () => {
    renderDashboard();

    expect(await screen.findByText("impact report")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Run now$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Dry run$/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /for the first time/ })).toBeNull();
  });
});

describe("the dashboard while the report loads", () => {
  it("shows a labelled skeleton, and no run buttons, when there is nothing cached", async () => {
    getReport.mockReturnValue(new Promise(() => {}));

    renderDashboard();

    const skeleton = await screen.findByRole("status", { name: "Loading the impact report" });
    expect(skeleton).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("button", { name: "Run now" })).not.toBeInTheDocument();
  });

  it("shows the remembered report at once, then lets the real one replace it", async () => {
    localStorage.setItem("shortlist.report.v1.30", JSON.stringify(cachedReport()));
    let resolve!: (value: EffectivenessReport) => void;
    getReport.mockReturnValue(new Promise<EffectivenessReport>((r) => (resolve = r)));

    renderDashboard();

    expect(await screen.findByText("impact report")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading the impact report" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeInTheDocument();

    resolve(report());
    expect(await screen.findByText("impact report")).toBeInTheDocument();
  });

  it("drops a cached normal report for the first-run panel once the real report says nothing has run", async () => {
    localStorage.setItem("shortlist.report.v1.30", JSON.stringify(cachedReport()));
    let resolve!: (value: EffectivenessReport) => void;
    getReport.mockReturnValue(new Promise<EffectivenessReport>((r) => (resolve = r)));

    renderDashboard();
    expect(await screen.findByText("impact report")).toBeInTheDocument();

    resolve(NO_RUN);

    await vi.waitFor(() => expect(screen.queryByText("impact report")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Dry run" })).not.toBeInTheDocument();
  });

  it("does not show the first-run panel from a cached report that is not first-run shaped", async () => {
    localStorage.setItem("shortlist.report.v1.30", JSON.stringify(cachedReport()));
    getReport.mockReturnValue(new Promise(() => {}));

    renderDashboard();

    expect(await screen.findByText("impact report")).toBeInTheDocument();
  });
});
