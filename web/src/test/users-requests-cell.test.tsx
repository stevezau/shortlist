import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { RowSources, User } from "@/lib/types";
import { UsersPage } from "@/pages/users";
import { makeUser } from "@/test/user-fixtures";

const { getUsers, getRequestRowSources } = vi.hoisted(() => ({
  getUsers: vi.fn(),
  getRequestRowSources: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      getRequestRowSources: (pattern: string) => getRequestRowSources(pattern),
      getPrivacyStatus: () => Promise.reject(new Error("not under test")),
      listCollections: () => Promise.resolve([]),
    },
  };
});

function sources(people: { user_id: number; linked: boolean; ready: number }[]): RowSources {
  return {
    complete: true,
    problems: [],
    overseerr: "connected",
    radarr: "off",
    sonarr: "off",
    seerr_requests: 0,
    seerr_requesters: 0,
    seerr_linked: 0,
    servers: [],
    tagged_movies: 0,
    tagged_shows: 0,
    tags: [],
    people: people.map((p) => ({ ...p, display_name: "" })),
  };
}

const SARAH: User = makeUser({ id: 4, username: "sarah", slug: "sarah" });
const KID: User = makeUser({ id: 5, username: "kid", slug: "kid", user_type: "managed" });

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UsersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UsersPage — the Overseerr link in the Requests column", () => {
  beforeEach(() => {
    getUsers.mockReset();
    getRequestRowSources.mockReset();
  });

  it("calls an automatic link Linked and keeps the ready note", async () => {
    getUsers.mockResolvedValue([SARAH]);
    getRequestRowSources.mockResolvedValue(sources([{ user_id: 4, linked: true, ready: 2 }]));

    renderPage();

    const pill = await screen.findByText("Linked");
    expect(pill).toHaveAttribute("title", "Requests linked to their Overseerr account");
    expect(pill.closest("td")).toHaveTextContent("2 ready");
  });

  it("says when the link was chosen by hand", async () => {
    getUsers.mockResolvedValue([{ ...SARAH, seerr_user_id: 12 }]);
    getRequestRowSources.mockResolvedValue(sources([{ user_id: 4, linked: true, ready: 0 }]));

    renderPage();

    expect(await screen.findByText("Linked")).toHaveAttribute(
      "title",
      "Requests linked to the Overseerr account you chose by hand",
    );
  });

  it("sends a shared person who is not linked to their settings to choose an account", async () => {
    getUsers.mockResolvedValue([SARAH]);
    getRequestRowSources.mockResolvedValue(sources([{ user_id: 4, linked: false, ready: 3 }]));

    renderPage();

    const pill = await screen.findByText("Not linked");
    expect(pill.closest("a")).toHaveAttribute("href", "/users/4?tab=settings");
    expect(pill.closest("td")).toHaveTextContent("Choose their Overseerr account");
    expect(pill.closest("td")).toHaveTextContent("3 ready");
  });

  it("tells a managed profile to choose a local Overseerr account", async () => {
    getUsers.mockResolvedValue([KID]);
    getRequestRowSources.mockResolvedValue(sources([{ user_id: 5, linked: false, ready: 0 }]));

    renderPage();

    const pill = await screen.findByText("Not linked");
    expect(pill.closest("a")).toHaveAttribute("href", "/users/5?tab=settings");
    expect(pill.closest("td")).toHaveTextContent("Choose their local Overseerr account");
  });
});
