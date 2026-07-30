import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowEditor } from "@/components/rows/row-editor";
import type * as ApiModule from "@/lib/api";
import type { Collection, User } from "@/lib/types";

const { updateCollection, settingsData } = vi.hoisted(() => ({
  updateCollection: vi.fn((id: number, body: unknown) =>
    Promise.resolve({ ...(body as object), id }),
  ),
  // Mutable so a test can serve a real server's globals; empty = "settings haven't loaded".
  settingsData: { current: {} as Record<string, unknown> },
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      updateCollection: (id: number, body: unknown) =>
        updateCollection(id, body),
      getSettings: () => Promise.resolve(settingsData.current),
      getLibraries: () => Promise.resolve([]),
      getImageProvider: () =>
        Promise.resolve({ capable: false, provider: "", reason: "" }),
    },
  };
});

function row(patch: Partial<Collection> = {}): Collection {
  return {
    id: 1,
    slug: "hidden-gems",
    name: "Hidden Gems",
    last_run_id: null,
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    schedule: "30 3 * * *",
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    rewatch: false,
    unstarted_only: false,
    freshness: null,
    recent_count: null,
    max_seeds: null,
    placement: "both",
    placement_friends: "both",
    pin_top: false,
    hub_anchor: {},
    poster: { mode: "", title: "", subtitle: "", style: "", has_image: false },
    ...patch,
  };
}

function user(patch: Partial<User> = {}): User {
  return {
    id: 1,
    username: "sarah",
    slug: "sarah",
    user_type: "shared",
    restricted: false,
    enabled: true,
    cold_start: false,
    history_depth: 10,
    last_run_at: null,
    request_tag: "",
    hit_rate: null,
    ...patch,
  };
}

function renderEditor(collection: Collection, users: User[] = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <RowEditor collection={collection} users={users} onClose={() => {}} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("RowEditor — inherited globals", () => {
  beforeEach(() => {
    settingsData.current = {};
  });

  it("names the global each inheriting field is actually following", async () => {
    settingsData.current = {
      "recommendations.watched_pct": 0.4,
      "recommendations.freshness": 0.5,
      "recommendations.recent_count": 8,
      "recommendations.max_seeds": 30,
    };
    renderEditor(
      row({
        watched_pct: null,
        freshness: null,
        recent_count: null,
        max_seeds: null,
      }),
    );

    // The whole point: "use the global default" now says WHAT the global is.
    expect(
      await screen.findByText(/40% — up to 40% already-watched/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/50% — refreshes about every 8 days/),
    ).toBeInTheDocument();
    expect(screen.getByText("8 recent watches")).toBeInTheDocument();
    expect(screen.getByText("30 watches")).toBeInTheDocument();
  });

  it("claims no global value while settings are still loading", () => {
    renderEditor(row({ watched_pct: null }));

    expect(screen.queryByText(/^Currently/)).toBeNull();
  });

  it("says nothing about the global on a field that overrides it", async () => {
    settingsData.current = { "recommendations.watched_pct": 0.4 };
    renderEditor(row({ watched_pct: 0.25 }));

    await waitFor(() =>
      expect(screen.queryByText(/40% — up to 40% already-watched/)).toBeNull(),
    );
  });
});

describe("RowEditor — already-watched titles", () => {
  beforeEach(() => {
    updateCollection.mockClear();
    settingsData.current = {};
  });

  it("shows the watched slider when a row overrides the global cap", () => {
    renderEditor(row({ watched_pct: 0.25 }));
    const slider = screen.getByRole("slider", {
      name: /already-watched/i,
    });
    expect(slider).toHaveValue("25");
    // The "use the global default" switch is OFF when the row sets its own cap.
    expect(
      screen.getByRole("switch", { name: /global already-watched default/i }),
    ).not.toBeChecked();
  });

  it("hides the slider and checks the switch when the row inherits the global cap", () => {
    renderEditor(row({ watched_pct: null }));
    expect(
      screen.queryByRole("slider", { name: /already-watched/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: /global already-watched default/i }),
    ).toBeChecked();
  });

  it("round-trips a per-row watched cap into the PATCH body", async () => {
    renderEditor(row({ watched_pct: null }));

    // Turn off "use global default" to reveal the slider (starts at 0%).
    await userEvent.click(
      screen.getByRole("switch", { name: /global already-watched default/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const call = updateCollection.mock.calls.at(0);
    expect(call?.[0]).toBe(1);
    expect((call?.[1] as Collection).watched_pct).toBe(0);
  });
});

describe("RowEditor — the default row's name", () => {
  const defaultRow = (patch: Partial<Collection> = {}) =>
    row({
      slug: "picked",
      name: "✨ {library_name} Picked for You",
      ...patch,
    });

  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the current name read-only and points to the Rename button", () => {
    renderEditor(defaultRow());
    // The name is in a disabled input — renaming happens via the dedicated button.
    const input = screen.getByDisplayValue("✨ {library_name} Picked for You");
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: /Rename/ })).toBeInTheDocument();
  });
});

describe("RowEditor — placement", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("keeps the same grid on a SHARED row, dimming the cell Plex cannot express", () => {
    // The grid does not change shape between row types. A shared row is ONE Plex collection with a
    // single `promotedToRecommended` flag, so "on for me, off for them" is not expressible — that
    // cell is shown at its true value but disabled, with the reason on hover, rather than the row
    // quietly becoming a different control.
    renderEditor(row({ build: "shared", placement: "both" }));

    const owner = screen.getByRole("switch", {
      name: /Owner Library Recommended/i,
    });
    const friends = screen.getByRole("switch", {
      name: /Friends Library Recommended/i,
    });

    expect(owner).toBeDisabled();
    expect(friends).toBeEnabled();
    // Disabled, but still showing the TRUE state — the row really is on their Recommended shelf.
    expect(owner).toBeChecked();
    expect(owner).toHaveAttribute("title", expect.stringMatching(/single collection/i));

    // Home stays split and fully editable, because Home visibility really is per-share.
    expect(screen.getByRole("switch", { name: /Owner Home/i })).toBeEnabled();
    expect(screen.getByRole("switch", { name: /Friends' Home/i })).toBeEnabled();
  });

  it("a per-person row leaves all four editable — the asymmetry is Plex's, not ours", () => {
    renderEditor(row({ build: "per_person" }));

    for (const name of [
      /Owner Library Recommended/i,
      /Friends Library Recommended/i,
      /Owner Home/i,
      /Friends' Home/i,
    ]) {
      expect(screen.getByRole("switch", { name })).toBeEnabled();
    }
  });

  it("reflects the saved placement as switch states", () => {
    renderEditor(row({ placement: "library", placement_friends: "library" }));
    expect(
      screen.getByRole("switch", { name: /Owner Library Recommended/i }),
    ).toBeChecked();
    expect(
      screen.getByRole("switch", { name: /Owner Home/i }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("switch", { name: /Friends Library Recommended/i }),
    ).toBeChecked();
    expect(
      screen.getByRole("switch", { name: /Friends' Home/i }),
    ).not.toBeChecked();
  });

  it("round-trips a changed placement into the PATCH body", async () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }));

    // Turn off Home (owner) — leaves owner library + friends unchanged
    await userEvent.click(screen.getByRole("switch", { name: /Owner Home/i }));
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.placement).toBe("library");
    expect(body.placement_friends).toBe("both");
  });

  // Regression (issue #6): encode() had no "neither" case and fell through to "library", so turning
  // the second switch of a pair off silently turned the first back on. A surface must stay off.
  it("keeps both switches off when the last one in a pair is turned off", async () => {
    renderEditor(row({ placement: "home", placement_friends: "both" }));

    await userEvent.click(screen.getByRole("switch", { name: /Owner Home/i }));

    expect(
      screen.getByRole("switch", { name: /Owner Home/i }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("switch", { name: /Owner Library Recommended/i }),
    ).not.toBeChecked();
  });

  it("saves 'off' for an audience with every surface turned off", async () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }));

    for (const name of [
      /Owner Library Recommended/i,
      /Owner Home/i,
      /Friends Library Recommended/i,
      /Friends' Home/i,
    ]) {
      await userEvent.click(screen.getByRole("switch", { name }));
    }
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.placement).toBe("off");
    expect(body.placement_friends).toBe("off");
  });

  it("sets each audience's Recommended flag independently", async () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }));

    // The owner keeps their own row on the shelf; friends' rows come off it.
    await userEvent.click(
      screen.getByRole("switch", { name: /Friends Library Recommended/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.placement).toBe("both");
    expect(body.placement_friends).toBe("home");
  });

  it("warns only while friends' rows sit on the Recommended shelf", async () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }));
    expect(
      screen.getByText(/no share filter to hide them behind/i),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("switch", { name: /Friends Library Recommended/i }),
    );
    expect(
      screen.queryByText(/no share filter to hide them behind/i),
    ).toBeNull();
  });

  it("explains where an all-off row can still be found", async () => {
    renderEditor(row({ placement: "off", placement_friends: "off" }));
    expect(
      screen.getByText(/won.t appear on any Home screen or Recommended shelf/i),
    ).toBeInTheDocument();
  });

  it("names the owner account behind 'Just me', and counts everyone else", () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }), [
      user({ id: 1, user_type: "owner", display_name: "stevezau" }),
      user({ id: 2, slug: "sarah" }),
      user({ id: 3, slug: "mike" }),
    ]);

    expect(screen.getAllByText("Just me").length).toBeGreaterThan(0);
    // The whole point of the rename: "me" is a specific Plex account, so name it.
    expect(screen.getByText("stevezau")).toBeInTheDocument();
    expect(screen.getByText("2 other people")).toBeInTheDocument();
  });

  it("says 'Just me' with no name rather than a wrong one while the roster loads", () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }), []);

    expect(screen.getAllByText("Just me").length).toBeGreaterThan(0);
    expect(screen.queryByText(/^\d+ other (person|people)$/)).toBeNull();
  });

  it("offers an explanation of who sees what", async () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }));
    expect(screen.getByText(/How does this work/i)).toBeInTheDocument();
    // The bit people actually come here for: why they still see everyone's rows.
    expect(
      screen.getByText(/you don.t have a share with yourself/i),
    ).toBeInTheDocument();
  });

  it("restates the current toggles as the outcome they produce", () => {
    renderEditor(row({ placement: "both", placement_friends: "home" }));
    expect(
      screen.getByText(
        /Your row shows on your Home screen and your Recommended shelf\. Everyone else.s row shows on their Home screen\./i,
      ),
    ).toBeInTheDocument();
  });

  it("updates the outcome line as a surface is turned off", async () => {
    renderEditor(row({ placement: "both", placement_friends: "both" }));

    await userEvent.click(screen.getByRole("switch", { name: /Owner Home/i }));

    expect(
      screen.getByText(/Your row shows on your Recommended shelf\./i),
    ).toBeInTheDocument();
  });

  it("names the switch to turn off, and says so even when the owner's own is already off", () => {
    // placement "home" = the owner's row is OFF the Recommended shelf, friends' are on it. The
    // surprising state: your shelf is still full of their rows, which reads as a broken toggle.
    renderEditor(row({ placement: "home", placement_friends: "both" }));

    expect(
      screen.getByText(/Your row is off this shelf, but everyone else.s rows/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Everyone else . Library Recommended/i),
    ).toBeInTheDocument();
  });
});

describe("RowEditor — placement on a shared row", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  const sharedRow = (patch: Partial<Collection> = {}) =>
    row({ build: "shared", ...patch });

  it("shows both Recommended cells, with the un-expressible one disabled", () => {
    // One collection for everyone means one `promotedToRecommended`. Rather than the grid changing
    // shape between row types, the cell Plex cannot express is shown at its true value but disabled —
    // a control that stays put and explains itself is easier to learn than one that moves or vanishes.
    renderEditor(sharedRow({ placement: "both", placement_friends: "both" }));

    const owner = screen.getByRole("switch", {
      name: /Owner Library Recommended/i,
    });
    expect(owner).toBeDisabled();
    expect(owner).toBeChecked(); // disabled, but still telling the truth about the shelf
    expect(
      screen.getByRole("switch", { name: /Friends Library Recommended/i }),
    ).toBeEnabled();
    // Home still splits by audience — those are two real Plex flags on the one collection.
    expect(screen.getByRole("switch", { name: /Owner Home/i })).toBeChecked();
    expect(
      screen.getByRole("switch", { name: /Friends' Home/i }),
    ).toBeChecked();
  });

  it("writes the collapsed Recommended flag to both audiences", async () => {
    renderEditor(sharedRow({ placement: "both", placement_friends: "both" }));

    await userEvent.click(
      screen.getByRole("switch", { name: /Friends Library Recommended/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.placement).toBe("home");
    expect(body.placement_friends).toBe("home");
  });

  it("describes the one row everyone shares, not a row each", () => {
    renderEditor(sharedRow({ placement: "both", placement_friends: "both" }));

    expect(
      screen.getByText(
        /This row shows on everyone.s Home screen and the Recommended shelf\./i,
      ),
    ).toBeInTheDocument();
    // The owner-shelf warning is about OTHER people's rows — a shared row has none.
    expect(
      screen.queryByText(/no share filter to hide them behind/i),
    ).toBeNull();
  });
});

describe("RowEditor — freshness", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the freshness slider only when the row overrides the global default", () => {
    renderEditor(row({ freshness: 0.25 }));
    expect(
      screen.getByRole("slider", { name: /how often the row refreshes/i }),
    ).toHaveValue("25");
    expect(
      screen.getByRole("switch", { name: /global freshness default/i }),
    ).not.toBeChecked();
  });

  it("stops inheriting at the global's own value, not at zero", async () => {
    // Turning "use the global" OFF should stop TRACKING the global, not change what the row does.
    // It used to snap to 0 — i.e. silently froze the row — which reads as a broken switch.
    renderEditor(row({ freshness: null }));

    await userEvent.click(
      screen.getByRole("switch", { name: /global freshness default/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).freshness,
    ).toBe(0.5);
  });
});

describe("RowEditor — recent watches to search", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the number field only when the row overrides the global default", () => {
    renderEditor(row({ recent_count: 5 }));
    expect(screen.getByLabelText(/Recent watches to search/i)).toHaveValue(5);
    expect(
      screen.getByRole("switch", { name: /global recent-watches default/i }),
    ).not.toBeChecked();
  });

  it("round-trips a per-row recent_count into the PATCH body", async () => {
    renderEditor(row({ recent_count: null }));

    await userEvent.click(
      screen.getByRole("switch", { name: /global recent-watches default/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).recent_count,
    ).toBe(10);
  });
});

describe("RowEditor — watches to build from", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the number field only when the row overrides the default", () => {
    renderEditor(row({ max_seeds: 3 }));
    expect(screen.getByLabelText(/^Watches to build from$/i)).toHaveValue(3);
    expect(
      screen.getByRole("switch", {
        name: /default number of watches to build from/i,
      }),
    ).not.toBeChecked();
  });

  it("round-trips a per-row max_seeds into the PATCH body", async () => {
    renderEditor(row({ max_seeds: null, media: "movie" }));

    await userEvent.click(
      screen.getByRole("switch", {
        name: /default number of watches to build from/i,
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    // 1, not the 30 default: turning the switch off is what someone does to make a
    // "Because you watched X" row honest, so the field opens where that lands.
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).max_seeds,
    ).toBe(1);
  });

  it("opens at 2, not 1, for a row covering movies AND TV", async () => {
    // Seeds are balanced across the media types present, so a budget of 1 yields ONE type — a
    // "both" row at 1 gathers nothing for its other half and that library never builds.
    renderEditor(row({ max_seeds: null, media: "both" }));

    await userEvent.click(
      screen.getByRole("switch", {
        name: /default number of watches to build from/i,
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).max_seeds,
    ).toBe(2);
  });

  it("warns a {top_seed} row that its name promises one title", () => {
    renderEditor(
      row({ name_template: "Because you watched {top_seed}", media: "movie" }),
    );
    expect(
      screen.getByText(/names one watch and fills itself from the other 29/i),
    ).toBeInTheDocument();
    // A movies-only row has no other half to strand, so it must not get the both-media caveat.
    expect(
      screen.queryByText(/would leave the other half empty/i),
    ).not.toBeInTheDocument();
  });

  it("tells a movies-and-TV {top_seed} row why 1 would strand half of it", () => {
    renderEditor(
      row({ name_template: "Because you watched {top_seed}", media: "both" }),
    );
    expect(
      screen.getByText(/would leave the other half empty/i),
    ).toBeInTheDocument();
  });

  it("stays quiet for a row whose name makes no such promise", () => {
    renderEditor(row({ name_template: "{library_name} Picked for You" }));
    expect(
      screen.queryByText(/names one watch and fills itself/i),
    ).not.toBeInTheDocument();
  });
});

describe("RowEditor — a shared row that can never build", () => {
  const sharedRow = (patch: Partial<Collection> = {}) =>
    row({ build: "shared", min_watchers: 2, ...patch });
  const warning = () => screen.queryByText(/This row can’t build yet/i);

  it("warns when only one person in the audience is active in runs", () => {
    // The exact shape of issue #3: a shared row on a server with one enabled user can never reach
    // its 2-watcher floor, so it silently reports "skipped" every night forever.
    renderEditor(sharedRow(), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike", enabled: false }),
    ]);
    expect(warning()).toBeInTheDocument();
    expect(
      screen.getByText(/only 1 of them is active in runs/i),
    ).toBeInTheDocument();
  });

  it("counts a PAUSED user as inactive — the engine drops them before any row is built", () => {
    renderEditor(sharedRow(), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike", prefs: { paused: true } }),
    ]);
    expect(warning()).toBeInTheDocument();
  });

  it("says nobody rather than 'only 0' when the audience is empty", () => {
    renderEditor(sharedRow({ audience: "subset", audience_user_ids: [] }), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike" }),
    ]);
    expect(
      screen.getByText(/nobody in its audience is active in runs/i),
    ).toBeInTheDocument();
  });

  it("stays quiet once the row can actually build", () => {
    renderEditor(sharedRow(), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike" }),
    ]);
    expect(warning()).toBeNull();
  });

  it("stays quiet on a per-person row, which has no watcher floor at all", () => {
    renderEditor(row({ build: "per_person" }), [user({ id: 1 })]);
    expect(warning()).toBeNull();
  });
});

describe("RowEditor — name template variables", () => {
  function renderNewRow() {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <RowEditor collection={null} users={[]} onClose={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
  }

  it("tells you which variables the name accepts", () => {
    renderNewRow();
    // Without this the Name box looks like plain text and nobody discovers per-person naming.
    expect(screen.getByText("{user}")).toBeInTheDocument();
    expect(screen.getByText("{library_name}")).toBeInTheDocument();
    expect(screen.getByText("{top_seed}")).toBeInTheDocument();
  });

  it("previews what a templated name becomes on Plex", async () => {
    const user = userEvent.setup();
    renderNewRow();

    // `{{` is user-event's escape for a literal brace, so this types "{user}'s Picks".
    await user.type(screen.getByLabelText("Name"), "{{user}'s Picks");
    expect(screen.getByText(/Sarah's Picks/)).toBeInTheDocument();
  });

  it("shows no preview for a plain name — there is nothing to substitute", async () => {
    const user = userEvent.setup();
    renderNewRow();

    await user.type(screen.getByLabelText("Name"), "Hidden Gems");
    expect(screen.queryByText(/would see/)).not.toBeInTheDocument();
  });
});
