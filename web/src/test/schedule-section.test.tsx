import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ScheduleSection } from "@/components/settings/schedule-section";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return {
    ApiError,
    apiErrorMessage: (error: unknown, fallback: string) =>
      error instanceof ApiError ? error.message : fallback,
    api: { putSettings: (values: Settings) => putSettings(values) },
  };
});

function renderSection(settings: Settings = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ScheduleSection settings={settings} />
    </QueryClientProvider>,
  );
}

describe("ScheduleSection", () => {
  beforeEach(() => putSettings.mockClear());

  it("saves the preset time as a nightly cron", async () => {
    renderSection({ "schedule.cron": "30 3 * * *" });
    // Nightly is the default mode; the run-time picker is shown.
    const time = screen.getByLabelText(/Run at/i);
    await userEvent.clear(time);
    await userEvent.type(time, "04:15");
    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toHaveProperty(
        "schedule.cron",
        "15 4 * * *",
      ),
    );
  });

  it("opens in Custom mode for a cron the presets can't round-trip", async () => {
    renderSection({ "schedule.cron": "0 */6 * * *" });
    // A step-hour cron would be silently flattened to nightly — so it must load as-is in Custom mode.
    const cron = screen.getByLabelText(/Cron expression/i);
    expect(cron).toHaveValue("0 */6 * * *");
    expect(screen.queryByLabelText(/Run at/i)).toBeNull();
  });

  it("keeps a non-Sunday weekday cron in Custom mode instead of relabelling it Sunday", async () => {
    // Regression: the presets only ever emit Sunday, so a Monday cron loaded as a "weekly preset"
    // would display "every Sunday" and get overwritten to dow 0 on the next save.
    renderSection({ "schedule.cron": "0 4 * * 1" });
    expect(screen.getByLabelText(/Cron expression/i)).toHaveValue("0 4 * * 1");
    expect(screen.queryByText(/every Sunday/i)).toBeNull();
    expect(screen.queryByLabelText(/Run at/i)).toBeNull();
  });

  it("saves a raw cron typed in Custom mode, and never one with too few fields", async () => {
    renderSection({ "schedule.cron": "30 3 * * *" });
    await userEvent.click(screen.getByRole("button", { name: /Custom/i }));

    const cron = screen.getByLabelText(/Cron expression/i);
    await userEvent.clear(cron);
    await userEvent.type(cron, "0 4 * * 1"); // Mondays at 4am
    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toHaveProperty(
        "schedule.cron",
        "0 4 * * 1",
      ),
    );

    // An incomplete expression is never POSTed; the field says why instead.
    putSettings.mockClear();
    await userEvent.clear(cron);
    await userEvent.type(cron, "0 4 *");
    expect(
      await screen.findByText(/needs five space-separated fields/i),
    ).toBeTruthy();
    expect(putSettings).not.toHaveBeenCalled();
  });
});
