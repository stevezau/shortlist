import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { UserDetailBody } from "@/pages/user-detail";
import type { User } from "@/lib/types";

// The page pulls several panels, each with its own query. None of them is what this file is about —
// it tests which TAB the URL selects — so the api is stubbed wholesale and the panels render empty.
vi.mock("@/lib/api", () => ({
  api: new Proxy({}, { get: () => () => Promise.resolve([]) }),
}));

const USER = {
  id: 1,
  username: "sarah",
  display_name: "Sarah H",
  slug: "sarah",
  user_type: "shared",
  enabled: true,
  prefs: {},
} as unknown as User;

function renderAt(url: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <UserDetailBody user={USER} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UserDetailBody — which tab the URL selects", () => {
  it("honours ?tab=history, which is where the dashboard links land", async () => {
    // The dashboard asserts the href it EMITS; without this nothing asserts the page honours it, so
    // renaming the key or the parse would leave every test green and land people on Rows.
    renderAt("/users/1?tab=history");

    expect(await screen.findByText(/What they did with their picks/i)).toBeTruthy();
  });

  it("defaults to Rows with no tab in the URL", () => {
    renderAt("/users/1");

    expect(screen.getByText(/Their personal rows/i)).toBeTruthy();
    expect(screen.queryByText(/What they did with their picks/i)).toBeNull();
  });

  it("falls back to Rows on a tab it does not recognise", () => {
    // A stale or hand-edited link must not render a blank page.
    renderAt("/users/1?tab=nonsense");

    expect(screen.getByText(/Their personal rows/i)).toBeTruthy();
  });
});
