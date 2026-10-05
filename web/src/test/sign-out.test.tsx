import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";

import { NavBody } from "@/components/layout/app-shell";
import type * as ApiModule from "@/lib/api";

const logout = vi.fn<() => Promise<void>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getSession: vi.fn().mockResolvedValue({ authenticated: true, login_required: true, username: "owner" }),
      getVersion: vi.fn().mockResolvedValue({ version: "1.9.3" }),
      getPrivacyStatus: vi.fn().mockResolvedValue(null),
      getSettings: vi.fn().mockResolvedValue({}),
      logout: () => logout(),
    },
  };
});

it("signing out forgets every remembered report", async () => {
  logout.mockResolvedValue(undefined);
  localStorage.setItem("shortlist.report.v1.30", "{}");
  localStorage.setItem("shortlist.report.90", "{}");
  localStorage.setItem("shortlist.other", "keep");
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <NavBody />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  await userEvent.click(await screen.findByRole("button", { name: /sign out/i }));

  await vi.waitFor(() => expect(localStorage.getItem("shortlist.report.v1.30")).toBeNull());
  expect(localStorage.getItem("shortlist.report.90")).toBeNull();
  expect(localStorage.getItem("shortlist.other")).toBe("keep");
});
