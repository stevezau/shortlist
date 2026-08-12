import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowEditor } from "@/components/rows/row-editor";
import type * as ApiModule from "@/lib/api";
import type { Collection, User } from "@/lib/types";

const { updateCollection, settingsData, startRun } = vi.hoisted(() => ({
  updateCollection: vi.fn((id: number, body: unknown) =>
    Promise.resolve({ ...(body as object), id }),
  ),
  startRun: vi.fn((_body: unknown) => Promise.resolve({ run_id: 42 })),
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
      startRun: (body: unknown) => startRun(body),
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
    refresh_days: null,
    recency: null,
    recent_count: null,
    max_seeds: null,
    cold_start: null,
    seed_window: 1,
    pick_order: "best",
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

describe("RowEditor — acting on the row you're editing", () => {
  beforeEach(() => {
    settingsData.current = {};
    startRun.mockClear();
  });

  it("rebuilds this row and shows its run history, without a trip to another page", async () => {
    // Both actions existed already — Run behind a dialog on the Runs page that made you re-pick the
    // row you were editing, Runs only on the Rows CARD, which this page replaced. Neither was
    // reachable from the editor at all.
    renderEditor(row());

    expect(await screen.findByRole("link", { name: /Runs/ })).toHaveAttribute(
      "href",
      "/runs?row=hidden-gems",
    );

    await userEvent.click(
      screen.getByRole("button", { name: /Run Hidden Gems now/i }),
    );
    expect(startRun).toHaveBeenCalledWith({ collection_ids: [1] });
  });

  it("warns that Run rebuilds the SAVED row once the form has been edited", async () => {
    // Run is scoped to the stored row, so pressing it mid-edit silently rebuilds without your
    // changes — and nothing on the button says so.
    renderEditor(row());

    await screen.findByRole("button", { name: /Run Hidden Gems now/i });
    expect(screen.queryByText(/unsaved changes/i)).toBeNull();

    await userEvent.click(
      screen.getByRole("switch", { name: /watch it again row/i }),
    );

    expect(await screen.findByText(/unsaved changes/i)).toBeInTheDocument();
  });
});

describe("RowEditor — inherited globals", () => {
  beforeEach(() => {
    settingsData.current = {};
  });

  it("names the global each inheriting field is actually following", async () => {
    settingsData.current = {
      "recommendations.watched_pct": 0.4,
      "recommendations.refresh_days": 8,
      "recommendations.recent_count": 8,
      "candidates.sources": ["tmdb_similar", "llm_web"],
      "recommendations.max_seeds": 30,
    };
    renderEditor(
      row({
        watched_pct: null,
        refresh_days: null,
        recent_count: null,
        max_seeds: null,
      }),
    );

    // The whole point: "use the global default" now says WHAT the global is.
    expect(
      await screen.findByText(/40% — up to 40% already-watched/),
    ).toBeInTheDocument();
    expect(screen.getByText(/every 8 days/)).toBeInTheDocument();
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

  it("lets you type a new name, and says it is not applied until you press Rename", async () => {
    renderEditor(defaultRow());
    const input = screen.getByDisplayValue("✨ {library_name} Picked for You");
    expect(input).toBeEnabled();

    await userEvent.type(input, "!");

    // The warning is the whole point of letting the box be editable: a name typed here has changed
    // nothing on Plex yet, and Save on this page will not apply it either.
    expect(await screen.findByRole("status")).toHaveTextContent(
      /Not applied yet/i,
    );
    expect(screen.getByRole("button", { name: /Rename/ })).toBeEnabled();
  });

  it("keeps Rename disabled until the name actually changes", async () => {
    // Enabled on an unchanged name it offered to rewrite every collection on Plex, for every
    // person, to the name they already had — minutes of writes for no change at all.
    renderEditor(defaultRow());
    const input = screen.getByDisplayValue("✨ {library_name} Picked for You");

    expect(screen.getByRole("button", { name: /Rename/ })).toBeDisabled();

    await userEvent.type(input, "!");
    expect(screen.getByRole("button", { name: /Rename/ })).toBeEnabled();

    // Typed back to what it was: nothing to apply, so nothing to press.
    await userEvent.type(input, "{backspace}");
    expect(screen.getByRole("button", { name: /Rename/ })).toBeDisabled();
  });

  it("does NOT send the typed name when the page is saved", async () => {
    // The draft is held apart from the form on purpose. Saving a new name here without renaming on
    // Plex would leave the database and the server disagreeing, with nothing on screen saying so.
    renderEditor(defaultRow());
    await userEvent.type(
      screen.getByDisplayValue("✨ {library_name} Picked for You"),
      " CHANGED",
    );
    await userEvent.click(screen.getByRole("button", { name: /^Save/ }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls[0]?.[1] as {
      name?: string;
      name_template?: string;
    };
    expect(body.name ?? "").not.toMatch(/CHANGED/);
    expect(body.name_template ?? "").not.toMatch(/CHANGED/);
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

    // Marked unavailable via aria-disabled, NOT the native `disabled` attribute — a truly disabled
    // switch drops out of the tab order, so its explanation could never be reached by keyboard or
    // screen reader (issue: aria-describedby pointed at an id that never existed either).
    expect(owner).toHaveAttribute("aria-disabled", "true");
    expect(owner).not.toBeDisabled();
    expect(friends).not.toHaveAttribute("aria-disabled");
    // Disabled, but still showing the TRUE state — the row really is on their Recommended shelf.
    expect(owner).toBeChecked();
    expect(owner).toHaveAttribute(
      "title",
      expect.stringMatching(/single collection/i),
    );
    // The explanation is announced: aria-describedby resolves to a real, matching id.
    const describedBy = owner.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy as string)).toHaveTextContent(
      /single collection/i,
    );

    // Home stays split and fully editable, because Home visibility really is per-share.
    expect(screen.getByRole("switch", { name: /Owner Home/i })).toBeEnabled();
    expect(
      screen.getByRole("switch", { name: /Friends' Home/i }),
    ).toBeEnabled();
  });

  it("keeps a disabled cell reachable by keyboard, and its toggle a no-op", async () => {
    renderEditor(row({ build: "shared", placement: "both" }));
    const owner = screen.getByRole("switch", {
      name: /Owner Library Recommended/i,
    });

    // Reachable: a native `disabled` button is skipped entirely by Tab.
    owner.focus();
    expect(owner).toHaveFocus();

    // A click can't actually flip it — the handler is a no-op for an unavailable cell.
    await userEvent.click(owner);
    expect(owner).toBeChecked();
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
      screen.getByText(/no share of your own for it to hide anything behind/i),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("switch", { name: /Friends Library Recommended/i }),
    );
    expect(
      screen.queryByText(
        /no share of your own for it to hide anything behind/i,
      ),
    ).toBeNull();
  });

  it("explains where an all-off row can still be found", async () => {
    renderEditor(row({ placement: "off", placement_friends: "off" }));
    expect(
      screen.getByText(/won.t appear on any Home screen or Recommended shelf/i),
    ).toBeInTheDocument();
  });

  it("warns that the Collections tab shows every person's row, whatever the switches say", async () => {
    // The question this answers came in cold from a v1 user: "why do I see everyone else's lists
    // too?" The shelf note below it only appears while Everyone else → Recommended shelf is on, so
    // an owner who found the rows in Plex's Collections tab had nothing on this page explaining it.
    renderEditor(row({ placement: "off", placement_friends: "off" }));
    expect(screen.getByText(/one row there per person/i)).toBeInTheDocument();

    // Still there with every switch on — it is not a consequence of any of them.
    cleanup();
    renderEditor(row({ placement: "both", placement_friends: "both" }));
    expect(screen.getByText(/one row there per person/i)).toBeInTheDocument();
  });

  it("does not claim a per-person Collections tab for a shared row", () => {
    // A shared row is ONE collection everybody gets, so "one row per person" would be a lie.
    renderEditor(row({ build: "shared", placement: "both" }));
    expect(screen.queryByText(/one row there per person/i)).toBeNull();
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
      screen.getByText(/Everyone else . Recommended shelf/i),
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
    expect(owner).toHaveAttribute("aria-disabled", "true");
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
      screen.queryByText(
        /no share of your own for it to hide anything behind/i,
      ),
    ).toBeNull();
  });
});

describe("RowEditor — rebuild cadence", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the cadence field only when the row overrides the global default", () => {
    renderEditor(row({ refresh_days: 11 }));
    expect(
      screen.getByRole("spinbutton", {
        name: /how often the row rebuilds/i,
      }),
    ).toHaveValue(11);
    expect(
      screen.getByRole("switch", { name: /global rebuild cadence/i }),
    ).not.toBeChecked();
  });

  it("stops inheriting at the global's own value, not at zero", async () => {
    // Turning "use the global" OFF should stop TRACKING the global, not change what the row does.
    // It used to snap to 0 — i.e. silently froze the row — which reads as a broken switch.
    renderEditor(row({ refresh_days: null }));

    await userEvent.click(
      screen.getByRole("switch", { name: /global rebuild cadence/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).refresh_days,
    ).toBe(8);
  });
});

describe("RowEditor — recent watches to search", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the number field only when the row overrides the global default", async () => {
    settingsData.current = { "candidates.sources": ["llm_web"] };
    renderEditor(row({ recent_count: 5 }));
    expect(
      await screen.findByLabelText(/Watches the AI web search looks up/i),
    ).toHaveValue(5);
    expect(
      screen.getByRole("switch", { name: /global recent-watches default/i }),
    ).not.toBeChecked();
  });

  it("round-trips a per-row recent_count into the PATCH body", async () => {
    settingsData.current = { "candidates.sources": ["llm_web"] };
    renderEditor(row({ recent_count: null }));
    await screen.findByRole("switch", {
      name: /global recent-watches default/i,
    });

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

describe("RowEditor — watches every source builds from", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the number field only when the row overrides the default", () => {
    renderEditor(row({ max_seeds: 3 }));
    expect(
      screen.getByLabelText(/^Watches every source builds from$/i),
    ).toHaveValue(3);
    expect(
      screen.getByRole("switch", {
        name: /default number of watches every source builds from/i,
      }),
    ).not.toBeChecked();
  });

  it("round-trips a per-row max_seeds into the PATCH body", async () => {
    renderEditor(row({ max_seeds: null, media: "movie" }));

    await userEvent.click(
      screen.getByRole("switch", {
        name: /default number of watches every source builds from/i,
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

  it("round-trips a per-row cold_start into the PATCH body", async () => {
    renderEditor(row({ cold_start: null }));

    await userEvent.click(
      screen.getByRole("switch", {
        name: /global setting for people without enough watch history/i,
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    // "skip", not the global's value: the only reason to reach for this control is to differ from
    // the global, and it is one flip away again.
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).cold_start,
    ).toBe("skip");
  });

  it("a row that inherits sends no cold_start override at all", async () => {
    // The regression that would pin every row to today's global: an editor that materialises
    // "popular" the moment it renders, so opening and saving a row silently ends its inheritance.
    renderEditor(row({ cold_start: null }));

    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).cold_start,
    ).toBeNull();
  });

  it("opens at 2, not 1, for a row covering movies AND TV", async () => {
    // Seeds are balanced across the media types present, so a budget of 1 yields ONE type — a
    // "both" row at 1 gathers nothing for its other half and that library never builds.
    renderEditor(row({ max_seeds: null, media: "both" }));

    await userEvent.click(
      screen.getByRole("switch", {
        name: /default number of watches every source builds from/i,
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
      screen.getByText(/blending their whole recent viewing/i),
    ).toBeInTheDocument();
    // A movies-only row has no other half to strand, so it must not get the both-media caveat.
    expect(
      screen.queryByText(/one of your two libraries would get nothing/i),
    ).not.toBeInTheDocument();
  });

  it("tells a movies-and-TV {top_seed} row why 1 would strand half of it", () => {
    renderEditor(
      row({
        name_template: "Because you watched {top_seed}",
        media: "both",
        max_seeds: 1,
      }),
    );
    expect(
      screen.getByText(/one of your two libraries would get nothing/i),
    ).toBeInTheDocument();
  });

  it("does not cry 'empty library' at a movies-and-TV row on the global default", () => {
    // At 30 seeds both media types get seeded, so the row builds in both libraries — its problem is
    // that the NAME won't match the contents, which is a different sentence. Saying a library would
    // get nothing there would be plain wrong.
    renderEditor(
      row({
        name_template: "Because you watched {top_seed}",
        media: "both",
        max_seeds: null,
      }),
    );

    expect(
      screen.queryByText(/one of your two libraries would get nothing/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/blending their whole recent viewing/i),
    ).toBeInTheDocument();
  });

  it("stays quiet for a row whose name makes no such promise", () => {
    renderEditor(row({ name_template: "{library_name} Picked for You" }));
    expect(
      screen.queryByText(/names one watch and fills itself/i),
    ).not.toBeInTheDocument();
  });
});

describe("RowEditor — how often it changes", () => {
  const cadenceBlock = () => screen.queryByText(/How often it changes/i);
  const cadenceToggle = () =>
    screen.queryByRole("switch", { name: /global rebuild cadence/i });

  it("drops the setting entirely on a row named after a watch", () => {
    // The engine runs these rows nightly whatever is stored, so there is no cadence to choose. It
    // was left as a slider, and the global default quietly made the row keep naming last week's film
    // (issue #57, reported twice). Replacing it with a heading and a paragraph explaining a control
    // that isn't there was just something else to read past — the section summary already says
    // "refreshes nightly".
    renderEditor(row({ name_template: "Because you watched {top_seed}" }));

    expect(cadenceBlock()).not.toBeInTheDocument();
    expect(cadenceToggle()).not.toBeInTheDocument();
  });

  it("drops it for a cycling row too, named or not", () => {
    // The engine forces nightly for `_names_a_seed(spec) OR seed_window > 1`. Gating the UI on only
    // the first left an unnamed cycling row showing a cadence field — and reporting "frozen" in
    // the section summary — while the engine ran it every night.
    renderEditor(
      row({ name_template: "Tonight's pick", max_seeds: 2, seed_window: 3 }),
    );

    expect(cadenceBlock()).not.toBeInTheDocument();
    expect(cadenceToggle()).not.toBeInTheDocument();
  });

  it("keeps it for a row that follows no watch", () => {
    // Scoped to rows that follow a watch. Everywhere else this is still a real choice, and removing
    // it would take away the only control over how often a row re-curates.
    renderEditor(row({ name_template: "{library_name} Picked for You" }));

    expect(cadenceBlock()).toBeInTheDocument();
    expect(cadenceToggle()).toBeInTheDocument();
  });
});

describe("RowEditor — which watch it follows", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("offers the cycle only to a row built from one or two watches", () => {
    // Above two the row is blending a history and has no single watch to follow, so the question
    // has no answer and asking it would be noise.
    renderEditor(row({ max_seeds: 1 }));
    expect(screen.getByText(/Which watch it follows/i)).toBeInTheDocument();

    cleanup();
    renderEditor(row({ max_seeds: 30 }));
    expect(
      screen.queryByText(/Which watch it follows/i),
    ).not.toBeInTheDocument();
  });

  it("says what the number means, and warns only once it actually cycles", () => {
    renderEditor(row({ max_seeds: 1, seed_window: 1 }));
    expect(
      screen.getByText(/Always the last thing they finished/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/writes to Plex most nights/i),
    ).not.toBeInTheDocument();

    cleanup();
    renderEditor(row({ max_seeds: 1, seed_window: 3 }));
    expect(
      screen.getByText(/Cycles through their last 3 watches/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/writes to Plex most nights/i)).toBeInTheDocument();
  });

  it("stops cycling when the budget grows past the control's range", async () => {
    // The control only renders for a 1..2-seed row. Widening the budget without clearing the window
    // left the row cycling — and forced to nightly rebuilds — with the control gone from the editor,
    // so there was nothing to see it by and no way to undo it.
    renderEditor(row({ max_seeds: 1, seed_window: 4 }));

    const budget = screen.getByLabelText(/^Watches every source builds from$/i);
    await userEvent.clear(budget);
    await userEvent.type(budget, "30");
    await userEvent.tab();

    expect(
      screen.queryByText(/Which watch it follows/i),
    ).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );
    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).seed_window,
    ).toBe(1);
  });

  it("round-trips the window into the PATCH body", async () => {
    renderEditor(row({ max_seeds: 1, seed_window: 1 }));

    const field = screen.getByLabelText(/Recent watches to choose from/i);
    await userEvent.clear(field);
    await userEvent.type(field, "3");
    await userEvent.tab();
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).seed_window,
    ).toBe(3);
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

describe("RowEditor — order", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("marks the row's current order as the pressed option", () => {
    renderEditor(row({ pick_order: "rating" }));

    expect(
      screen.getByRole("button", { name: "Highest rated" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Best match" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("names the rating service the server is configured for", async () => {
    // "Highest rated" alone does not say WHOSE score. The answer lives in a setting the owner may
    // never have opened, so the editor states it where the choice is made.
    settingsData.current = { "recommendations.rating_source": "imdb" };
    renderEditor(row({ pick_order: "rating" }));

    expect(
      await screen.findByText(/Highest IMDb score first/i),
    ).toBeInTheDocument();
  });

  it("explains what the chosen order does, and names shuffle's cost", async () => {
    renderEditor(row({ pick_order: "best" }));
    expect(
      screen.getByText(/Strongest suggestions first/i),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Shuffled" }));

    // The one order that rewrites the collection on Plex when nothing else changed — the owner
    // should not have to discover that from their run history.
    expect(screen.getByText(/different order every day/i)).toBeInTheDocument();
    expect(screen.getByText(/writes to Plex/i)).toBeInTheDocument();
  });

  // Every chip, not just one. The failure this catches is a label reaching the PATCH body as the
  // wrong value, and the six sit next to each other in one control — "Newest released" beside "Just
  // added", "Shuffled" beside "Taking turns" — which is exactly where a mix-up would land.
  it.each([
    ["Best match", "best"],
    ["Highest rated", "rating"],
    ["Newest released", "newest"],
    ["Shuffled", "shuffle"],
    ["Just added", "new_first"],
    ["Taking turns", "rotate"],
  ])("round-trips the %s order into the PATCH body", async (label, value) => {
    // Start on an order that is never the one under test, so a chip that silently fails to register
    // cannot pass by leaving the row on the value we are asserting.
    renderEditor(row({ pick_order: value === "best" ? "shuffle" : "best" }));

    await userEvent.click(screen.getByRole("button", { name: label }));
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).pick_order,
    ).toBe(value);
  });

  it("warns that taking turns writes to Plex on otherwise-unchanged days", async () => {
    renderEditor(row({ pick_order: "best" }));

    await userEvent.click(screen.getByRole("button", { name: "Taking turns" }));

    // Same disclosure "Shuffled" carries above: these are the only two orders that cost a Plex
    // write on a night the row itself did not change, and the owner should not have to find that
    // out from their run history.
    expect(
      screen.getByText(/front moves along by one title/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/writes to Plex/i)).toBeInTheDocument();
  });
});

describe("RowEditor — rating source is answerable where the order is chosen", () => {
  it("reveals the source only when the order actually uses one", async () => {
    // "Highest rated" raises "rated by whom?" at that moment. Answering it in Settings — a different
    // screen, under a different heading — is how the setting stayed undiscovered.
    settingsData.current = { "recommendations.rating_source": "imdb" };
    renderEditor(row({ pick_order: "best" }));
    expect(screen.queryByLabelText("Rated by")).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Highest rated" }),
    );

    expect(await screen.findByLabelText("Rated by")).toHaveValue("imdb");
  });
});

describe("RowEditor — every group is on screen, only the optional ones fold", () => {
  const groupNamed = (title: string) =>
    screen.getByText(title).closest("details");

  it("leaves the groups that decide what a row does open", () => {
    renderEditor(row());

    expect(screen.getByLabelText("Name", { exact: true })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Best match" })).toBeVisible();
    expect(screen.getByText("How many titles")).toBeVisible();

    // Open, because a page has room for them. As a modal these were collapsed to fit inside the
    // viewport cap — which is how the movies-and-TV seed warning ended up somewhere nobody looks.
    for (const group of ["The basics", "What goes in it", "Where it appears"]) {
      expect(groupNamed(group)).toHaveAttribute("open");
    }
  });

  it("folds only the two groups most people never touch", () => {
    renderEditor(row());

    for (const group of ["Artwork", "Requests"]) {
      expect(groupNamed(group)).not.toHaveAttribute("open");
    }
  });

  it("a folded group still says what is inside it", () => {
    // A disclosure that hides its contents AND what they are set to is worse than no disclosure.
    // Scoped to the group's own summary: the preview panel also reports the tag, so an unscoped
    // match would pass on the panel alone even if the summary said nothing.
    renderEditor(row({ request_tag: "family-picks" }));

    const requests = screen.getByText("Requests").closest("details");
    expect(requests).not.toHaveAttribute("open");
    expect(requests).toHaveTextContent(/family-picks/);
  });

  it("shows a warning that used to be buried in a collapsed group", () => {
    // The reason the accordions had to go. This advice decides whether a movies-and-TV row builds
    // at all, and it lived inside a section that started closed.
    renderEditor(
      row({
        name_template: "Because you watched {top_seed}",
        media: "both",
        max_seeds: 1,
      }),
    );

    expect(
      screen.getByText(/one of your two libraries would get nothing/i),
    ).toBeVisible();
  });
});

describe("RowEditor — a typed row says so", () => {
  it("summarises an empty library selection as that row's TYPE, not every library", async () => {
    // "[]" means every library OF THIS ROW'S TYPE. Saying "every library" on a movies row
    // contradicted the picker directly below it, which ticks only the movie ones.
    renderEditor(row({ media: "movie", library_keys: [] }));
    expect(screen.getByText(/every movie library/)).toBeInTheDocument();

    cleanup();
    renderEditor(row({ media: "show", library_keys: [] }));
    expect(screen.getByText(/every TV library/)).toBeInTheDocument();

    cleanup();
    renderEditor(row({ media: "both", library_keys: [] }));
    expect(screen.getByText(/every library/)).toBeInTheDocument();
  });

  it("names the rewatch switch after the row someone wants", () => {
    // The old label ("Lead with things they've seen") could only be understood by someone who had
    // already understood that the percentage above is a ceiling.
    renderEditor(row());
    expect(
      screen.getByLabelText("Make this a watch it again row"),
    ).toBeInTheDocument();
  });
});

describe("RowEditor — settings that would do nothing are not offered", () => {
  it("hides the AI-search cap on a row that doesn't use AI web search", async () => {
    // It caps ONE source's lookups. On a row without that source it changes nothing, and it sits
    // next to the row-wide seed budget, so leaving it on such a row read as a rival answer to the
    // same question.
    settingsData.current = { "candidates.sources": ["tmdb_similar"] };
    renderEditor(row({ recent_count: 5 }));

    expect(
      await screen.findByText("Watches every source builds from"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/Watches the AI web search looks up/i),
    ).not.toBeInTheDocument();
  });

  it("offers it once the row's own sources include AI web search", async () => {
    settingsData.current = { "candidates.sources": ["tmdb_similar"] };
    renderEditor(row({ recent_count: 5, candidate_sources: ["llm_web"] }));

    expect(
      await screen.findByLabelText(/Watches the AI web search looks up/i),
    ).toBeInTheDocument();
  });
});

describe("RowEditor — the outcome preview", () => {
  beforeEach(() => {
    settingsData.current = {};
  });

  it("resolves an inheriting row's cadence against the real global", async () => {
    // "Whatever the global default is" names the setting instead of its effect, which is the one
    // answer this panel must never give — it exists to say what the row will DO.
    settingsData.current = { "recommendations.refresh_days": 8 };
    renderEditor(row({ refresh_days: null, name_template: "Popular here" }));

    expect(await screen.findByText("Every 8 days")).toBeInTheDocument();
  });

  it("says every night for a row that follows a watch, whatever is stored", () => {
    settingsData.current = { "recommendations.refresh_days": 8 };
    renderEditor(
      row({ refresh_days: 0, name_template: "Because you watched {top_seed}" }),
    );

    expect(screen.getByText("Every night")).toBeInTheDocument();
  });

  it("shows what a templated name becomes, not the raw placeholder", () => {
    renderEditor(row({ name_template: "Because you watched {top_seed}" }));

    expect(screen.getByText(/“Because you watched Fargo”/)).toBeInTheDocument();
  });
});

describe("RowEditor — the preview tells the truth about who varies", () => {
  it("does not claim a SHARED row is named per person", () => {
    // A shared row is one Plex collection everybody sees. Only {library_name} moves.
    renderEditor(
      row({ build: "shared", name_template: "Popular {library_name} here" }),
    );

    expect(
      screen.queryByText(/each person gets their own name/i),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/each library gets its own/i)).toBeInTheDocument();
  });

  it("says a per-person row is named per person", () => {
    renderEditor(
      row({
        build: "per_person",
        name_template: "Because you watched {top_seed}",
      }),
    );

    expect(
      screen.getByText(/each person gets their own name/i),
    ).toBeInTheDocument();
  });
});

describe("RowEditor — the preview's sample library", () => {
  it("previews a TV-only row with a TV library, not a movie one", () => {
    // "More Movies to watch" on a shows-only row is a name it can never produce.
    renderEditor(
      row({ media: "show", name_template: "More {library_name} to watch" }),
    );

    expect(screen.getByText(/“More TV Shows to watch”/)).toBeInTheDocument();
  });
});

describe("RowEditor — only series they haven't started", () => {
  beforeEach(() => {
    updateCollection.mockClear();
    settingsData.current = {};
  });

  it("offers the switch on a films-and-shows row, not just a shows-only one", () => {
    // The bug: gated on `media === "show"`, so the default "both" row — the one most installs have —
    // could never turn this on, even though the API accepts it and the engine honours it. The API
    // refuses exactly one combination, movies-only, and the control now matches that.
    renderEditor(row({ media: "both" }));

    expect(
      screen.getByRole("switch", {
        name: /only series they have not started/i,
      }),
    ).toBeInTheDocument();
  });

  it("hides it on a movies-only row, which is the one thing the API refuses", () => {
    renderEditor(row({ media: "movie" }));

    expect(
      screen.queryByRole("switch", {
        name: /only series they have not started/i,
      }),
    ).not.toBeInTheDocument();
  });

  it("says the switch only matters once already-watched titles are allowed", () => {
    // Since 1.2 a 0% row drops started shows anyway, so the old copy ("normally only finished shows
    // are skipped") described a rule that no longer exists.
    renderEditor(row({ media: "show" }));

    expect(
      screen.getByText(
        /only changes anything if you.*allowed already-watched titles/i,
      ),
    ).toBeInTheDocument();
  });

  it("round-trips the flag on a both row into the PATCH body", async () => {
    renderEditor(row({ media: "both", unstarted_only: false }));

    await userEvent.click(
      screen.getByRole("switch", {
        name: /only series they have not started/i,
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const call = updateCollection.mock.calls.at(0);
    expect((call?.[1] as Collection).unstarted_only).toBe(true);
  });
});

describe("RowEditor — recent releases", () => {
  beforeEach(() => {
    updateCollection.mockClear();
    settingsData.current = {};
  });

  it("shows the slider only when the row overrides the global default", () => {
    renderEditor(row({ recency: 0.8 }));
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toHaveValue("80");
    expect(
      screen.getByRole("switch", { name: /global recent-releases default/i }),
    ).not.toBeChecked();
  });

  it("hides the slider while the row is inheriting", () => {
    renderEditor(row({ recency: null }));
    expect(
      screen.queryByRole("slider", { name: /release date counts/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: /global recent-releases default/i }),
    ).toBeChecked();
  });

  it("stops inheriting at the global's own value, not at zero", async () => {
    // Same trap freshness hit: dropping to 0 on un-inherit silently changes what the row does, so
    // the switch reads as broken. Here it would ALSO be indistinguishable from a deliberate
    // "any era" row, which is a real setting someone might have chosen.
    settingsData.current = { "recommendations.recency": 0.6 };
    renderEditor(row({ recency: null }));

    await userEvent.click(
      screen.getByRole("switch", { name: /global recent-releases default/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect((updateCollection.mock.calls.at(0)?.[1] as Collection).recency).toBe(
      0.6,
    );
  });

  it("offers the control on a row that follows a watch, where freshness is withheld", async () => {
    // A `{top_seed}` row is forced to a nightly cadence, so the editor hides its freshness slider.
    // Which titles win is still a free choice there, so this control must NOT be hidden with it.
    renderEditor(
      row({ name_template: "Because you watched {top_seed}", recency: 0.5 }),
    );

    expect(
      screen.queryByRole("switch", { name: /global rebuild cadence/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toBeInTheDocument();
  });
});

describe("RowEditor — a shared row hides the dials that do not apply to it", () => {
  beforeEach(() => {
    settingsData.current = {};
  });

  const perPersonOnly = [
    /global already-watched default/i,
    /global rebuild cadence/i,
    /watch it again/i,
    /not started/i,
    /global setting for people without enough watch history/i,
  ];

  it("offers them on a per-person row", () => {
    // The control: without it, the shared-row assertions below could pass on a broken editor.
    renderEditor(row({ build: "per_person" }));
    for (const name of perPersonOnly) {
      expect(screen.getByRole("switch", { name })).toBeInTheDocument();
    }
  });

  it("hides them on a shared row, rather than promising what the engine ignores", () => {
    // `_shared_row` never calls `_apply_watched_cap`, `_prefer_watched` or `_is_refresh_night`, and
    // cold-start is meaningless for a row built from aggregate history. Showing a control the
    // engine drops is worse than showing none — it states a behaviour.
    renderEditor(row({ build: "shared", min_watchers: 2 }));
    for (const name of perPersonOnly) {
      expect(screen.queryByRole("switch", { name })).not.toBeInTheDocument();
    }
  });

  it("hides every search control, because a shared row does not search", () => {
    // A shared row is a straight tally of the server's most-watched titles. Sources, the seed budget
    // and the AI web-search dials were all inert here the moment it stopped searching, and a control
    // the engine ignores is worse than no control — it promises a behaviour.
    renderEditor(row({ build: "shared", min_watchers: 2 }));

    expect(
      screen.queryByText(/sources you enabled in Settings/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/most-watched titles, most watched first/i),
    ).toBeInTheDocument();
  });

  it("still offers those controls on a PER-PERSON row", () => {
    // The matrix cell that keeps the fix honest: hiding them for everyone would gut the per-person
    // row, which is where they all still do something.
    renderEditor(row({ build: "per_person" }));

    expect(
      screen.queryByText(/most-watched titles, most watched first/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/sources you enabled in Settings/i),
    ).toBeInTheDocument();
  });

  it("keeps the controls a shared row DOES honour", () => {
    // Sources, libraries and display order all work on the shared path — hiding those would remove
    // real function.
    renderEditor(row({ build: "shared", min_watchers: 2 }));
    expect(screen.getByRole("group", { name: /order/i })).toBeInTheDocument();
  });

  it("hides the release-date weight, which a shared row has nothing to apply it to", () => {
    // Recency weights a title's release date inside a SCORED CANDIDATE POOL. A shared row is the
    // server's most-watched titles ranked by how many people watched them, so there is no pool and
    // no score for the weight to act on — offering a dial that silently does nothing is the bug
    // this row type already had with `watched_pct` and `rewatch`. "Newest first" is how a shared
    // row leans modern now.
    renderEditor(row({ build: "shared", min_watchers: 2 }));
    expect(
      screen.queryByRole("switch", { name: /global recent-releases default/i }),
    ).not.toBeInTheDocument();
  });
});

describe("RowPreview — what a shared row says it will do", () => {
  // The panel exists to explain the row, so naming behaviour the engine no longer has is worse than
  // leaving a line out. A shared row shows what people HAVE watched, pools everyone's viewing,
  // searches nothing, and is ordered by watcher count.
  it("describes the tally, not the search it used to run", () => {
    renderEditor(row({ build: "shared", min_watchers: 2 }));

    expect(
      screen.getByText("What people here have watched most"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/pooled — no search, no AI/i),
    ).toBeInTheDocument();
    expect(screen.getByText("Most watched first")).toBeInTheDocument();
    expect(screen.queryByText("Found via")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Only things they haven.t seen/i),
    ).not.toBeInTheDocument();
  });

  it("leaves a per-person row's summary alone", () => {
    renderEditor(row({ build: "per_person" }));

    expect(screen.getByText("Found via")).toBeInTheDocument();
    expect(
      screen.queryByText("What people here have watched most"),
    ).not.toBeInTheDocument();
  });
});
