import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { Collection } from "@/lib/types";
import { fullCollection } from "@/test/collection-builders";
import { RowsPage } from "@/pages/rows";

const { getUsers, listCollections } = vi.hoisted(() => ({
  getUsers: vi.fn(),
  listCollections: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      listCollections: () => listCollections(),
      getSettings: () => Promise.resolve({}),
      getLibraries: () => Promise.resolve([]),
    },
  };
});

const SUBSET_ROW: Collection = fullCollection({ audience: "subset", audience_user_ids: [4] });

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RowsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowsPage", () => {
  beforeEach(() => {
    getUsers.mockReset();
    listCollections.mockReset();
  });

  it("never says a row reaches 'No one yet' just because the user list failed to load", async () => {
    getUsers.mockRejectedValue(new ApiError(500, "Couldn’t load your users."));
    listCollections.mockResolvedValue([SUBSET_ROW]);
    renderPage();

    // `usersQuery.data ?? []` used to swallow the failure and report a real audience as "No one yet",
    // and would have offered an empty audience list in the editor.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Couldn’t load your users/i,
    );
    expect(screen.queryByText(/No one yet/i)).toBeNull();
    expect(screen.getByRole("button", { name: /Add a row/i })).toBeDisabled();
  });

  it("names the audience once the users are known", async () => {
    getUsers.mockResolvedValue([
      {
        id: 4,
        username: "sarah",
        slug: "sarah",
        user_type: "shared",
        restricted: false,
        enabled: true,
        cold_start: false,
        history_depth: 10,
        last_run_at: null,
        request_tag: "",
        requested_by_tag: "",
        picks_watched_30d: null,
        last_pick_watched_at: null,
      },
    ]);
    listCollections.mockResolvedValue([SUBSET_ROW]);
    renderPage();

    expect(await screen.findByText(/sarah · 15 titles/i)).toBeTruthy();
  });

  // A row name is a TEMPLATE, and the card marks each `{placeholder}` as a grey chip rather than
  // printing braces — but nothing said what a chip was, so "✨ [library name] Picked for You" read
  // as a stray tag stuck on the row (audit finding, Sep 2026). The row editor already answers this
  // with a worked example; this is that example, once, under the list.
  it("explains the placeholder chip when a row on screen has one", async () => {
    getUsers.mockResolvedValue([]);
    listCollections.mockResolvedValue([
      { ...SUBSET_ROW, name: "✨ {library_name} Picked for You" },
    ]);
    renderPage();

    expect(
      await screen.findByText(/✨ Movies Picked for You/),
    ).toBeInTheDocument();
  });

  it("explains the chip in plain sentences, not a run-on with a dangling fragment", async () => {
    // It read "…reads ✨ Movies Picked for You on Plex. An example: the real library, person or recent
    // watch fills in." — the owner could not tell whether that was one sentence or two.
    getUsers.mockResolvedValue([]);
    listCollections.mockResolvedValue([
      { ...SUBSET_ROW, name: "✨ {library_name} Picked for You" },
    ]);
    renderPage();

    const legend = (await screen.findByText(/Movies Picked for You/)).closest("p");
    expect(legend?.textContent).toBe(
      "Grey chips like library name are placeholders, filled in when Shortlist builds the row. " +
        "For example, ✨ library name Picked for You shows on Plex as ✨ Movies Picked for You. " +
        "A person’s name or a recent watch fills in the same way.",
    );
  });

  it("says nothing about chips when no row has one", async () => {
    // An explainer for something not on screen is noise on the page it explains.
    getUsers.mockResolvedValue([]);
    listCollections.mockResolvedValue([SUBSET_ROW]);
    renderPage();

    expect(await screen.findByText("Hidden Gems")).toBeInTheDocument();
    expect(screen.queryByText(/✨ Movies Picked for You/)).toBeNull();
  });
});

describe("RowsPage — the day-schedule badge", () => {
  beforeEach(() => {
    getUsers.mockReset();
    listCollections.mockReset();
    getUsers.mockResolvedValue([]);
  });

  it("says nothing for a row that appears every day", async () => {
    // The ordinary row must be untouched: a badge on every row would make the schedule look like
    // something every row has.
    listCollections.mockResolvedValue([
      { ...SUBSET_ROW, show_days: [], shown_today: true },
    ]);
    renderPage();

    expect(await screen.findByText("Hidden Gems")).toBeInTheDocument();
    expect(screen.queryByText(/today/i)).toBeNull();
  });

  it("says Showing today for a scheduled row that is on", async () => {
    listCollections.mockResolvedValue([
      { ...SUBSET_ROW, show_days: [1, 3, 5], shown_today: true },
    ]);
    renderPage();

    expect(await screen.findByText("Showing today")).toBeInTheDocument();
  });

  it("names the season a seasonal row is showing, and until when", async () => {
    listCollections.mockResolvedValue([
      {
        ...SUBSET_ROW,
        show_days: [],
        shown_today: true,
        seasons: ["halloween", "christmas"],
        season_status: {
          showing: { slug: "halloween", name: "Halloween", emoji: "🎃", starts: "2026-10-01", ends: "2026-10-31" },
          next: { slug: "christmas", name: "Christmas", emoji: "🎄", starts: "2026-11-25", ends: "2026-12-25" },
        },
      },
    ]);
    renderPage();

    expect(await screen.findByText(/Showing 🎃 Halloween until/)).toBeInTheDocument();
    expect(screen.queryByText("Showing today")).toBeNull();
  });

  it("says a seasonal row is hidden until its next season", async () => {
    // Out of season the row is simply gone from Plex, which is the "my row disappeared" question
    // all over again — the badge answers it with the date it comes back.
    listCollections.mockResolvedValue([
      {
        ...SUBSET_ROW,
        show_days: [],
        shown_today: false,
        seasons: ["christmas"],
        season_status: {
          showing: null,
          next: { slug: "christmas", name: "Christmas", emoji: "🎄", starts: "2026-11-25", ends: "2026-12-25" },
        },
      },
    ]);
    renderPage();

    expect(await screen.findByText(/Hidden until 🎄 Christmas starts on/)).toBeInTheDocument();
  });

  it("says Hidden today for a scheduled row that is off", async () => {
    // The whole reason this badge exists: a scheduled row that is simply absent from Plex is
    // indistinguishable from a broken one, and "my row disappeared" is the question the feature
    // creates. `shown_today` comes from the server, so this never disagrees with Plex.
    listCollections.mockResolvedValue([
      { ...SUBSET_ROW, show_days: [1, 3, 5], shown_today: false },
    ]);
    renderPage();

    expect(await screen.findByText("Hidden today")).toBeInTheDocument();
  });
});


it("opens the add-a-row page from Add a row", async () => {
  getUsers.mockResolvedValue([]);
  listCollections.mockResolvedValue([SUBSET_ROW]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/rows"]}>
        <Routes>
          <Route path="/rows" element={<RowsPage />} />
          <Route path="/rows/new" element={<p>add a row page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const opener = screen.getByRole("button", { name: "Add a row" });
  await waitFor(() => expect(opener).toBeEnabled());
  await userEvent.click(opener);
  expect(await screen.findByText("add a row page")).toBeInTheDocument();
});

describe("RowsPage — the chip note and the card's meta line", () => {
  beforeEach(() => {
    getUsers.mockReset();
    listCollections.mockReset();
    getUsers.mockResolvedValue([]);
    localStorage.clear();
  });

  it("goes away for good once dismissed", async () => {
    listCollections.mockResolvedValue([{ ...SUBSET_ROW, name: "✨ {library_name} Picked for You" }]);
    const first = renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss this note" }));
    expect(screen.queryByText(/are placeholders/)).toBeNull();
    first.unmount();

    renderPage();
    await screen.findByRole("heading", { name: /Picked for You/ });
    expect(screen.queryByText(/are placeholders/)).toBeNull();
  });

  it("still shows the note when storage is blocked", async () => {
    const blocked = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    listCollections.mockResolvedValue([{ ...SUBSET_ROW, name: "✨ {library_name} Picked for You" }]);
    renderPage();
    expect(await screen.findByText(/are placeholders/)).toBeInTheDocument();
    blocked.mockRestore();
  });

  it("gives every card a kind, an audience and a schedule", async () => {
    listCollections.mockResolvedValue([SUBSET_ROW, { ...SUBSET_ROW, id: 2, slug: "manual", name: "Manual", schedule: "", build: "shared" }]);
    renderPage();
    expect(await screen.findByText(/Picked for You · .* · 15 titles · Movies & TV Shows · Every day at /)).toBeInTheDocument();
    expect(screen.getByText(/Popular on this server · .* · Manual runs only/)).toBeInTheDocument();
  });
});
