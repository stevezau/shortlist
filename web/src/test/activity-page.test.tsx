/**
 * Activity: what used to be the Jobs and Logs pages, plus the record of every change Shortlist made on
 * Plex, as tabs of one page.
 *
 * The tab lives in `?tab=` so a link can open any one (the old /jobs and /logs redirect here), and
 * anything that is not a known tab falls back to Jobs rather than an empty page. The Jobs, Job history
 * and Log panels are the old pages' bodies and are covered by `jobs-page.test.tsx` and
 * `logs-page.test.tsx`; the "Changes on Plex" table is covered here.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChangesOnPlex } from "@/components/activity/changes-on-plex";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { AuditEvent, Run } from "@/lib/types";
import { ActivityPage } from "@/pages/activity";

vi.mock("@/pages/jobs", () => ({
  JobsPanel: () => <p>jobs panel</p>,
  JobHistoryPanel: () => <p>job history panel</p>,
}));
vi.mock("@/pages/logs", () => ({ LogsPanel: () => <p>log panel</p> }));

const { getEventLog, getRuns, getSettings, listCollections, getUsers } = vi.hoisted(() => ({
  getEventLog: vi.fn(),
  getRuns: vi.fn(),
  getSettings: vi.fn(),
  listCollections: vi.fn(),
  getUsers: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getEventLog: (params: unknown) => getEventLog(params),
      getRuns: () => getRuns(),
      getSettings: () => getSettings(),
      listCollections: () => listCollections(),
      getUsers: () => getUsers(),
    },
  };
});

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.search}</output>;
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderAt(path: string) {
  render(
    <QueryClientProvider client={client()}>
      <MemoryRouter initialEntries={[path]}>
        <ActivityPage />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ActivityPage", () => {
  beforeEach(() => {
    getEventLog.mockReset();
    getEventLog.mockResolvedValue([]);
    getRuns.mockResolvedValue([]);
    getSettings.mockResolvedValue({});
    listCollections.mockResolvedValue([]);
    getUsers.mockResolvedValue([]);
  });

  it("opens on Jobs, under one header for the whole page", () => {
    renderAt("/activity");

    expect(screen.getByRole("heading", { level: 1, name: "Activity" })).toBeInTheDocument();
    expect(screen.getByText(/what Shortlist is doing, what it did/i)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Jobs" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("jobs panel")).toBeInTheDocument();
    expect(screen.queryByText("log panel")).toBeNull();
  });

  it("has one level of tabs: the job history is a tab of the page, not a switch inside Jobs", () => {
    renderAt("/activity");

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Jobs",
      "Job history",
      "Log",
      "Changes on Plex",
    ]);
  });

  it("opens straight on the Log for ?tab=log", () => {
    renderAt("/activity?tab=log");

    expect(screen.getByRole("tab", { name: "Log" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("log panel")).toBeInTheDocument();
    expect(screen.queryByText("jobs panel")).toBeNull();
  });

  it("opens the job history for ?tab=history", () => {
    renderAt("/activity?tab=history");

    expect(screen.getByRole("tab", { name: "Job history" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("job history panel")).toBeInTheDocument();
    expect(screen.queryByText("jobs panel")).toBeNull();
  });

  it("still opens the job history from an old ?view=activity link", () => {
    // The Jobs panel's own inner switch wrote `?tab=jobs&view=activity`, and /jobs?tab=activity
    // redirects to that address, so bookmarks and stored notification links carry it.
    renderAt("/activity?tab=jobs&view=activity&filter=failed");

    expect(screen.getByRole("tab", { name: "Job history" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("job history panel")).toBeInTheDocument();
  });

  it("opens the changes on Plex for ?tab=changes", async () => {
    renderAt("/activity?tab=changes");

    expect(screen.getByRole("tab", { name: "Changes on Plex" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Nothing has changed on Plex yet")).toBeInTheDocument();
  });

  it("falls back to Jobs for a tab it does not know", () => {
    renderAt("/activity?tab=timeline");

    expect(screen.getByRole("tab", { name: "Jobs" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("jobs panel")).toBeInTheDocument();
  });

  it("switches tabs from the keyboard and records the choice in the address", async () => {
    renderAt("/activity?tab=jobs");

    screen.getByRole("tab", { name: "Jobs" }).focus();
    await userEvent.keyboard("{ArrowRight}");

    expect(screen.getByRole("tab", { name: "Job history" })).toHaveFocus();
    expect(screen.getByText("job history panel")).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("?tab=history");
  });

  it("drops the Jobs tab's own view and filter when moving to the Log", async () => {
    renderAt("/activity?tab=jobs&view=activity&filter=failed");

    await userEvent.click(screen.getByRole("tab", { name: "Log" }));

    expect(screen.getByTestId("where").textContent).toBe("?tab=log");
  });
});

// --- Changes on Plex ------------------------------------------------------------------------------

function ev(id: number, scope: string, message: Record<string, unknown>, ts = "2026-10-03T02:31:00+00:00"): AuditEvent {
  return { id, ts, level: "info", scope, message };
}

const RUN_12: Run = {
  id: 12,
  started_at: "2026-10-03T02:30:00+00:00",
  began_at: "2026-10-03T02:30:00+00:00",
  finished_at: "2026-10-03T02:32:00+00:00",
  status: "done",
  trigger: "schedule",
  dry_run: false,
  error: null,
  privacy: null,
  promotion_blockers: [],
  stats: { users_ok: 4, users_error: 0 },
} as unknown as Run;

const SHARE_MERGE = ev(40, "run.privacy_sync", {
  run_id: 12,
  dry_run: false,
  plex_account_id: 501,
  username: "mike",
  fields: { filterMovies: { before: "", after: "label!=shortlist_kid" } },
});

const KID_ROW = ev(39, "run.user", {
  run_id: 12,
  dry_run: false,
  user: "kid",
  status: "ok",
  diff: { added: ["Arrival", "Dune"], removed: [], deleted: [], duplicates_removed: [], collection_title: "Movies Picked for You", created: true },
  error: null,
});

const MIKE_UNCHANGED = ev(38, "run.user", {
  run_id: 12,
  dry_run: false,
  user: "mike",
  status: "ok",
  diff: { added: [], removed: [], kept: ["Heat"], deleted: [], duplicates_removed: [], collection_title: "Movies Picked for You", created: false },
  error: null,
});

const DRY_MERGE = ev(
  20,
  "run.privacy_sync",
  {
    run_id: 9,
    dry_run: true,
    plex_account_id: 502,
    username: "jess",
    fields: { filterMovies: { before: "", after: "label!=shortlist_sarah" } },
  },
  "2026-10-02T08:02:44+00:00",
);

function renderChanges() {
  render(
    <QueryClientProvider client={client()}>
      <MemoryRouter>
        <ChangesOnPlex />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ChangesOnPlex", () => {
  beforeEach(() => {
    getEventLog.mockReset();
    getRuns.mockReset();
    getRuns.mockResolvedValue([RUN_12]);
    getSettings.mockReset();
    getSettings.mockResolvedValue({ "events.retention": 0 });
    listCollections.mockResolvedValue([]);
    getUsers.mockResolvedValue([]);
  });

  it("asks the server for the Plex writes only, one page at a time", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE]);
    renderChanges();

    await screen.findByText("mike");
    expect(getEventLog).toHaveBeenCalledWith({ plexWrites: true, beforeId: undefined, limit: 200 });
  });

  it("groups changes under the run that made them, newest first, with the run's own facts", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE, KID_ROW, DRY_MERGE]);
    renderChanges();

    const header = await screen.findByText(/Run #12/);
    const headerRow = header.closest("tr") as HTMLElement;
    expect(headerRow).toHaveTextContent(/Scheduled · 4 people/);
    const rows = screen.getAllByRole("row").map((row) => row.textContent ?? "");
    const at = (text: string) => rows.findIndex((row) => row.includes(text));
    // Run #12's header, then its two changes, then the older dry run's header and change.
    expect(at("Run #12")).toBeLessThan(at("Share filter merged"));
    expect(at("Share filter merged")).toBeLessThan(at("+2 titles, collection created"));
    expect(at("+2 titles, collection created")).toBeLessThan(at("Run #9"));
    expect(rows[at("Run #9")]).toMatch(/dry run · nothing written/i);
  });

  it("sets filter rules in the code face and labels each change Real or Dry run", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE, DRY_MERGE]);
    renderChanges();

    const rule = await screen.findByText("label!=shortlist_kid");
    expect(rule.tagName).toBe("CODE");
    const realRow = rule.closest("tr") as HTMLElement;
    expect(within(realRow).getByText("Real")).toBeInTheDocument();
    const dryRow = screen.getByText("label!=shortlist_sarah").closest("tr") as HTMLElement;
    expect(within(dryRow).getByText("Dry run")).toBeInTheDocument();
  });

  it("filters to real writes or dry runs", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE, DRY_MERGE]);
    renderChanges();
    await screen.findByText("mike");

    await userEvent.click(screen.getByRole("button", { name: "Dry run" }));
    expect(screen.queryByText("mike")).toBeNull();
    expect(screen.getByText("jess")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Real" }));
    expect(screen.getByText("mike")).toBeInTheDocument();
    expect(screen.queryByText("jess")).toBeNull();
  });

  it("filters by person, row or account", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE, KID_ROW]);
    renderChanges();
    await screen.findByText("mike");

    await userEvent.type(screen.getByRole("searchbox", { name: "Filter changes" }), "picked");

    expect(screen.getByText("kid")).toBeInTheDocument();
    expect(screen.queryByText("mike")).toBeNull();
  });

  it("counts a person's unchanged row in the run header instead of listing it", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE, KID_ROW, MIKE_UNCHANGED]);
    renderChanges();

    const header = (await screen.findByText(/Run #12/)).closest("tr") as HTMLElement;
    expect(header).toHaveTextContent("1 unchanged");
    expect(screen.queryByText(/Nothing changed on Plex/)).toBeNull();
  });

  it("opens the raw message for any change", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE]);
    renderChanges();
    await screen.findByText("mike");

    await userEvent.click(screen.getByRole("button", { name: /Diff/ }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/"filterMovies"/)).toBeInTheDocument();
    expect(within(dialog).getByText(/run\.privacy_sync/)).toBeInTheDocument();
  });

  it("says how long the record is kept, and where to change it", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE]);
    renderChanges();

    expect(await screen.findByText(/Kept forever\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Change in Settings → System/ })).toHaveAttribute(
      "href",
      "/settings#advanced",
    );
  });

  it("says a shorter retention in months", async () => {
    getSettings.mockResolvedValue({ "events.retention": 6 });
    getEventLog.mockResolvedValue([SHARE_MERGE]);
    renderChanges();

    expect(await screen.findByText(/Kept for 6 months\./)).toBeInTheDocument();
  });

  it("points at the Jobs tab's pills for the jobs that make these writes", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE]);
    renderChanges();

    await screen.findByText("mike");
    expect(screen.getByText("Changes Plex")).toBeInTheDocument();
    expect(screen.getByText("Can delete")).toBeInTheDocument();
  });

  it("explains an empty record", async () => {
    getEventLog.mockResolvedValue([]);
    renderChanges();

    expect(await screen.findByText("Nothing has changed on Plex yet")).toBeInTheDocument();
  });

  it("says when nothing matches the filter, with a way back", async () => {
    getEventLog.mockResolvedValue([SHARE_MERGE]);
    renderChanges();
    await screen.findByText("mike");

    await userEvent.type(screen.getByRole("searchbox", { name: "Filter changes" }), "zzz");
    expect(screen.getByText("No changes match")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByText("mike")).toBeInTheDocument();
  });

  it("offers a retry when the record cannot be read", async () => {
    getEventLog.mockRejectedValueOnce(new ApiError(500, "The database is locked."));
    getEventLog.mockResolvedValue([SHARE_MERGE]);
    renderChanges();

    expect(await screen.findByText(/database is locked/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Try again/ }));
    expect(await screen.findByText("mike")).toBeInTheDocument();
  });

  it("pages back through older changes from the oldest one it holds", async () => {
    const page = Array.from({ length: 200 }, (_, i) =>
      ev(1000 - i, "run.privacy_sync", { ...SHARE_MERGE.message, username: `user${i}` }),
    );
    getEventLog.mockResolvedValueOnce(page).mockResolvedValueOnce([DRY_MERGE]);
    renderChanges();

    await userEvent.click(await screen.findByRole("button", { name: "Load older changes" }));

    await waitFor(() =>
      expect(getEventLog).toHaveBeenLastCalledWith({ plexWrites: true, beforeId: 801, limit: 200 }),
    );
    expect(await screen.findByText("jess")).toBeInTheDocument();
  });
});
