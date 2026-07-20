import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AdvancedSection } from "@/components/settings/advanced-section";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", () => ({
  api: { putSettings: (values: Settings) => putSettings(values) },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AdvancedSection settings={settings} />
    </QueryClientProvider>,
  );
}

describe("AdvancedSection", () => {
  beforeEach(() => putSettings.mockClear());

  it("marks the saved level active and defaults an unset level to DEBUG", () => {
    renderSection({ "log.level": "TRACE" });
    expect(
      screen
        .getByRole("button", { name: "TRACE" })
        .getAttribute("aria-pressed"),
    ).toBe("true");

    renderSection({});
    // Two DEBUG buttons now on screen (one per render); the second render's is unset → DEBUG active.
    const debug = screen.getAllByRole("button", { name: "DEBUG" });
    expect(debug.some((b) => b.getAttribute("aria-pressed") === "true")).toBe(
      true,
    );
  });

  it("auto-saves the chosen level (no Save button)", async () => {
    renderSection({ "log.level": "INFO" });
    await userEvent.click(screen.getByRole("button", { name: "TRACE" }));
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith({ "log.level": "TRACE" }),
    );
  });

  it("auto-saves run concurrency as a number and defaults to 4", async () => {
    renderSection({});
    expect(
      screen.getByRole("button", { name: "4" }).getAttribute("aria-pressed"),
    ).toBe("true");
    await userEvent.click(screen.getByRole("button", { name: "8" }));
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith({ "run.concurrency": 8 }),
    );
  });

  it("auto-saves the Plex request timeout as a number and defaults to 45s", async () => {
    renderSection({});
    expect(
      screen.getByRole("button", { name: "45s" }).getAttribute("aria-pressed"),
    ).toBe("true");
    await userEvent.click(screen.getByRole("button", { name: "60s" }));
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith({ "plex.timeout_s": 60 }),
    );
  });
});
