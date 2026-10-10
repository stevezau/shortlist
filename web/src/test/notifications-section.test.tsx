import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationsSection } from "@/components/settings/notifications-section";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, f: string) => f,
  api: { putSettings },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <NotificationsSection settings={settings} />
    </QueryClientProvider>,
  );
}

const on = { "notify.webhook.enabled": true, "notify.webhook.url": "•••••" };

describe("NotificationsSection", () => {
  beforeEach(() => {
    putSettings.mockClear();
  });

  it("saves the switch", async () => {
    renderSection({ "notify.webhook.url": "•••••" });
    fireEvent.click(
      screen.getByRole("switch", { name: /Send alerts to a webhook/i }),
    );
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith({
        "notify.webhook.enabled": true,
      }),
    );
  });

  it("puts the switch back when the save fails", async () => {
    // A switch stuck on "on" over a server that still says off is worse than a switch that refuses:
    // the owner walks away believing they'll be told when a run fails.
    putSettings.mockRejectedValueOnce(new Error("nope"));
    renderSection({ "notify.webhook.url": "•••••" });
    const toggle = screen.getByRole("switch", {
      name: /Send alerts to a webhook/i,
    });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/Couldn’t save/i),
    );
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("points at the webhook row above it when no address is saved", () => {
    renderSection({ "notify.webhook.enabled": true });
    const link = screen.getByRole("link", { name: /Set up the webhook above/i });
    expect(link.getAttribute("href")).toBe("#connection-notify");
  });

  it("greys out the switch until there is an address, but the events can still be chosen ahead", () => {
    // The stored values stay as they are (enabled, events); only what the owner sees is honest.
    renderSection({ "notify.webhook.enabled": true, "notify.webhook.events": ["run.failed"] });
    const toggle = screen.getByRole("switch", { name: /Send alerts to a webhook/i });
    expect(toggle).toBeDisabled();
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    const tick = screen.getByRole("checkbox", { name: /A run failed/i });
    expect(tick).toBeEnabled();
    expect(tick).toBeChecked();
    expect(putSettings).not.toHaveBeenCalled();
  });

  it("does not ask for an address it already has", () => {
    renderSection(on);
    expect(screen.queryByRole("link", { name: /Set up the webhook above/i })).toBeNull();
  });

  it("no longer carries the address or the header itself", () => {
    renderSection(on);
    expect(screen.queryByLabelText(/Webhook address/i)).toBeNull();
    expect(screen.queryByLabelText(/Header/i)).toBeNull();
  });

  describe("what to send", () => {
    it("ticks the events the server says are switched on", () => {
      renderSection({
        ...on,
        "notify.webhook.events": ["run.failed", "privacy.exposure"],
      });
      expect(
        screen.getByRole("checkbox", { name: /A run failed/i }),
      ).toHaveProperty("checked", true);
      expect(
        screen.getByRole("checkbox", { name: /Someone can see a row/i }),
      ).toHaveProperty("checked", true);
      expect(
        screen.getByRole("checkbox", { name: /A run started/i }),
      ).toHaveProperty("checked", false);
    });

    it("stays hidden until notifications are on", () => {
      renderSection({ "notify.webhook.events": ["run.failed"] });
      expect(screen.queryByRole("checkbox", { name: /A run failed/i })).toBeNull();
    });

    it("saves the whole list when one event is ticked", async () => {
      renderSection({ ...on, "notify.webhook.events": ["run.failed"] });
      fireEvent.click(screen.getByRole("checkbox", { name: /A job failed/i }));
      await waitFor(() =>
        expect(putSettings).toHaveBeenCalledWith({
          "notify.webhook.events": ["run.failed", "job.failed"],
        }),
      );
    });

    it("puts the tick back when the save fails", async () => {
      putSettings.mockRejectedValueOnce(new Error("nope"));
      renderSection({ ...on, "notify.webhook.events": ["run.failed"] });
      const box = screen.getByRole("checkbox", { name: /A run failed/i });
      fireEvent.click(box);
      await waitFor(() =>
        expect(screen.getByRole("alert").textContent).toMatch(/Couldn’t save/i),
      );
      expect(box).toHaveProperty("checked", true);
    });

    it("says routine jobs only speak up when there is news", () => {
      renderSection(on);
      expect(screen.getByText(/privacy sync and playback credits/i)).toBeTruthy();
    });
  });
});
