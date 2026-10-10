import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UserDetailBody } from "@/pages/user-detail";
import type { User, UserRow } from "@/lib/types";

// The page pulls several panels, each with its own query. Only the rows list serves anything (for
// the row-card tests below); every other call is stubbed to an empty list, since the tab tests
// only care which TAB the URL selects.
const { userRows } = vi.hoisted(() => ({ userRows: { current: [] as unknown[] } }));
vi.mock("@/lib/api", () => ({
  api: new Proxy(
    {},
    {
      get: (_target, name) => {
        if (name === "getUserRows") return () => Promise.resolve(userRows.current);
        // A row card asks whether hit rates have matured, which reads into the report's shape.
        if (name === "getReport") return () => Promise.resolve({ first_pick: null, overall: { landing: {} } });
        return () => Promise.resolve([]);
      },
    },
  ),
}));

/** One of the person's rows as the server lists it, with no picks. */
function userRow(patch: Partial<UserRow>): UserRow {
  return {
    collection_id: 5,
    slug: "your-requests",
    name: "📬 {library_name} you asked for",
    library: "Movies",
    section_key: "1",
    media: "movie",
    size: 20,
    recent_count: 8,
    is_default: false,
    muted: false,
    override: { row_size: null, recent_count: null, muted: null },
    picks: [],
    ...patch,
  } as UserRow;
}

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

beforeEach(() => {
  userRows.current = [];
});

describe("UserDetailBody — which tab the URL selects", () => {
  it("identifies blocked titles as a settings section with its controls visible", () => {
    renderAt("/users/1?tab=settings");
    const section = screen.getByRole("region", { name: "Blocked titles" });
    expect(within(section).getByRole("textbox", { name: "Search a title to block" })).toBeVisible();
    expect(within(section).getByRole("button", { name: "Search" })).toBeVisible();
    expect(screen.getByLabelText("Nickname (optional)")).toBeVisible();
    expect(screen.getByLabelText("Manage Plex sharing settings for sarah")).toBeVisible();
  });

  it("honours ?tab=watched, which is where the dashboard links land", async () => {
    // The dashboard asserts the href it EMITS; without this nothing asserts the page honours it, so
    // renaming the key or the parse would leave every test green and land people on Rows.
    renderAt("/users/1?tab=watched");

    expect(
      await screen.findByText(/What they did with their picks/i),
    ).toBeTruthy();
  });

  it("still honours the old ?tab=history links", async () => {
    // The tab is labelled "Watched" and its value said "history" (audit finding, Sep 2026). The
    // value was renamed to match the label — but bookmarks and any link already sent out still say
    // `history`, and silently landing them on Rows would be worse than the mismatch was.
    renderAt("/users/1?tab=history");

    expect(
      await screen.findByText(/What they did with their picks/i),
    ).toBeTruthy();
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

describe("UserDetailBody — how a row card names its row", () => {
  it("fills {library_name} with the card's own library, instead of printing the token and the library both", async () => {
    // The card knows exactly which library it is — it said so in a "— Movies" suffix, right after
    // the literal "{library_name}" it could have filled.
    userRows.current = [userRow({})];
    renderAt("/users/1");

    expect(await screen.findByText("📬 Movies you asked for")).toBeInTheDocument();
    expect(screen.queryByText(/\{library_name\}/)).toBeNull();
    expect(screen.queryByText(/— Movies/)).toBeNull();
  });

  it("keeps the library suffix on a row whose name does not mention its library", async () => {
    userRows.current = [userRow({ name: "✨ Picked for You", collection_id: 6 })];
    renderAt("/users/1");

    expect(await screen.findByText("✨ Picked for You — Movies")).toBeInTheDocument();
  });
});
