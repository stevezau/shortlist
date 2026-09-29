import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RowRenamePage } from "@/pages/row-rename";

const { listCollections, updateCollection } = vi.hoisted(() => ({
  listCollections: vi.fn(),
  updateCollection: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, listCollections, updateCollection } };
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
