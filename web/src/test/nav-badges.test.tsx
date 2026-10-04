import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";

import { NavBody } from "@/components/layout/app-shell";
import type * as ApiModule from "@/lib/api";

const listCollections = vi.fn();
const getUsers = vi.fn();

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
      listCollections: () => listCollections(),
      getUsers: () => getUsers(),
    },
  };
});

function renderNav() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <NavBody />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  listCollections.mockReset();
  getUsers.mockReset();
});

it("shows the Rows count on first render without visiting the Rows page", async () => {
  listCollections.mockResolvedValue([{ id: 1 }, { id: 2 }, { id: 3 }]);

  renderNav();

  await vi.waitFor(() => expect(screen.getByRole("link", { name: /^Rows/ })).toHaveTextContent("Rows3"));
  expect(listCollections).toHaveBeenCalledTimes(1);
});

it("shows no Rows count while the collections request is pending", () => {
  listCollections.mockReturnValue(new Promise(() => {}));

  renderNav();

  expect(screen.getByRole("link", { name: /^Rows/ })).toHaveTextContent(/^Rows$/);
});

it("does not request users on shell mount", async () => {
  listCollections.mockResolvedValue([]);

  renderNav();

  await vi.waitFor(() => expect(listCollections).toHaveBeenCalled());
  expect(getUsers).not.toHaveBeenCalled();
  expect(screen.getByRole("link", { name: /^Users/ })).toHaveTextContent(/^Users$/);
});
