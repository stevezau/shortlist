import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowCard } from "@/components/rows/row-card";
import type { Collection, User } from "@/lib/types";

const updateCollection = vi.fn((_id: number, _body: unknown) =>
  Promise.resolve({}),
);
const startRun = vi.fn((_body: unknown) => Promise.resolve({ run_id: 42 }));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    posterImageUrl: (id: number) => `/api/collections/${id}/poster/image`,
    getSettings: () => Promise.resolve({ "row.size": "15" }),
    getLibraries: () =>
      Promise.resolve([
        { key: "1", title: "Movies", type: "movie" },
        { key: "2", title: "4K Movies", type: "movie" },
      ]),
    updateCollection: (id: number, body: unknown) => updateCollection(id, body),
    startRun: (body: unknown) => startRun(body),
  },
}));

const USERS: User[] = [];

function collection(patch: Partial<Collection> = {}): Collection {
  return {
    id: 1,
    slug: "hidden-gems",
    name: "Hidden Gems",
    last_run_id: null,
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    refresh_days: null,
    placement: "both",
    pin_top: false,
    hub_anchor: {},
    ...patch,
  } as Collection;
}

function renderCard(value: Collection) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        {/* A real route table, so "Run lands you on the run it started" is asserted as navigation
            rather than as a mocked callback that could point anywhere. */}
        <Routes>
          <Route
            path="/"
            element={
              <RowCard collection={value} users={USERS} onEdit={() => {}} />
            }
          />
          <Route path="/runs/:id" element={<p>run detail</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowCard", () => {
  beforeEach(() => {
    startRun.mockClear();
    updateCollection.mockClear();
  });

  it("runs just this row, and lands on the run it started", async () => {
    // `collection_ids` has always been part of POST /api/runs; the only way to reach it was the
    // "Run selected rows…" dialog on the Runs page, where you re-picked the row you were looking at.
    renderCard(collection());

    await userEvent.click(
      await screen.findByRole("button", { name: /Run Hidden Gems now/i }),
    );

    // The ids are the whole contract of this button — asserting only that a run started would pass
    // just as happily for "rebuild every row on the server".
    expect(startRun).toHaveBeenCalledWith({ collection_ids: [1] });
    expect(await screen.findByText("run detail")).toBeInTheDocument();
  });

  it("won't run a row that is switched off", async () => {
    // A run SKIPS a disabled row and then takes it off Plex, so "Run" there does the opposite of
    // what the word promises.
    renderCard(collection({ enabled: false }));

    const run = await screen.findByRole("button", {
      name: /Run Hidden Gems now/i,
    });
    expect(run).toBeDisabled();
    await userEvent.click(run, { pointerEventsCheck: 0 });
    expect(startRun).not.toHaveBeenCalled();
  });

  it("offers Delete on the default row, same as every other row", async () => {
    // The default row used to hide this button, so the first card in the list lacked the control
    // every card below it had, with nothing on screen explaining why. Disabling it is still the
    // reversible option; deleting it is allowed.
    renderCard(collection({ slug: "picked", name: "Picked for You" }));

    expect(
      await screen.findByRole("button", { name: /Delete Picked for You/ }),
    ).toBeEnabled();
  });

  it("shows a row's own sources and libraries so overrides are visible without opening it", async () => {
    renderCard(
      collection({
        candidate_sources: ["trakt"],
        library_keys: ["2"],
      }),
    );
    expect(await screen.findByText("Sources: Trakt")).toBeTruthy();
    expect(await screen.findByText("Libraries: 4K Movies")).toBeTruthy();
  });

  it("shows no override badges for a row that follows the global defaults", () => {
    renderCard(collection());
    expect(screen.queryByText(/^Sources:/)).toBeNull();
    expect(screen.queryByText(/^Libraries:/)).toBeNull();
  });

  // The poster slot is fixed-width whether or not there's an image, so every card in the list lines
  // up. Rendering the <img> conditionally with nothing in its place shifted posterless rows left.
  it("keeps a poster-sized slot for a row with no poster", () => {
    const { container } = renderCard(collection());
    expect(container.querySelector("img")).toBeNull();
    const slot = container.querySelector('[title^="No poster"]');
    expect(slot).toBeTruthy();
    expect(slot?.className).toContain("h-16");
    expect(slot?.className).toContain("w-11");
  });

  it("shows the image, and no placeholder, for a row that has a poster", () => {
    const { container } = renderCard(
      collection({
        poster: {
          mode: "upload",
          title: "",
          subtitle: "",
          style: "",
          has_image: true,
        },
      }),
    );
    const img = container.querySelector("img");
    expect(img).toBeTruthy();
    expect(img?.className).toContain("h-16");
    expect(container.querySelector('[title^="No poster"]')).toBeNull();
  });

  it("asks before turning a row OFF, and does not save until you confirm", async () => {
    // The toggle's consequence is invisible and deferred: the next run takes the row off Plex for
    // everyone who has it (`rows._remove_muted_and_retired`). A switch is the wrong amount of
    // ceremony for that on its own.
    const user = userEvent.setup();
    updateCollection.mockClear();
    renderCard(collection({ enabled: true }));

    await user.click(await screen.findByRole("switch"));

    expect(updateCollection).not.toHaveBeenCalled();
    expect(screen.getByText(/next run takes this row off Plex/i)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: /Turn it off/i }));
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect(updateCollection.mock.calls[0]?.[1]).toMatchObject({
      enabled: false,
    });
  });

  it("keeps the row on if you back out of the confirmation", async () => {
    const user = userEvent.setup();
    updateCollection.mockClear();
    renderCard(collection({ enabled: true }));

    await user.click(await screen.findByRole("switch"));
    await user.click(screen.getByRole("button", { name: /Keep it on/i }));

    expect(updateCollection).not.toHaveBeenCalled();
  });

  it("turns a row back ON in one click — enabling removes nothing", async () => {
    const user = userEvent.setup();
    updateCollection.mockClear();
    renderCard(collection({ enabled: false }));

    await user.click(await screen.findByRole("switch"));

    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect(updateCollection.mock.calls[0]?.[1]).toMatchObject({
      enabled: true,
    });
  });

  it("does not offer Rename — that lives in the editor, beside the name it changes", () => {
    renderCard(collection());
    expect(screen.queryByRole("button", { name: /^Rename$/i })).toBeNull();
  });
});
