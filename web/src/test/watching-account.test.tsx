import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { Collection, CollectionBody, User } from "@/lib/types";
import {
  WatchingAccountPage,
  rowsOnTheSharedShelf,
} from "@/pages/watching-account";

const { getUsers, listCollections, updateCollection, dismissNotification } =
  vi.hoisted(() => ({
    getUsers: vi.fn(),
    listCollections: vi.fn(),
    updateCollection: vi.fn((_id: number, _body: CollectionBody) =>
      Promise.resolve({} as Collection),
    ),
    dismissNotification: vi.fn((_id: string) => Promise.resolve({ ok: true })),
  }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      listCollections: () => listCollections(),
      updateCollection: (id: number, body: CollectionBody) =>
        updateCollection(id, body),
      dismissNotification: (id: string) => dismissNotification(id),
    },
  };
});

function user(over: Partial<User>): User {
  return {
    id: 1,
    username: "u",
    slug: "u",
    user_type: "shared",
    restricted: false,
    enabled: true,
    cold_start: false,
    history_depth: 0,
    last_run_at: null,
    request_tag: "",
    hit_rate: null,
    nickname: "",
    friendly_name: "",
    display_name: "",
    avatar_url: "",
    plex_account_id: 0,
    restriction_profile: "",
    unhidden_rows: 0,
    departed: false,
    preview_titles: [],
    prefs: {},
    ...over,
  };
}

function row(over: Partial<Collection>): Collection {
  return {
    id: 1,
    name: "Picked for You",
    build: "per_person",
    enabled: true,
    placement: "both",
    placement_friends: "both",
    ...over,
  } as Collection;
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WatchingAccountPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getUsers.mockResolvedValue([
    user({ id: 1, user_type: "owner", username: "owner" }),
    user({ id: 2, username: "sarah" }),
    user({ id: 3, username: "mike" }),
  ]);
  listCollections.mockResolvedValue([row({})]);
});

describe("rowsOnTheSharedShelf", () => {
  it("counts only enabled per-person rows that claim the friends' library shelf", () => {
    const rows = [
      row({ id: 1, placement_friends: "both" }),
      row({ id: 2, placement_friends: "library" }),
      row({ id: 3, placement_friends: "home" }), // Home is already split by audience
      row({ id: 4, placement_friends: "off" }),
      row({ id: 5, build: "shared" }), // one collection everyone sees on purpose
      row({ id: 6, enabled: false }),
    ];

    expect(rowsOnTheSharedShelf(rows).map((r) => r.id)).toEqual([1, 2]);
  });
});

describe("WatchingAccountPage", () => {
  it("names who else is on the server without over-claiming a row count", async () => {
    // NOT "all 3 rows". The true count is rows x their resolved audience, which neither `others`
    // nor `others + 1` gets right once a row is audience="subset" or muted per-user — and a
    // confident wrong number is worse than a qualitative one.
    renderPage();

    // The h1 also says "everyone's rows", so scope to the explanatory copy.
    expect(await screen.findByText(/2 other people/i)).toBeInTheDocument();
    expect(screen.queryByText(/all 3 rows/i)).not.toBeInTheDocument();
  });

  it("says the shelf only clears on each row's next run", async () => {
    // `placement_friends` is next-run-only (jobs-and-runs-design.md §12) — a green tick with no
    // timing reads as "your shelf is clear now", which it is not.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /do this for me/i }),
    );

    expect(
      await screen.findByText(/placement is applied by a row.s next run/i),
    ).toBeInTheDocument();
  });

  it("says how many rows were saved before a failure, not that nothing changed", async () => {
    // The PATCHes are sequential, so "it failed" and "nothing changed" are different claims.
    listCollections.mockResolvedValue([
      row({ id: 1, name: "A" }),
      row({ id: 2, name: "B" }),
    ]);
    updateCollection
      .mockResolvedValueOnce({} as Collection)
      .mockRejectedValueOnce(new Error("boom"));
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /do this for me/i }),
    );

    expect(await screen.findByText(/Changed 1 of 2/i)).toBeInTheDocument();
  });

  it("takes rows off the friends' shelf while leaving Home placement alone", async () => {
    listCollections.mockResolvedValue([
      row({ id: 1, name: "Picked for You", placement_friends: "both" }),
      row({ id: 2, name: "Hidden Gems", placement_friends: "library" }),
    ]);
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /do this for me/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(2));
    // The contract this page is responsible for: only `placement_friends` moves, and it drops the
    // LIBRARY half while keeping whatever Home setting the row already had. "both" keeps Home;
    // "library" had none, so it goes fully off.
    expect(updateCollection).toHaveBeenCalledWith(1, {
      name: "Picked for You",
      placement_friends: "home",
    });
    expect(updateCollection).toHaveBeenCalledWith(2, {
      name: "Hidden Gems",
      placement_friends: "off",
    });
  });

  it("offers nothing to do when no row is on the friends' shelf", async () => {
    listCollections.mockResolvedValue([row({ placement_friends: "home" })]);
    renderPage();

    expect(
      await screen.findByRole("button", { name: /do this for me/i }),
    ).toBeDisabled();
    expect(screen.getByText(/already done/i)).toBeInTheDocument();
  });

  it("scrolls the transfer step into view when it appears", async () => {
    // It mounts below the fold, so without this "Set it up" reads as having done nothing.
    const scrollIntoView = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView;
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /set it up/i }),
    );

    expect(
      await screen.findByRole("heading", {
        name: /set up the watching account/i,
      }),
    ).toBeInTheDocument();
    expect(scrollIntoView).toHaveBeenCalled();
  });

  it("retires BOTH surfaces — this one is a decision about the fact, not about an alert", async () => {
    // The bell and the inline note dismiss independently by design. "Leave it — I don't mind seeing
    // them" is the one place that means "stop telling me at all", so it has to cover both.
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /^dismiss$/i }),
    );

    await waitFor(() =>
      expect(dismissNotification).toHaveBeenCalledWith("owner-sees-all-rows"),
    );
    await waitFor(() =>
      expect(dismissNotification).toHaveBeenCalledWith(
        "owner-sees-all-rows-note",
      ),
    );
  });
});
