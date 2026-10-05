import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RowRenamePage } from "@/pages/row-rename";

const { listCollections, updateCollection, getUsers } = vi.hoisted(() => ({
  listCollections: vi.fn(),
  updateCollection: vi.fn(),
  // Resolved by default so no test reaches for `fetch`, which several of them stub for the stream.
  getUsers: vi.fn((): Promise<unknown[]> => Promise.resolve([])),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, listCollections, updateCollection, getUsers } };
});

function streamOf(events: object[]) {
  const body = new TextEncoder().encode(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(""));
  let sent = false;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => (sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: body })),
      }),
    },
  };
}

function renderRename(state: object = { proposedName: "New Name" }, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[{ pathname: "/rows/7/rename", state }]}>
        <Routes>
          <Route path="/rows/:id/rename" element={<RowRenamePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowRenamePage — what each person's rename came to", () => {
  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn(); // jsdom has no layout, so no scrolling
    listCollections.mockResolvedValue([{ id: 7, slug: "comedy", name: "Old Name", name_template: "Old Name" }]);
    updateCollection.mockResolvedValue({});
  });
  afterEach(() => vi.unstubAllGlobals());

  it("prefills the saved name after a direct page load without claiming a rename happened", async () => {
    renderRename({});
    expect(await screen.findByDisplayValue("Old Name")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to rename/)).not.toBeInTheDocument();
    expect(updateCollection).not.toHaveBeenCalled();
  });

  it("keeps a typed draft when the collection refreshes", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderRename({}, client);
    const input = await screen.findByDisplayValue("Old Name");
    await userEvent.clear(input);
    await userEvent.type(input, "My draft");
    listCollections.mockResolvedValue([{ id: 7, slug: "comedy", name: "Refreshed", name_template: "Refreshed" }]);
    await client.invalidateQueries({ queryKey: ["collections"] });
    expect(screen.getByDisplayValue("My draft")).toBeInTheDocument();
  });

  it("keeps going past one person Plex refused, and says why in plain words", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamOf([
          {
            user: "sarah",
            display_name: "Sarah",
            library: "Movies",
            error: "Plex refused 'New Name' in Movies: something in that library already has that name.",
          },
          { user: "mike", display_name: "Mike", old: "Old Name", new: "New Name", libraries: ["Movies"] },
          { done: true, total: 1 },
        ]),
      ),
    );

    renderRename();

    const log = await screen.findByRole("list", { name: /rename results/i });
    expect(within(log).getByText(/Mike/)).toBeInTheDocument();
    expect(within(log).getByText(/Sarah: Plex refused/)).toBeInTheDocument();
    expect(await screen.findByText(/renamed 1 collection on Plex/i)).toBeInTheDocument();
    expect(screen.getByText(/1 could not be renamed/i)).toBeInTheDocument();
  });

  it("says a row that takes its name at the next run is not renamed yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamOf([
          {
            user: "sarah",
            display_name: "Sarah",
            old: "Old Name",
            new: "New Name",
            libraries: ["Movies"],
            next_run: true,
          },
          { done: true, total: 0 },
        ]),
      ),
    );

    renderRename();

    const log = await screen.findByRole("list", { name: /rename results/i });
    expect(within(log).getByText(/at this row's next run/i)).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to rename/i)).not.toBeInTheDocument();
  });

  it("still stops on an error that is not about one person", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(streamOf([{ error: "Plex isn't reachable." }, { done: true, total: 0 }])),
    );

    renderRename();

    expect(await screen.findByText("Plex isn't reachable.")).toBeInTheDocument();
  });
});

describe("RowRenamePage — where the name was saved", () => {
  const fetchMock = vi.fn();
  const streamed = () => JSON.parse((fetchMock.mock.calls[0]?.[1] as RequestInit).body as string);

  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn();
    // After the editor's save the cached row already carries the new name.
    listCollections.mockResolvedValue([{ id: 7, slug: "comedy", name: "New Name", name_template: "New Name" }]);
    updateCollection.mockReset().mockResolvedValue({});
    fetchMock.mockReset().mockResolvedValue(streamOf([{ done: true, total: 0 }]));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("streams straight from the old title when the editor already saved the name", async () => {
    renderRename({ proposedName: "New Name", oldTemplate: "{season} picks", alreadySaved: true });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(streamed()).toEqual({ name_template: "New Name", old_template: "{season} picks" });
    expect(updateCollection).not.toHaveBeenCalled();
  });

  it.each([
    ["the stream reports an error", () => fetchMock.mockResolvedValue(streamOf([{ error: "Plex isn't reachable." }]))],
    ["the request fails", () => fetchMock.mockRejectedValue(new Error("Plex isn't reachable."))],
  ])("says the name was saved but Plex wasn't renamed when %s", async (_, fail) => {
    fail();
    renderRename({ proposedName: "New Name", oldTemplate: "{season} picks", alreadySaved: true });

    expect(
      await screen.findByText(
        "Your settings and the new name were saved, but the rename didn't finish on Plex. It's applied the next time the row runs.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Plex isn't reachable.")).toBeInTheDocument();
  });

  it("still saves the name with the rename deferred, then streams, when it arrives from Rename…", async () => {
    listCollections.mockResolvedValue([{ id: 7, slug: "comedy", name: "Old Name", name_template: "Old Name" }]);
    renderRename({ proposedName: "New Name" });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(updateCollection).toHaveBeenCalledWith(7, {
      name: "New Name",
      name_template: "New Name",
      defer_rename: true,
    });
    expect(updateCollection.mock.invocationCallOrder[0]).toBeLessThan(fetchMock.mock.invocationCallOrder[0] ?? 0);
    expect(streamed()).toEqual({ name_template: "New Name", old_template: "Old Name" });
  });
});

describe("RowRenamePage — how far a rename reaches, before the button", () => {
  const person = (id: number, enabled = true, departed = false) => ({
    id,
    username: `user${id}`,
    display_name: `User ${id}`,
    enabled,
    departed,
  });

  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn();
    getUsers.mockReset();
  });

  function row(overrides: object) {
    listCollections.mockResolvedValue([
      { id: 7, slug: "comedy", name: "Old Name", name_template: "Old Name", build: "per_person", ...overrides },
    ]);
  }

  it("counts the enabled people a chosen-people row is built for", async () => {
    row({ audience: "subset", audience_user_ids: [1, 2, 3] });
    getUsers.mockResolvedValue([person(1), person(2), person(3, false), person(4)]);
    renderRename({});

    const reach = await screen.findByText("Renames this row’s collection for 2 people.");
    const button = screen.getByRole("button", { name: "Rename on Plex" });
    expect(reach.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(button).toBeEnabled();
  });

  it("counts everyone enabled and still on the server for an everyone row", async () => {
    row({ audience: "everyone", audience_user_ids: [] });
    getUsers.mockResolvedValue([person(1), person(2), person(3), person(4, false), person(5, true, true)]);
    renderRename({});

    expect(await screen.findByText("Renames this row’s collection for 3 people.")).toBeInTheDocument();
  });

  it("says one person, not one people", async () => {
    row({ audience: "subset", audience_user_ids: [1] });
    getUsers.mockResolvedValue([person(1)]);
    renderRename({});

    expect(await screen.findByText("Renames this row’s collection for 1 person.")).toBeInTheDocument();
  });

  it("names the one shared collection for a shared row", async () => {
    row({ build: "shared", audience: "everyone", audience_user_ids: [] });
    getUsers.mockResolvedValue([person(1), person(2), person(3)]);
    renderRename({});

    expect(
      await screen.findByText("Renames this row’s shared collection, seen by 3 people."),
    ).toBeInTheDocument();
  });

  it("holds the Rename button until the count has loaded", async () => {
    row({ audience: "everyone", audience_user_ids: [] });
    let resolveUsers: (value: unknown[]) => void = () => {};
    getUsers.mockImplementation(() => new Promise((resolve) => { resolveUsers = resolve; }));
    renderRename({});

    const button = await screen.findByRole("button", { name: "Rename on Plex" });
    expect(button).toBeDisabled();
    resolveUsers([person(1)]);
    await waitFor(() => expect(button).toBeEnabled());
  });
});
