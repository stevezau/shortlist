import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationBell } from "@/components/layout/notification-bell";
import type { AppNotification } from "@/lib/types";

const { getNotifications, dismissNotification } = vi.hoisted(() => ({
  getNotifications: vi.fn(),
  dismissNotification: vi.fn((_id: string) => Promise.resolve({ ok: true })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getNotifications: () => getNotifications(),
    dismissNotification: (id: string) => dismissNotification(id),
  },
}));

function renderBell() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <NotificationBell />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const UPDATE: AppNotification = {
  id: "update-9.9.9",
  severity: "info",
  title: "Shortlist 9.9.9 is available",
  body: "A newer version has been released.",
  action_url: "https://github.com/stevezau/shortlist/releases/tag/v9.9.9",
  action_label: "View release",
  dismissable: true,
};

const FAILED: AppNotification = {
  id: "run-failed-3",
  severity: "error",
  title: "The last run failed",
  body: "The most recent run ended in an error.",
  action_url: "/runs/3",
  action_label: "See the run",
  dismissable: true,
};

describe("NotificationBell", () => {
  beforeEach(() => {
    getNotifications.mockReset();
    dismissNotification.mockClear();
  });

  it("badges the count and lists the notifications when opened", async () => {
    getNotifications.mockResolvedValue({ notifications: [FAILED, UPDATE] });
    renderBell();
    // The badge shows the count once loaded.
    const bell = await screen.findByRole("button", {
      name: /Notifications \(2\)/,
    });
    await userEvent.click(bell);
    expect(screen.getByText("The last run failed")).toBeTruthy();
    expect(screen.getByText("Shortlist 9.9.9 is available")).toBeTruthy();
    // The error's action is an internal link; the update's is an external release link.
    expect(screen.getByRole("link", { name: "See the run" })).toHaveAttribute(
      "href",
      "/runs/3",
    );
    expect(screen.getByRole("link", { name: "View release" })).toHaveAttribute(
      "target",
      "_blank",
    );
  });

  it("shows an all-caught-up empty state and no badge when there's nothing", async () => {
    getNotifications.mockResolvedValue({ notifications: [] });
    renderBell();
    const bell = await screen.findByRole("button", { name: "Notifications" });
    expect(bell.textContent).not.toMatch(/\d/); // no count badge
    await userEvent.click(bell);
    expect(screen.getByText(/all caught up/i)).toBeTruthy();
  });

  it("dismisses a notification by its id", async () => {
    getNotifications.mockResolvedValue({ notifications: [UPDATE] });
    renderBell();
    await userEvent.click(
      await screen.findByRole("button", { name: /Notifications/ }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() =>
      expect(dismissNotification).toHaveBeenCalledWith("update-9.9.9"),
    );
  });
});
