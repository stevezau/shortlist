import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowCard } from "@/components/rows/row-card";
import type { Collection, User } from "@/lib/types";

const updateCollection = vi.fn((_id: number, _body: unknown) =>
  Promise.resolve({}),
);
const startRun = vi.fn((_body: unknown) => Promise.resolve({ run_id: 42 }));
// Fails unless a test serves a history: a card must still render when the row's history can't load.
const getCollectionEffectiveness = vi.fn((_id: number): Promise<unknown> => Promise.reject(new Error("no history")));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  apiUrl: (path: string) => path,
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
    getCollectionEffectiveness: (id: number) => getCollectionEffectiveness(id),
  },
}));

const USERS: User[] = [];

function collection(patch: Partial<Collection> = {}): Collection {
  return {
    id: 1,
    slug: "hidden-gems",
    name: "Hidden Gems",
    last_run_id: null,
    preview_titles: [],
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

  it("names a fixed AI row for its theme and marks it AI", async () => {
    renderCard(
      collection({
        name: "{theme_emoji} {theme}",
        name_template: "{theme_emoji} {theme}",
        theme_id: 5,
        theme_name: "Heist films",
        theme_emoji: "🕶️",
      }),
    );

    expect(await screen.findByRole("heading", { name: "🕶️ Heist films" })).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.queryByText("theme emoji")).not.toBeInTheDocument();
  });

  it("drops the emoji slot cleanly when the theme has none", async () => {
    renderCard(
      collection({ name: "{theme_emoji} {theme}", theme_id: 5, theme_name: "Heist films", theme_emoji: null }),
    );

    expect(await screen.findByRole("heading", { name: "Heist films" })).toBeInTheDocument();
  });

  it("keeps the placeholder chips on an Explore row, which has no one theme, but still says AI", async () => {
    renderCard(collection({ name: "{theme_emoji} {theme}", theme_id: 5, theme_name: null, theme_emoji: null }));

    expect(await screen.findByText("theme")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
  });

  it("does not mark an ordinary row AI", async () => {
    renderCard(collection());

    await screen.findByRole("heading", { name: "Hidden Gems" });
    expect(screen.queryByText("AI")).not.toBeInTheDocument();
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

  it("offers the way out on the default row, same as every other row", async () => {
    // The default row used to hide this control, so the first card in the list lacked what every
    // card below it had, with nothing on screen explaining why. Disabling it is still the
    // reversible option; deleting it is allowed.
    renderCard(collection({ slug: "picked", name: "Picked for You" }));

    // In the card's "⋯" menu now, so no red text sits on the list until someone opens it.
    await userEvent.click(screen.getByRole("button", { name: "More actions for Picked for You" }));
    expect(screen.getByRole("menuitem", { name: "Remove or delete…" })).toBeInTheDocument();
  });

  it("offers ONE way out, pointing at the editor that explains the difference", async () => {
    // "Remove from Plex" and "Delete" sat here side by side with nothing saying which one loses the
    // row's settings — a hover title each, and nothing at all on a phone (audit finding, Sep 2026).
    // The editor's danger section already states the difference above the same two buttons.
    renderCard(collection({ id: 9, slug: "gems", name: "Hidden Gems" }));

    await userEvent.click(screen.getByRole("button", { name: "More actions for Hidden Gems" }));
    const out = screen.getByRole("menuitem", { name: "Remove or delete…" });
    expect(out).toHaveAttribute("href", "/rows/9#remove-this-row");
    // Neither of the two ambiguous buttons may survive on the card.
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Remove Hidden Gems from Plex/ }),
    ).toBeNull();
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

  const POSTER = { mode: "upload", title: "", subtitle: "", style: "", has_image: true } as Collection["poster"];
  const picks = (...keys: number[]) => keys.map((rating_key) => ({ rating_key, title: `Title ${rating_key}` }));

  it("shows four of the row's latest picks, through the same poster proxy as the run detail, ahead of its own poster", () => {
    const { container } = renderCard(collection({ preview_titles: picks(11, 12, 13, 14), poster: POSTER }));
    const sources = [...container.querySelectorAll("img")].map((img) => img.getAttribute("src"));
    expect(sources).toEqual([
      "/api/picks/11/poster",
      "/api/picks/12/poster",
      "/api/picks/13/poster",
      "/api/picks/14/poster",
    ]);
    expect(container.querySelector('[title^="No poster"]')).toBeNull();
  });

  it("falls back to the row's own poster when it has fewer than four latest picks to show", () => {
    const { container } = renderCard(collection({ preview_titles: picks(11, 12, 13), poster: POSTER }));
    const sources = [...container.querySelectorAll("img")].map((img) => img.getAttribute("src"));
    expect(sources).toHaveLength(1);
    expect(sources[0]).toMatch(/^\/api\/collections\/1\/poster\/image/);
  });

  it("falls back to the placeholder when it has neither enough picks nor a poster", () => {
    const { container } = renderCard(collection({ preview_titles: picks(11) }));
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector('[title^="No poster"]')).toBeTruthy();
  });

  it("asks before turning a row OFF, and does not save until you confirm", async () => {
    // The toggle's consequence reaches past this screen: saving it takes the row off Plex for everyone
    // who has it straight away (`row_changes.py`, RECONCILE collection.disable). A switch is the wrong
    // amount of ceremony for that on its own.
    const user = userEvent.setup();
    updateCollection.mockClear();
    renderCard(collection({ enabled: true }));

    await user.click(await screen.findByRole("switch"));

    expect(updateCollection).not.toHaveBeenCalled();
    expect(screen.getByText(/takes it off Plex straight away, for everyone who has it/i)).toBeTruthy();

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

  it("keeps Edit, Runs, Rename and the way out in one menu, and no red until it opens", async () => {
    // Four actions sat on every card as buttons, one of them red, so a list of four rows carried four
    // red "Remove or delete" labels before anyone had decided to remove anything.
    const { container } = renderCard(collection({ id: 9, slug: "gems", name: "Hidden Gems" }));

    expect(screen.queryByRole("menu")).toBeNull();
    expect(container.querySelector(".text-destructive-text")).toBeNull();

    const trigger = screen.getByRole("button", { name: "More actions for Hidden Gems" });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    await userEvent.click(trigger);

    const menu = screen.getByRole("menu", { name: "More actions for Hidden Gems" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual(["Edit", "Runs", "Rename on Plex…", "Remove or delete…"]);
    expect(items[1]).toHaveAttribute("href", "/runs?row=gems");
    expect(items[2]).toHaveAttribute("href", "/rows/9/rename");
    expect(items[3]).toHaveAttribute("href", "/rows/9#remove-this-row");
    expect(items[3]).toHaveClass("text-destructive-text");
    expect(items[0]).toHaveFocus();
  });

  it("says when the row last built, and that an off row is on nobody's Plex", async () => {
    const today = new Date();
    today.setHours(2, 30, 0, 0);
    const history = { last_delivered_at: today.toISOString(), first_delivered_at: today.toISOString() };
    const time = today.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

    getCollectionEffectiveness.mockResolvedValueOnce(history);
    const { unmount } = renderCard(collection());
    expect(await screen.findByText(`Last built ${time} today`)).toBeInTheDocument();
    unmount();

    getCollectionEffectiveness.mockResolvedValueOnce(history);
    renderCard(collection({ enabled: false }));
    expect(await screen.findByText(`Off, not on anyone's Plex · last built ${time} today`)).toBeInTheDocument();
  });

  it("claims no build at all while the row's history can't be read", async () => {
    renderCard(collection());
    await waitFor(() => expect(getCollectionEffectiveness).toHaveBeenCalled());
    expect(screen.queryByText(/Not built yet|Last built/)).toBeNull();
  });

  it("walks the menu with the arrow keys and closes it on Escape, back on its button", async () => {
    renderCard(collection());
    const trigger = screen.getByRole("button", { name: "More actions for Hidden Gems" });
    trigger.focus();
    await userEvent.keyboard("{Enter}");

    const items = within(screen.getByRole("menu")).getAllByRole("menuitem");
    await userEvent.keyboard("{ArrowDown}");
    expect(items[1]).toHaveFocus();
    await userEvent.keyboard("{ArrowUp}{ArrowUp}");
    expect(items[3]).toHaveFocus();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
  });

  it("never renames in place — Rename on Plex… is a link to the rename screen", () => {
    renderCard(collection());
    expect(screen.queryByRole("button", { name: /^Rename$/i })).toBeNull();
  });

  it("shows a real template token as a placeholder, keeping the words around it", () => {
    // The list is the one screen that shows the row's TEMPLATE; everywhere else shows it resolved
    // ("✨ Movies Picked for You"). Printed raw, it reads as a substitution that failed.
    renderCard(collection({ name: "✨ {library_name} Picked for You" }));

    expect(screen.getByText("library name")).toBeTruthy();
    expect(screen.queryByText(/\{library_name\}/)).toBeNull();
    expect(screen.getByText(/Picked for You/)).toBeTruthy();
  });

  it("leaves a token the engine does not substitute exactly as typed", () => {
    // The name field is free text and no whitelist is enforced server-side, so `{genre}` reaches
    // Plex as literal braces in the collection title. Dressing it up as a resolved placeholder
    // would hide the typo on the one screen that could show it before the next run ships it.
    renderCard(collection({ name: "Best of {genre}" }));

    expect(screen.getByText(/Best of \{genre\}/)).toBeTruthy();
    expect(screen.queryByText("genre")).toBeNull();
  });
});
