import type { UseQueryResult } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HealthStrip } from "@/components/dashboard/health-strip";
import { ApiError } from "@/lib/api";
import type * as QueriesModule from "@/lib/queries";
import type { AppNotification, NotificationsPage } from "@/lib/types";

// The hook, not the fetch. `HealthStrip` is a rendering of whatever the shared notifications query
// currently holds, and the four states are the branches worth pinning; the hook's own wiring to
// `api.getNotifications` is already covered by notification-bell.test.tsx.
const { useNotifications } = vi.hoisted(() => ({
  useNotifications: vi.fn<() => UseQueryResult<NotificationsPage>>(),
}));

vi.mock("@/lib/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof QueriesModule>()),
  useNotifications: () => useNotifications(),
}));

function queryResult(
  partial: Partial<UseQueryResult<NotificationsPage>>,
): UseQueryResult<NotificationsPage> {
  return {
    isPending: false,
    isError: false,
    refetch: vi.fn(),
    ...partial,
  } as unknown as UseQueryResult<NotificationsPage>;
}

function loaded(notifications: AppNotification[]) {
  return queryResult({ data: { notifications, dismissed: [] } });
}

function alert(patch: Partial<AppNotification> = {}): AppNotification {
  return {
    id: "unhideable-rows-9",
    severity: "error",
    title: "kid can see other people's rows",
    body: "Plex won't let Shortlist hide anything from a profiled account.",
    action_url: "/users",
    action_label: "Open Users",
    dismissable: false,
    ...patch,
  };
}

function renderStrip() {
  return render(
    <MemoryRouter>
      <HealthStrip />
    </MemoryRouter>,
  );
}

describe("HealthStrip", () => {
  beforeEach(() => useNotifications.mockReset());

  it("renders nothing at all when every area is quiet", () => {
    // Six permanently-green chips were an all-clear nobody asked for, on every page load — the same
    // thing notifications-design.md refuses for the webhook ("no 'all clear' ping nobody asked
    // for"). They were also weaker than they looked: most of these alerts are dismissable, so a
    // dismissal turns a chip green while the fact behind it stands.
    useNotifications.mockReturnValue(loaded([]));
    const { container } = renderStrip();

    expect(screen.queryByRole("list")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });

  it("shows only the area that needs attention, not the five that do not", () => {
    useNotifications.mockReturnValue(loaded([alert()]));
    renderStrip();

    const strip = screen.getByRole("list", { name: /Needs attention/i });
    expect(within(strip).getAllByRole("link")).toHaveLength(1);
    expect(
      within(strip).getByRole("link", {
        name: /Privacy: kid can see other people's rows/i,
      }),
    ).toHaveAttribute("href", "/users");
    expect(screen.queryByText(/nothing outstanding/i)).toBeNull();
  });

  it("shows nothing while the alerts are still loading", () => {
    // Never a bare all-clear before the answer is known — the trap the bell was fixed for. With the
    // strip silent when healthy, a placeholder would flash six grey pills and then collapse on
    // every single dashboard load.
    useNotifications.mockReturnValue(queryResult({ isPending: true }));
    const { container } = renderStrip();

    expect(container.querySelectorAll("li")).toHaveLength(0);
    expect(screen.queryByText(/nothing outstanding/i)).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("says the check failed, with a retry, rather than six reassuring chips", async () => {
    const refetch = vi.fn();
    useNotifications.mockReturnValue(
      queryResult({
        isError: true,
        error: new ApiError(502, "Upstream is down"),
        refetch,
      }),
    );
    renderStrip();

    expect(screen.getByRole("alert")).toHaveTextContent("Upstream is down");
    expect(screen.queryByRole("link")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(refetch).toHaveBeenCalledOnce();
  });
});
