/**
 * Old addresses still land on the page that replaced them.
 *
 * Sharing became Privacy, and Jobs + Logs became tabs of Activity. The old paths are in bookmarks, in
 * the docs, and baked into the `action_url` of notifications already stored in people's databases
 * (`notifications.py` writes "/sharing", "/jobs" and "/logs"), so a 404 there would read as the
 * feature being gone.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { AppRoutes } from "@/App";
import type * as ApiModule from "@/lib/api";

// The session never answers, so the auth gate sits on its skeleton and nothing else is fetched: the
// only thing under test is where the router ends up.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { ...actual.api, getSession: vi.fn(() => new Promise(() => {})) },
  };
});

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname + location.search}</output>;
}

function landAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("old addresses", () => {
  it.each([
    ["/sharing", "/privacy"],
    ["/jobs", "/activity?tab=jobs"],
    ["/logs", "/activity?tab=log"],
    ["/schedule", "/activity?tab=jobs"],
    ["/tools", "/activity?tab=jobs"],
  ])("%s lands on %s", async (from, to) => {
    landAt(from);

    expect(await screen.findByTestId("where")).toHaveTextContent(to);
    expect(screen.getByTestId("where").textContent).toBe(to);
  });

  it("keeps the Jobs page's own failed-runs link working", async () => {
    // The Jobs page's "N failed" badge wrote `?tab=activity&filter=failed`; the same view is now
    // the Jobs tab's Activity view, and the filter must survive the move.
    landAt("/jobs?tab=activity&filter=failed");

    expect(await screen.findByTestId("where")).toHaveTextContent("/activity");
    const search = new URLSearchParams(screen.getByTestId("where").textContent?.split("?")[1]);
    expect(search.get("tab")).toBe("jobs");
    expect(search.get("view")).toBe("activity");
    expect(search.get("filter")).toBe("failed");
  });
});
