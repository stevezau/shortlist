import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppRoutes } from "@/App";
import type * as ApiModule from "@/lib/api";

const { getSession } = vi.hoisted(() => ({
  getSession: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { ...actual.api, getSession },
  };
});

function renderAt(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("assistant browser handoffs", () => {
  beforeEach(() => {
    getSession.mockReset();
    getSession.mockResolvedValue({ authenticated: false, login_required: true });
  });

  it("keeps OAuth consent reachable before owner login", async () => {
    renderAt("/assistant/consent?client_id=public-client&state=opaque");

    expect(await screen.findByText("Sign in to review this connection")).toBeVisible();
    expect(screen.getByText(/assistant never receives your Plex token/i)).toBeVisible();
  });

  it("keeps exact change review reachable before owner login", async () => {
    renderAt("/assistant/changes/change_abc123");

    expect(await screen.findByText("Sign in to review this change")).toBeVisible();
    expect(screen.getByText(/bound to this exact saved plan/i)).toBeVisible();
  });
});
