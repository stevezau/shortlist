import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { ScheduleResponse } from "@/lib/types";
import { ScheduleTimeline } from "@/components/jobs/schedule-timeline";

const { getSchedule, putSettings } = vi.hoisted(() => ({
  getSchedule: vi.fn(),
  putSettings: vi.fn((_settings: Record<string, unknown>) =>
    Promise.resolve({}),
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getSchedule: () => getSchedule(),
      putSettings: (settings: Record<string, unknown>) =>
        putSettings(settings),
    },
  };
});

const SCHEDULE: ScheduleResponse = {
  jobs: [
    {
      type: "job",
      kind: "backup.take",
      label: "Back up the database",
      description: "Copy the database to /config/backups.",
      setting: "backup.cron",
      cron: "0 3 * * *",
      optional: false,
      writes_plex: false,
      next_run: "2026-07-30T03:00:00Z",
    },
    {
      type: "job",
      kind: "privacy.sync",
      label: "Privacy sync",
      description: "Merge every account's share filter.",
      setting: "privacy.sync_cron",
      cron: "15 5 * * *",
      optional: false,
      writes_plex: true,
      next_run: "2026-07-30T05:15:00Z",
    },
    {
      type: "job",
      kind: "sync.check",
      label: "Sync check",
      description: "Fix anything that drifted.",
      setting: "sync.check_cron",
      cron: "",
      optional: true,
      writes_plex: true,
      next_run: null,
    },
  ],
  rows: [
    {
      type: "rows",
      cron: "30 3 * * *",
      next_run: "2026-07-30T03:30:00Z",
      rows: [
        { id: 1, slug: "picked", name: "Picked for You" },
        { id: 2, slug: "faves", name: "My Faves" },
      ],
    },
  ],
};

function renderSchedule() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ScheduleTimeline />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ScheduleTimeline", () => {
  beforeEach(() => {
    getSchedule.mockReset();
    putSettings.mockClear();
    getSchedule.mockResolvedValue(SCHEDULE);
  });

  it("lists rows and jobs together, ordered by when they next fire", async () => {
    renderSchedule();

    await screen.findByText("Back up the database");
    const labels = screen
      .getAllByText(/Back up the database|Build rows|Privacy sync/)
      .map((el) => el.textContent);
    // 03:00 backup → 03:30 rows → 05:15 privacy sync.
    expect(labels).toEqual([
      "Back up the database",
      "Build rows",
      "Privacy sync",
    ]);
  });

  it("names the rows that share a cron, since one trigger builds all of them", async () => {
    renderSchedule();

    expect(await screen.findByText("Picked for You")).toBeInTheDocument();
    expect(screen.getByText("My Faves")).toBeInTheDocument();
  });

  it("flags the entries that write to Plex", async () => {
    renderSchedule();

    await screen.findByText("Privacy sync");
    // Only privacy sync and the drift check write; the backup is local files.
    expect(screen.getAllByText("writes to Plex")).toHaveLength(2);
  });

  it("separates what is not scheduled, and offers to add one", async () => {
    renderSchedule();

    expect(await screen.findByText("Not scheduled")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Add a schedule/i }),
    ).toBeInTheDocument();
  });

  it("saves a changed cron to that entry's own settings key", async () => {
    renderSchedule();

    await screen.findByText("Privacy sync");
    const privacyButton = screen.getAllByRole("button", { name: /Change/i })[1];
    expect(privacyButton).toBeDefined();
    await userEvent.click(privacyButton!);

    const input = screen.getByRole("textbox");
    await userEvent.clear(input);
    await userEvent.type(input, "0 6 * * *");
    await userEvent.tab();

    expect(putSettings).toHaveBeenCalledWith({
      "privacy.sync_cron": "0 6 * * *",
    });
  });

  it("sends row schedules to the row editor rather than editing them here", async () => {
    // One owner per setting: a cron must never be validated two different ways.
    renderSchedule();

    await screen.findByText("Build rows");
    expect(screen.getByRole("link", { name: /Edit rows/i })).toHaveAttribute(
      "href",
      "/rows",
    );
  });
});
