import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BackupPanel } from "@/components/jobs/backup-panel";
import type * as ApiModule from "@/lib/api";

const { getBackups, getPendingRestore, restoreBackup, cancelRestore, getSettings, getSchedule } = vi.hoisted(() => ({
  getBackups: vi.fn(),
  getPendingRestore: vi.fn(),
  restoreBackup: vi.fn(),
  cancelRestore: vi.fn(),
  getSettings: vi.fn(),
  getSchedule: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { ...actual.api, getBackups, getPendingRestore, restoreBackup, cancelRestore, getSettings, getSchedule },
  };
});

const BACKUP = { name: "shortlist_20260913_030000_scheduled.db", size_bytes: 2048, created_at: "2026-09-13T03:00:00Z" };

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <BackupPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("BackupPanel — a restore waiting for a restart", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getBackups.mockResolvedValue([BACKUP]);
    getSettings.mockResolvedValue({});
    getSchedule.mockResolvedValue({ jobs: [], rows: [] });
  });

  it("says a restore is waiting, which backup, and that a restart applies it", async () => {
    getPendingRestore.mockResolvedValue({
      pending: { backup: BACKUP.name, requested_at: "2026-09-14T00:00:00+00:00" },
    });

    renderPanel();

    const waiting = await screen.findByRole("status", { name: /restore waiting/i });
    expect(waiting).toHaveTextContent("20260913_030000_scheduled");
    expect(waiting).toHaveTextContent(/restart the container/i);
    // Dropped a day after it was asked for, and the deadline is a date, not "a day of 3 minutes ago".
    expect(waiting).toHaveTextContent(new Date("2026-09-15T00:00:00+00:00").toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }));
    expect(waiting).toHaveTextContent(/who could see which rows/i);
    expect(screen.getByRole("button", { name: /cancel the restore/i })).toBeInTheDocument();
  });

  it("cancels it, and the notice goes", async () => {
    getPendingRestore
      .mockResolvedValueOnce({ pending: { backup: BACKUP.name, requested_at: "2026-09-14T00:00:00+00:00" } })
      .mockResolvedValue({ pending: null });
    cancelRestore.mockResolvedValue({ pending: null });

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /cancel the restore/i }));

    expect(cancelRestore).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("status", { name: /restore waiting/i })).not.toBeInTheDocument());
  });

  it("shows no notice when nothing is waiting", async () => {
    getPendingRestore.mockResolvedValue({ pending: null });

    renderPanel();

    await screen.findByText(BACKUP.name.replace("shortlist_", "").replace(".db", ""));
    expect(screen.queryByRole("status", { name: /restore waiting/i })).not.toBeInTheDocument();
  });

  it("asks again after a restore is confirmed, so the notice appears without a reload", async () => {
    getPendingRestore
      .mockResolvedValueOnce({ pending: null })
      .mockResolvedValue({ pending: { backup: BACKUP.name, requested_at: "2026-09-14T00:00:00+00:00" } });
    restoreBackup.mockResolvedValue({ restored: BACKUP.name, message: "Ready to restore.", privacy_note: "Rows note." });

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(await screen.findByRole("status", { name: /restore waiting/i })).toBeInTheDocument();
  });

  it("says nothing is ready after the waiting restore is cancelled", async () => {
    getPendingRestore
      .mockResolvedValueOnce({ pending: null })
      .mockResolvedValueOnce({ pending: { backup: BACKUP.name, requested_at: "2026-09-14T00:00:00+00:00" } })
      .mockResolvedValue({ pending: null });
    restoreBackup.mockResolvedValue({ restored: BACKUP.name, message: "Ready to restore.", privacy_note: "Rows note." });
    cancelRestore.mockResolvedValue({ pending: null });

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await userEvent.click(await screen.findByRole("button", { name: /cancel the restore/i }));

    await waitFor(() => expect(screen.queryByRole("status", { name: /restore waiting/i })).not.toBeInTheDocument());
    expect(screen.queryByText(/Ready to restore/)).not.toBeInTheDocument();
    expect(screen.queryByText("Rows note.")).not.toBeInTheDocument();
  });
});

describe("BackupPanel — its schedule", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getBackups.mockResolvedValue([BACKUP]);
    getPendingRestore.mockResolvedValue({ pending: null });
    getSettings.mockResolvedValue({});
  });

  it("labels the blank schedule chip with the built-in backup time", async () => {
    // The backup panel draws its own CronPicker, so the Built-in label that reached the generic job
    // panel missed it and it still read "Daily" (seen live on 2026-09-28).
    getSchedule.mockResolvedValue({
      jobs: [
        {
          type: "job",
          kind: "backup.take",
          label: "Back up the database",
          description: "",
          setting: "backup.cron",
          cron: "",
          using_default: true,
          default_cron: "0 3 * * *",
          optional: false,
          writes_plex: false,
          next_run: "2026-09-29T03:00:00Z",
        },
      ],
      rows: [],
    });
    renderPanel();

    expect(await screen.findByRole("button", { name: "Built-in (03:00)" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Daily" })).toBeNull();
  });
});
