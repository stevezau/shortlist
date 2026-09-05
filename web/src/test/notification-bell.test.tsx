import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
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

  it("keeps the paragraph break in a multi-paragraph body", async () => {
    // The server writes some bodies as two paragraphs separated by a blank line — the shelf
    // contention one carries the "which Agregarr are you running" advice that way. HTML collapses
    // that to a single space by default, which turns the longest notification we send into a wall
    // of text, so the renderer has to honour the break.
    getNotifications.mockResolvedValue({
      notifications: [
        { ...UPDATE, body: "First paragraph.\n\nSecond paragraph." },
      ],
    });
    renderBell();
    await userEvent.click(
      await screen.findByRole("button", { name: /Notifications/ }),
    );

    const body = screen.getByText(/First paragraph/);
    expect(body).toHaveClass("whitespace-pre-line");
    // The newlines must survive into the DOM, not just be styled — a body the server sent with a
    // break and the UI stored without one would render identically to a collapsed single paragraph.
    expect(body.textContent).toBe("First paragraph.\n\nSecond paragraph.");
  });
});

describe("NotificationBell when the fetch fails", () => {
  it("does not report an error as 'all caught up'", async () => {
    // `data?.notifications ?? []` made a failed fetch and a genuinely quiet server identical, so an
    // unreachable API rendered the most reassuring sentence in the app. This is the bell that
    // surfaces privacy problems and failed runs — silence has to mean silence.
    getNotifications.mockRejectedValue(new Error("network"));
    renderBell();

    await userEvent.click(
      await screen.findByRole("button", { name: /notifications/i }),
    );

    expect(
      await screen.findByText(/couldn.t load notifications/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/all caught up/i)).not.toBeInTheDocument();
  });

  it("offers a retry when it fails", async () => {
    getNotifications.mockRejectedValue(new Error("network"));
    renderBell();

    await userEvent.click(
      await screen.findByRole("button", { name: /notifications/i }),
    );

    expect(
      await screen.findByRole("button", { name: /try again/i }),
    ).toBeInTheDocument();
  });

  it("still says all caught up when the server really is quiet", async () => {
    getNotifications.mockResolvedValue({ notifications: [] });
    renderBell();

    await userEvent.click(
      await screen.findByRole("button", { name: /notifications/i }),
    );

    expect(await screen.findByText(/all caught up/i)).toBeInTheDocument();
  });
});
