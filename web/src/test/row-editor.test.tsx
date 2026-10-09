import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowEditor } from "@/components/rows/row-editor";
import type * as ApiModule from "@/lib/api";
import { overrideName } from "@/test/override-name";
import { toInput } from "@/lib/collections";
import type { Collection, User } from "@/lib/types";
import { BUILTINS } from "@/test/season-fixtures";

const { updateCollection, settingsData, startRun, scheduleData, privacyData, effectivenessData, librariesData } = vi.hoisted(() => ({
  librariesData: { current: [] as unknown[] },
  // null = that endpoint fails, which is what every test that doesn't set one gets.
  scheduleData: { current: null as unknown },
  privacyData: { current: null as unknown },
  effectivenessData: { current: null as unknown },
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
      getLibraries: () => Promise.resolve(librariesData.current),
      getSeasons: () => Promise.resolve(BUILTINS),
      getSeasonPresets: () => Promise.resolve([]),
      getImageProvider: () =>
        Promise.resolve({ capable: false, provider: "", reason: "" }),
      startRun: (body: unknown) => startRun(body),
      getSchedule: () =>
        scheduleData.current ? Promise.resolve(scheduleData.current) : Promise.reject(new Error("no schedule")),
      getPrivacyStatus: () =>
        privacyData.current ? Promise.resolve(privacyData.current) : Promise.reject(new Error("no privacy read")),
      getCollectionEffectiveness: () =>
        effectivenessData.current
          ? Promise.resolve(effectivenessData.current)
          : Promise.reject(new Error("no history")),
    },
  };
});

function row(patch: Partial<Collection> = {}): Collection {
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
    schedule: "30 3 * * *",
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    fallback_name: "",
    description: "",
    sort_title_prefix: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    rewatch: false,
    rewatch_cooldown_days: 30,
    requests_row: false,
    requests_window_days: 90,
    requests_tag_pattern: "",
    unstarted_only: false,
    refresh_days: null,
    idle_hold_days: null,
    recency: null,
    recent_count: null,
    max_seeds: null,
    max_runtime: null,
    min_year: null,
    max_year: null,
    min_rating: null,
    cold_start: null,
    req_min_rating: null,
    req_min_votes: null,
    req_min_demand: null,
    req_min_year: null,
    req_max_year: null,
    req_auto_send: null,
    req_auto_min_demand: null,
    req_auto_min_rating: null,
    req_max_per_row: null,
    req_radarr_quality_profile_id: null,
    req_radarr_root_folder: null,
    req_sonarr_quality_profile_id: null,
    req_sonarr_root_folder: null,
    req_sonarr_monitor: null,
    req_language_mode: null,
    req_preferred_languages: null,
    req_min_rating_other: null,
    seed_window: 1,
    pick_order: "best",
    placement: "both",
    placement_friends: "both",
    show_days: [],
    shown_today: true,
    seasons: [],
    season_lead_days: 30,
    season_after_days: 0,
    season_status: null,
    pin_top: false,
    hub_anchor: {},
    poster: { mode: "", title: "", subtitle: "", style: "", has_image: false },
    ai_instructions: { mode: "default", text: "" },
    theme_id: null,
    theme_name: null,
    theme_emoji: null,
    ai_paused: false,
    ai_tokens: 0,
    theme_mode: "fixed",
    explore_brief: "",
    theme_days: null,
    refresh_share: null,
    repeat_cooldown_days: null,
    avoid_rows: null,
    ...patch,
  };
}

function user(patch: Partial<User> = {}): User {
  return {
    manage_sharing: true,
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
    requested_by_tag: "",
    picks_watched_30d: null,
    last_pick_watched_at: null,
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

function renderEditor(collection: Collection, users: User[] = [], expand = true) {
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
  if (expand) document.querySelectorAll<HTMLDetailsElement>("details[data-settings-group], details[data-setting='kind']").forEach((group) => { if (group.dataset.settingsGroup !== "Requests") group.open = true; });
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

  it("turns the row off from here, instead of sending you to the Rows page for it", async () => {
    // The editor's own copy used to say "use the toggle on the Rows page" — a page change to do the
    // one reversible thing to the row you already have open.
    renderEditor(row());

    const toggle = await screen.findByRole("switch", {
      name: /Enable Hidden Gems/i,
    });
    await userEvent.click(toggle);

    // Turning OFF confirms first: the consequence is invisible and deferred until the next run.
    expect(
      await screen.findByRole("heading", { name: /Turn off/i }),
    ).toBeInTheDocument();
  });

  it("points at removing and deleting rather than leaving them below the fold", async () => {
    // They stay fenced off at the bottom — they reach into other people's Plex and Discard does not
    // undo them — but fenced off had become invisible: nothing on the first screen said they exist.
    // The jump list, on the first screen, names the Danger zone.
    renderEditor(row());

    const nav = screen.getByRole("navigation", { name: "Row settings sections" });
    expect(within(nav).getByRole("link", { name: "Danger zone" })).toHaveAttribute("href", "#danger-zone");
  });

  it("warns that Run rebuilds the SAVED row once the form has been edited", async () => {
    // Run is scoped to the stored row, so pressing it mid-edit silently rebuilds without your
    // changes — and nothing on the button says so.
    renderEditor(row());

    await screen.findByRole("button", { name: /Run Hidden Gems now/i });
    expect(screen.queryByText(/unsaved changes/i)).toBeNull();

    // The "watch it again" switch this used to flip is now a kind; switching to it is the same edit.
    await userEvent.click(screen.getByRole("radio", { name: "Watch it again" }));
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));

    expect(await screen.findByText(/unsaved changes/i)).toBeInTheDocument();
  });
});

describe("RowEditor — Live on Plex and the save bar", () => {
  beforeEach(() => {
    settingsData.current = {};
    updateCollection.mockClear();
  });

  it("puts the on/off switch in Live on Plex, and it still writes straight away", async () => {
    renderEditor(row());
    const live = screen.getByRole("region", { name: "Live on Plex" });
    expect(within(live).getByText("Changes here apply to Plex immediately.")).toBeInTheDocument();

    await userEvent.click(within(live).getByRole("switch", { name: /Enable Hidden Gems/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Turn it off" }));

    // No Save pressed: the switch PATCHes the whole saved row with only `enabled` flipped.
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect(updateCollection).toHaveBeenCalledWith(1, { ...toInput(row()), enabled: false });
  });

  it("holds a draft edit in the save bar, naming the change, until Save sends it", async () => {
    renderEditor(row());
    const bar = screen.getByRole("region", { name: "Unsaved changes" });
    expect(within(bar).queryByText(/unsaved change/)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Shuffled" }));

    expect(within(bar).getByText("1 unsaved change")).toBeInTheDocument();
    expect(bar).toHaveTextContent("Order: Best match → Shuffled");
    expect(updateCollection).not.toHaveBeenCalled();

    await userEvent.click(within(bar).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect(updateCollection).toHaveBeenCalledWith(1, { ...toInput(row()), pick_order: "shuffle" });
  });

  it("names an AI instructions change in the save bar and sends it on Save", async () => {
    renderEditor(row({ candidate_sources: ["llm_web"] }));
    const bar = screen.getByRole("region", { name: "Unsaved changes" });

    await userEvent.click(screen.getByRole("button", { name: "Add to the default" }));
    expect(bar).toHaveTextContent("AI instructions: Use the default → Add to the default");
    await userEvent.type(screen.getByRole("textbox", { name: "Also tell the AI" }), "No sequels.");

    await userEvent.click(within(bar).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect(updateCollection).toHaveBeenCalledWith(1, {
      ...toInput(row({ candidate_sources: ["llm_web"] })),
      ai_instructions: { mode: "add", text: "No sequels." },
    });
  });

  it("counts no change after adding instructions and switching back to the default, and keeps the text", async () => {
    renderEditor(row({ candidate_sources: ["llm_web"] }));
    const bar = screen.getByRole("region", { name: "Unsaved changes" });

    await userEvent.click(screen.getByRole("button", { name: "Add to the default" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Also tell the AI" }), "x");
    expect(within(bar).getByText("1 unsaved change")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Use the default" }));
    expect(within(bar).queryByText(/unsaved change/)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Add to the default" }));
    expect(screen.getByRole("textbox", { name: "Also tell the AI" })).toHaveValue("x");
  });

  it("counts no change after switching a row's instructions to Write your own and back", async () => {
    renderEditor(row({ candidate_sources: ["llm_web"], ai_instructions: { mode: "add", text: "No sequels." } }));
    const bar = screen.getByRole("region", { name: "Unsaved changes" });

    await userEvent.click(screen.getByRole("button", { name: "Write your own" }));
    expect(within(bar).getByText("1 unsaved change")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add to the default" }));
    expect(within(bar).queryByText(/unsaved change/)).toBeNull();
  });

  it("throws the draft away on Discard without saving anything", async () => {
    renderEditor(row());
    const bar = screen.getByRole("region", { name: "Unsaved changes" });
    await userEvent.type(screen.getByLabelText("Description"), "Picked nightly");
    expect(within(bar).getByText("1 unsaved change")).toBeInTheDocument();

    await userEvent.click(within(bar).getByRole("button", { name: "Discard" }));

    expect(screen.getByLabelText("Description")).toHaveValue("");
    expect(within(bar).queryByText(/unsaved change/)).toBeNull();
    expect(updateCollection).not.toHaveBeenCalled();
  });

  it("keeps the name read-only, changed only through Rename on Plex", async () => {
    renderEditor(row());
    const looks = screen.getByRole("region", { name: "Name & look" });
    expect(within(looks).queryByRole("textbox", { name: "Name" })).toBeNull();
    expect(within(looks).getByText("Changed with Rename on Plex")).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Live on Plex" })).getByRole("button", { name: "Rename on Plex…" }),
    ).toBeEnabled();
  });

  it("sums up the row's record in one line, judging nothing before its picks have had their time", async () => {
    effectivenessData.current = {
      delivered: 60,
      watched: 0,
      finished: 0,
      first_delivered_at: "2026-09-20T02:30:00Z",
      last_delivered_at: "2026-09-28T02:30:00Z",
      matured: null,
      matured_days: 30,
      per_library: [],
      runs: 3,
    };
    try {
      renderEditor(row());
      const live = screen.getByRole("region", { name: "Live on Plex" });
      expect(await within(live).findByText(/titles delivered/)).toBeInTheDocument();
      expect(live).toHaveTextContent("60 titles delivered");
      expect(live).toHaveTextContent("0 watched so far, a pick is judged once it’s had 30 days");
      expect(within(live).getByRole("link", { name: "See runs →" })).toHaveAttribute("href", "/runs?row=hidden-gems");
    } finally {
      effectivenessData.current = null;
    }
  });

  it("says when it next runs, from the scheduler when the schedule on screen is the saved one", async () => {
    const next = new Date();
    next.setDate(next.getDate() + 1);
    next.setHours(2, 30, 0, 0);
    scheduleData.current = {
      jobs: [],
      rows: [{ cron: "30 2 * * *", next_run: next.toISOString(), rows: [{ id: 1, name: "Hidden Gems", slug: "hidden-gems" }], type: "cron" }],
    };
    try {
      renderEditor(row({ schedule: "30 2 * * *" }));
      const when = next.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
      expect(await within(screen.getByRole("region", { name: "Schedule" })).findByText(`It runs tomorrow at ${when}.`, { exact: false })).toBeInTheDocument();
    } finally {
      scheduleData.current = null;
    }
  });

  it("resolves an inheriting row's cadence against the real global in the Next line", async () => {
    settingsData.current = { "recommendations.refresh_days": 8 };
    renderEditor(row({ refresh_days: null, schedule: "30 3 * * *" }));
    const schedule = screen.getByRole("region", { name: "Schedule" });

    expect(
      await within(schedule).findByText(/Its titles change on a cycle of 8 days \(the global default\)\./),
    ).toHaveTextContent("Next: It runs every day at 3:30 AM, Shortlist's clock.");
  });

  it("says every night for a row that follows a watch, whatever cadence is stored", () => {
    settingsData.current = { "recommendations.refresh_days": 8 };
    renderEditor(row({ refresh_days: 0, name_template: "Because you watched {top_seed}" }));

    expect(
      within(screen.getByRole("region", { name: "Schedule" })).getByText(/Its titles change every night, because it follows their latest watch/),
    ).toBeInTheDocument();
  });

  it("names each person it reaches and whether their account hides other rows, leaving the owner to the caveat", async () => {
    privacyData.current = {
      accounts: [
        { account_id: 11, user_id: 2, state: "hiding", restriction_profile: "" },
        { account_id: 12, user_id: 3, state: "missing", restriction_profile: "" },
      ],
      enforcement: {},
      error: null,
      read_at: "2026-10-03T00:00:00Z",
      rows_error: null,
      rows_on_plex: [],
      summary: "attention",
    };
    try {
      renderEditor(row(), [
        user({ id: 1, username: "owner", user_type: "owner" }),
        user({ id: 2, username: "sarah" }),
        user({ id: 3, username: "mike" }),
        user({ id: 4, username: "paused", prefs: { paused: true } }),
      ]);
      const who = screen.getByRole("region", { name: "Who gets it" });
      const rows = within(who).getAllByRole("row").slice(1);

      expect(rows.map((line) => within(line).getAllByRole("cell")[0]!.textContent)).toEqual([
        expect.stringContaining("sarah"),
        expect.stringContaining("mike"),
      ]);
      expect(await within(rows[0]!).findByText("Yes")).toBeInTheDocument();
      expect(within(rows[1]!).getByRole("link", { name: /No, hide rules missing/ })).toHaveAttribute("href", "/privacy");
      expect(within(who).getByText(/Plex can.t restrict the owner/)).toBeInTheDocument();
    } finally {
      privacyData.current = null;
    }
  });

  it("keeps delete out of the header: the Danger zone is the only way to it", () => {
    renderEditor(row());
    const danger = screen.getByRole("region", { name: "Danger zone" });
    expect(within(danger).getByRole("button", { name: "Delete Hidden Gems" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Delete/ })).toHaveLength(1);
  });
});

describe("RowEditor — a name that needs a watch", () => {
  it("asks what to call the row for people who have watched nothing", async () => {
    // Issue #84. `{top_seed}` needs a title the person has watched; someone new to the server has
    // none, so the row has no name for them. Shortlist used to invent one — a hardcoded English
    // "✨ Picked for You" that ignored the operator's own settings and claimed a watch that never
    // happened. It no longer does, which makes this a decision the operator has to be ABLE to make,
    // beside the name that creates the question rather than discovered from Plex days later.
    renderEditor(row({ name: "Car vous avez regardé {top_seed}" }));

    expect(
      await screen.findByLabelText(/Name for someone who.s new/i),
    ).toBeInTheDocument();
  });

  it("says plainly that leaving it empty means those people get no row", async () => {
    // The empty state is the DEFAULT and it is the consequential one — it decides whether ~19 of 22
    // people on a real server get this row at all. It has to read as a choice, not a blank field.
    renderEditor(row({ name: "Because you watched {top_seed}" }));

    await screen.findByLabelText(/Name for someone who.s new/i);
    expect(screen.getByText(/won.t get\s+this row/i)).toBeInTheDocument();
  });

  it("stays out of the way for a row whose name never needs one", async () => {
    renderEditor(row({ name: "✨ {library_name} Picked for You" }));

    await screen.findByLabelText(/^row name$/i).catch(() => null);
    expect(
      screen.queryByLabelText(/Name for someone who.s new/i),
    ).not.toBeInTheDocument();
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
      screen.getByRole("button", { name: overrideName(/already-watched/) }),
    ).toHaveTextContent("Reset");
  });

  it("hides the slider and checks the switch when the row inherits the global cap", () => {
    renderEditor(row({ watched_pct: null }));
    expect(
      screen.queryByRole("slider", { name: /already-watched/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: overrideName(/already-watched/) }),
    ).toHaveTextContent("Override");
  });

  it("round-trips a per-row watched cap into the PATCH body", async () => {
    renderEditor(row({ watched_pct: null }));

    // Turn off "use global default" to reveal the slider (starts at 0%).
    await userEvent.click(
      screen.getByRole("button", { name: overrideName(/already-watched/) }),
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
  const openRename = () => userEvent.click(screen.getByRole("button", { name: "Rename on Plex…" }));
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
    await openRename();
    const input = screen.getByDisplayValue("✨ {library_name} Picked for You");
    expect(input).toBeEnabled();

    await userEvent.type(input, "!");

    // The warning is the whole point of letting the box be editable: a name typed here has changed
    // nothing on Plex yet, and Save on this page will not apply it either.
    expect(await screen.findByText(/Not applied yet — press/)).toHaveTextContent(
      /Not applied yet/i,
    );
    expect(screen.getByRole("button", { name: /Rename/ })).toBeEnabled();
  });

  it("lists the {} placeholders beside an existing row's name, before and after you type", async () => {
    // Only a NEW row's name box said {user}/{library_name}/{top_seed} could be used. An existing row's
    // said "Renaming rewrites this row on Plex…" and nothing else, so the placeholders were invisible
    // exactly where a rename is typed.
    renderEditor(defaultRow());
    await openRename();
    // The hint is one sentence built from several spans, so match the paragraph as a whole.
    const hint = () =>
      screen.getByText(
        (_, el) => el?.tagName === "P" && /for the Plex library the row lands in/.test(el.textContent ?? ""),
      );
    expect(hint()).toHaveTextContent("{user}");
    expect(hint()).toHaveTextContent("{top_seed}");
    expect(screen.getByText(/Renaming rewrites this row on Plex/)).toBeInTheDocument();

    await userEvent.type(screen.getByDisplayValue("✨ {library_name} Picked for You"), "!");

    expect(await screen.findByText(/Not applied yet — press/)).toHaveTextContent(/Not applied yet/i);
    expect(hint()).toHaveTextContent("{library_name}");
  });

  it("keeps Rename disabled until the name actually changes", async () => {
    // Enabled on an unchanged name it offered to rewrite every collection on Plex, for every
    // person, to the name they already had — minutes of writes for no change at all.
    renderEditor(defaultRow());
    await openRename();
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
    await openRename();
    await userEvent.type(
      screen.getByDisplayValue("✨ {library_name} Picked for You"),
      " CHANGED",
    );
    await userEvent.keyboard("{Escape}");
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
        name: /titles refresh every, in days/i,
      }),
    ).toHaveValue(11);
    expect(
      screen.getByRole("button", { name: overrideName(/refresh every/) }),
    ).toHaveTextContent("Reset");
  });

  it("stops inheriting at the global's own value, not at zero", async () => {
    // Turning "use the global" OFF should stop TRACKING the global, not change what the row does.
    // It used to snap to 0 — i.e. silently froze the row — which reads as a broken switch.
    renderEditor(row({ refresh_days: null }));

    await userEvent.click(
      screen.getByRole("button", { name: overrideName(/refresh every/) }),
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

describe("RowEditor — idle hold", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the row's own ceiling when it overrides the global", () => {
    renderEditor(row({ idle_hold_days: 21 }));
    expect(
      screen.getByRole("spinbutton", {
        name: /hold a row for an inactive viewer/i,
      }),
    ).toHaveValue(21);
    expect(
      screen.getByRole("button", { name: overrideName(/hold when/) }),
    ).toHaveTextContent("Reset");
  });

  it("still offers the hold on a row that names its seed", async () => {
    // The engine DOES hold a `{top_seed}` row: its cadence is forced nightly so it keeps answering
    // to the watch it names, but the seed is drawn from history, so nobody who watched nothing can
    // have moved it. Hiding the control here would repeat, in mirror image, the exact bug the
    // cadence field shipped once — an editor promising one thing while the engine does another
    // (issue #57). It is also the row the wizard creates, so hiding it guts the feature.
    renderEditor(row({ name_template: "Because you watched {top_seed}" }));
    expect(
      screen.getByRole("button", { name: overrideName(/hold when/) }),
    ).toBeInTheDocument();
    // ...while the CADENCE control stays hidden for it, which is a different question.
    expect(
      screen.queryByRole("button", { name: overrideName(/refresh every/) }),
    ).not.toBeInTheDocument();
  });

  it("does not call the hold a no-op on a row the engine rebuilds nightly", async () => {
    // The row the hold exists for, and the cell the warning got wrong. `effective_refresh_days`
    // FORCES a `{top_seed}` row to nightly whatever its stored cadence says — which is exactly why
    // an 8-day hold on it is a real 7-night hold. Judging it against the stored 8 called it a no-op
    // and told the owner to raise a setting that was already working, quoting back a cadence the
    // engine documents as ignored (and whose control is hidden on this very row).
    settingsData.current = {
      "recommendations.refresh_days": 8,
      "recommendations.recency": 0.5,
    };
    renderEditor(
      row({
        name_template: "Because you watched {top_seed}",
        idle_hold_days: 8,
      }),
    );

    // Wait for something ONLY rendered once the settings query resolves — an inherited field's
    // "global is …" hint. Awaiting the spinbutton is not enough: it renders from `input` regardless,
    // so the absence assertion below would pass simply because no cadence had loaded yet, which is
    // exactly how this test first went green against unfixed code.
    expect(
      await screen.findByText(/leans towards recent releases/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("spinbutton", {
        name: /hold a row for an inactive viewer/i,
      }),
    ).toHaveValue(8);
    expect(screen.queryByText(/no effect/i)).not.toBeInTheDocument();
  });

  it("does call it a no-op on an ordinary row the cadence already beats", async () => {
    // The control cell: same numbers, but a row whose stored cadence is the one the engine uses.
    settingsData.current = { "recommendations.refresh_days": 8 };
    renderEditor(row({ idle_hold_days: 8 }));
    expect(await screen.findByText(/no effect/i)).toBeInTheDocument();
  });

  it("hides the hold on a row that cycles its seed", async () => {
    // That rotation advances one watch per rebuild by design, driven by the cadence and not by new
    // watches, so `effective_idle_hold_days` forces it off however it is set. A control the engine
    // overrides must not be on screen.
    renderEditor(row({ seed_window: 3 }));
    expect(
      screen.queryByRole("button", { name: overrideName(/hold when/) }),
    ).not.toBeInTheDocument();
  });

  it("stops inheriting at the global's own value, not at zero", async () => {
    // Same trap as the cadence switch beside it: snapping to 0 would silently turn the hold OFF for
    // this row, which reads as the switch doing something it did not say it would.
    settingsData.current = { "recommendations.idle_hold_days": 30 };
    renderEditor(row({ idle_hold_days: null }));

    await userEvent.click(screen.getByRole("button", { name: overrideName(/hold when/) }));
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).idle_hold_days,
    ).toBe(30);
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
      await screen.findByLabelText(/^Watches the AI web search looks up$/i),
    ).toHaveValue(5);
    expect(
      screen.getByRole("button", { name: overrideName(/watches the ai web search/) }),
    ).toHaveTextContent("Reset");
  });

  it("round-trips a per-row recent_count into the PATCH body", async () => {
    settingsData.current = { "candidates.sources": ["llm_web"] };
    renderEditor(row({ recent_count: null }));
    await screen.findByRole("button", { name: overrideName(/watches the ai web search/) });

    await userEvent.click(
      screen.getByRole("button", { name: overrideName(/watches the ai web search/) }),
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

describe("RowEditor — how many recent watches to match", () => {
  beforeEach(() => {
    updateCollection.mockClear();
    settingsData.current = {};
  });

  it("shows the number field only when the row overrides the default", () => {
    renderEditor(row({ max_seeds: 3 }));
    expect(
      screen.getByLabelText(/^How many recent watches to match$/i),
    ).toHaveValue(3);
    expect(
      screen.getByRole("button", { name: overrideName(/how many recent watches/) }),
    ).toHaveTextContent("Reset");
  });

  it("round-trips a per-row max_seeds into the PATCH body", async () => {
    // Making a "Because you watched X" row honest — 1 watch on a movies-only row — used to be done by
    // turning this switch off. It is now the Because you watched row's own "Based on" choice.
    renderEditor(
      row({
        name_template: "Because you watched {top_seed}",
        max_seeds: null,
        media: "movie",
      }),
    );

    await userEvent.click(
      screen.getByRole("radio", { name: "Their latest film" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).max_seeds,
    ).toBe(1);
  });

  it("stops inheriting a Picked for You row's count at the global, so it stays Picked for You", async () => {
    // 1 or 2 would make it a Because you watched row, which only the kind picker should do.
    settingsData.current = { "recommendations.max_seeds": 25 };
    renderEditor(row({ max_seeds: null, media: "movie" }));

    await userEvent.click(
      await screen.findByRole("button", { name: overrideName(/how many recent watches/) }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).max_seeds,
    ).toBe(25);
  });

  it("round-trips a per-row cold_start into the PATCH body", async () => {
    renderEditor(row({ cold_start: null }));

    await userEvent.click(
      screen.getByRole("button", { name: overrideName(/when someone hasn’t watched enough/) }),
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
    // "both" row at 1 gathers nothing for its other half and that library never builds. Making a
    // row about one watch is now the switch to Because you watched.
    renderEditor(row({ max_seeds: null, media: "both" }));

    await userEvent.click(
      screen.getByRole("radio", { name: "Because you watched" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).max_seeds,
    ).toBe(2);
  });

  it("tells a {top_seed} blend that its name mentions only one of its watches", () => {
    // Neutral help now, not an amber "Set it to 1" (owner-approved mockup): a blend is a real choice.
    renderEditor(
      row({ name_template: "Because you watched {top_seed}", media: "movie" }),
    );
    expect(
      screen.getByText(/but the name only mentions the latest one/i),
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
      screen.getByText(/but the name only mentions the latest one/i),
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
  const cadenceBlock = () => screen.queryByText(/Titles refresh every/i);
  const cadenceToggle = () =>
    screen.queryByRole("button", { name: overrideName(/refresh every/) });

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
    expect(
      screen.getByLabelText(/Take turns between their last/i),
    ).toBeEnabled();

    cleanup();
    renderEditor(row({ max_seeds: 30 }));
    expect(
      screen.queryByLabelText(/Take turns between their last/i),
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
    // Named after a watch: only then does a blend stay a Because you watched row (`rowKindOf`).
    renderEditor(
      row({
        name_template: "Because you watched {top_seed}",
        max_seeds: 1,
        seed_window: 4,
      }),
    );

    // The budget is "Based on" on a Because you watched row; a blend is the wider budget.
    await userEvent.click(
      screen.getByRole("radio", { name: /A blend of their last/i }),
    );

    expect(
      screen.getByLabelText(/Take turns between their last/i),
    ).toBeDisabled();
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

    const field = screen.getByLabelText(/Take turns between their last/i);
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
    expect(within(screen.getByRole("region", { name: "Name & look" })).getByText(/Sarah's Picks/)).toBeInTheDocument();
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
    expect(screen.queryByLabelText("Rated by · global setting")).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Highest rated" }),
    );

    expect(await screen.findByLabelText("Rated by · global setting")).toHaveValue("imdb");
    expect(screen.getByRole("link", { name: "Global row defaults" })).toHaveAttribute("href", "/settings#defaults");
  });
});

describe("RowEditor — one page of sections, one way around it", () => {
  const SECTIONS = [
    ["Name & look", "name-and-look"],
    ["Who gets it", "who-gets-it"],
    ["What goes in", "what-goes-in"],
    ["Schedule", "schedule"],
    ["Placement", "placement"],
    ["Requests", "requests"],
    ["Danger zone", "danger-zone"],
  ] as const;
  const section = (name: string) => screen.getByRole("region", { name });

  it("lists the seven sections in order, each a link to that section on the page", () => {
    renderEditor(row(), [], false);
    const nav = screen.getByRole("navigation", { name: "Row settings sections" });
    const links = within(nav).getAllByRole("link");

    // The first text node is the label; a hint such as "1 override" follows it in its own span.
    expect(links.map((link) => link.firstChild?.textContent)).toEqual(SECTIONS.map(([label]) => label));
    expect(links.map((link) => link.getAttribute("href"))).toEqual(SECTIONS.map(([, id]) => `#${id}`));
    const regions = SECTIONS.map(([label, id]) => {
      const region = section(label);
      expect(region).toHaveAttribute("id", id);
      return region;
    });
    // In document order, the order the list gives them.
    regions.slice(1).forEach((region, index) => {
      expect(regions[index]!.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });
  });

  it("folds only placement: no section chips, every section on the page, Edit placement opens its controls", async () => {
    renderEditor(row(), [], false);
    expect([...document.querySelectorAll("details[data-settings-group]")].map((group) => group.getAttribute("data-settings-group"))).toEqual(["Placement"]);
    expect(screen.queryByRole("button", { name: "Appearance" })).toBeNull();
    for (const [label] of SECTIONS) expect(section(label)).toBeVisible();
    // Closed, the line says where the row shows; the controls wait behind Edit placement.
    const placement = section("Placement");
    expect(within(placement).getByText("Recommended shelf on · Home screen on · every day")).toBeVisible();
    expect(within(placement).getByLabelText("Sort title prefix")).not.toBeVisible();
    await userEvent.click(within(placement).getByText("Edit placement"));
    expect(within(placement).getByLabelText("Sort title prefix")).toBeVisible();
  });

  it("has no Danger zone for a row being created, which has nothing on Plex yet", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <RowEditor collection={null} users={[]} onClose={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Row settings sections" });
    expect(within(nav).queryByRole("link", { name: "Danger zone" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Live on Plex" })).toBeNull();
  });

  it("keeps everything people see about the row together at the top", () => {
    // The name, description and poster used to be three groups apart — the last two folded near the
    // bottom — so the settings that decide what someone sees on Plex were the hardest to find.
    renderEditor(row());

    const looks = section("Name & look");
    expect(within(looks).getByText("Changed with Rename on Plex")).toBeInTheDocument();
    expect(within(looks).getByLabelText("Description")).toBeInTheDocument();
    expect(within(looks).getByRole("button", { name: "Plex default" })).toBeInTheDocument();
    expect(within(looks).getByText("On Plex")).toBeInTheDocument();
  });

  it("puts each setting in the section that answers its question", () => {
    renderEditor(row());

    expect(within(section("What goes in")).getByText("How many titles")).toBeInTheDocument();
    expect(within(section("What goes in")).getByRole("button", { name: "Best match" })).toBeInTheDocument();
    expect(within(section("What goes in")).getByText("Row type: Picked for You")).toBeInTheDocument();
    expect(within(section("Schedule")).getByText("Runs on…", { selector: "label" })).toBeInTheDocument();
    expect(within(section("What goes in")).getByText("Titles refresh every…")).toBeInTheDocument();
    expect(within(section("Placement")).getByLabelText("Sort title prefix")).toBeInTheDocument();
    expect(within(section("Placement")).getByRole("button", { name: "Every day" })).toBeInTheDocument();
  });

  it("shows the Requests settings outright when requests are on", async () => {
    settingsData.current = { "requests.enabled": true };
    renderEditor(row({ request_tag: "family-picks" }));

    expect(await within(section("Requests")).findByDisplayValue("family-picks")).toBeVisible();
  });

  it("says in the Requests section when requests are off", async () => {
    settingsData.current = { "requests.enabled": false };
    renderEditor(row({ request_tag: "family-picks" }));

    const requests = section("Requests");
    expect(await within(requests).findByText(/Requests are turned off/)).toBeInTheDocument();
    expect(within(requests).queryByLabelText(/Request tag/)).toBeNull();
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

  it("saves a description and sort title prefix, and shows what the row sorts as", async () => {
    renderEditor(row());

    await userEvent.type(screen.getByLabelText("Description"), "Picked nightly");
    await userEvent.type(screen.getByLabelText("Sort title prefix"), "!010_");
    expect(screen.getByText("!010_Hidden Gems")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(-1)?.[1] as Collection;
    expect(body.description).toBe("Picked nightly");
    expect(body.sort_title_prefix).toBe("!010_");
  });

  it("shows a TV-only row sorting under a TV library's name, not a movie library's", () => {
    // The editor has to hand the field the row's media: the field defaulted to "Movies" on its own.
    renderEditor(
      row({
        media: "show",
        name_template: "More {library_name} to watch",
        sort_title_prefix: "!010_",
      }),
    );

    expect(screen.getByText("!010_More TV Shows to watch")).toBeInTheDocument();
    expect(screen.queryByText("!010_More Movies to watch")).not.toBeInTheDocument();
  });

  it("previews the description on the Plex card as the sample person would see it", async () => {
    renderEditor(row());

    await userEvent.type(
      screen.getByLabelText("Description"),
      "Picked for {{user}",
    );

    const looks = screen.getByRole("region", { name: "Name & look" });
    expect(within(looks).getByText("Picked for Sarah")).toBeInTheDocument();
  });

  it("offers the recently-finished cooldown only on a watch-it-again row", () => {
    // It only changes a rewatch row; on any other row it would be a dial that does nothing.
    renderEditor(row({ rewatch: false }));
    expect(
      screen.queryByLabelText(/Skip titles finished in the last/i),
    ).not.toBeInTheDocument();

    cleanup();
    renderEditor(row({ rewatch: true, watched_pct: 1 }));
    expect(screen.getByLabelText(/Skip titles finished in the last/i)).toHaveValue(30);
  });

  it("saves a changed cooldown", async () => {
    renderEditor(row({ rewatch: true, watched_pct: 1 }));

    const input = screen.getByLabelText(/Skip titles finished in the last/i);
    await userEvent.clear(input);
    await userEvent.type(input, "90");
    await userEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(-1)?.[1] as Collection;
    expect(body.rewatch_cooldown_days).toBe(90);
  });

  it("names the rewatch switch after the row someone wants", () => {
    // The old label ("Lead with things they've seen") could only be understood by someone who had
    // already understood that the percentage above is a ceiling. The switch is now a kind.
    renderEditor(row());
    expect(
      screen.getByRole("radio", { name: "Watch it again" }),
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
      await screen.findByText("How many recent watches to match"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/Watches the AI web search looks up/i),
    ).not.toBeInTheDocument();
  });

  it("offers it once the row's own sources include AI web search", async () => {
    settingsData.current = { "candidates.sources": ["tmdb_similar"] };
    renderEditor(row({ recent_count: 5, candidate_sources: ["llm_web"] }));

    expect(
      await screen.findByLabelText(/^Watches the AI web search looks up$/i),
    ).toBeInTheDocument();
  });
});

describe("RowEditor — the outcome preview", () => {
  beforeEach(() => {
    settingsData.current = {};
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
      screen.getByRole("button", { name: overrideName(/recent releases/) }),
    ).toHaveTextContent("Reset");
  });

  it("hides the slider while the row is inheriting", () => {
    renderEditor(row({ recency: null }));
    expect(
      screen.queryByRole("slider", { name: /release date counts/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: overrideName(/recent releases/) }),
    ).toHaveTextContent("Override");
  });

  it("stops inheriting at the global's own value, not at zero", async () => {
    // Same trap freshness hit: dropping to 0 on un-inherit silently changes what the row does, so
    // the switch reads as broken. Here it would ALSO be indistinguishable from a deliberate
    // "any era" row, which is a real setting someone might have chosen.
    settingsData.current = { "recommendations.recency": 0.6 };
    renderEditor(row({ recency: null }));

    await userEvent.click(
      screen.getByRole("button", { name: overrideName(/recent releases/) }),
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
      screen.queryByRole("button", { name: overrideName(/refresh every/) }),
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

  // The "watch it again" switch that was on this list is a kind now, offered on every row by the
  // kind picker, so it is no longer a per-person dial to hide.
  const perPersonOnly = [
    ["button", overrideName(/already-watched/)],
    ["button", overrideName(/refresh every/)],
    ["switch", /not started/i],
    ["button", overrideName(/when someone hasn’t watched enough/)],
  ] as const;

  it("offers them on a per-person row", () => {
    // The control: without it, the shared-row assertions below could pass on a broken editor.
    renderEditor(row({ build: "per_person" }));
    for (const [role, name] of perPersonOnly) {
      expect(screen.getByRole(role, { name })).toBeInTheDocument();
    }
  });

  it("hides them on a shared row, rather than promising what the engine ignores", () => {
    // `_shared_row` never calls `_apply_watched_cap`, `_prefer_watched` or `_is_refresh_night`, and
    // cold-start is meaningless for a row built from aggregate history. Showing a control the
    // engine drops is worse than showing none — it states a behaviour.
    renderEditor(row({ build: "shared", min_watchers: 2 }));
    for (const [role, name] of perPersonOnly) {
      expect(screen.queryByRole(role, { name })).not.toBeInTheDocument();
    }
  });

  it("hides every search control, because a shared row does not search", () => {
    // A shared row is a straight tally of the server's most-watched titles. Sources, the seed budget
    // and the AI web-search dials were all inert here the moment it stopped searching, and a control
    // the engine ignores is worse than no control — it promises a behaviour.
    renderEditor(row({ build: "shared", min_watchers: 2 }));

    expect(
      screen.queryByText(/sources you enabled in/i),
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
      screen.getByText(/sources you enabled in/i),
    ).toBeInTheDocument();
  });

  it("keeps the controls a shared row DOES honour", () => {
    // Sources, libraries and display order all work on the shared path — hiding those would remove
    // real function.
    renderEditor(row({ build: "shared", min_watchers: 2 }));
    expect(screen.getByRole("group", { name: /order/i })).toBeInTheDocument();
  });

  it("does not ask which watch a shared row follows, even with a small seed budget stored", () => {
    // A shared row has no seeds to follow: it is the server's most-watched titles. A budget of 1 or 2 left
    // over from its per-person days used to bring the question back while the budget itself stayed hidden.
    renderEditor(row({ build: "shared", min_watchers: 2, max_seeds: 1 }));
    expect(
      screen.queryByLabelText(/Take turns between their last/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "Based on" })).toBeNull();
  });

  it("hides the release-date weight, which a shared row has nothing to apply it to", () => {
    // Recency weights a title's release date inside a SCORED CANDIDATE POOL. A shared row is the
    // server's most-watched titles ranked by how many people watched them, so there is no pool and
    // no score for the weight to act on — offering a dial that silently does nothing is the bug
    // this row type already had with `watched_pct` and `rewatch`. "Newest first" is how a shared
    // row leans modern now.
    renderEditor(row({ build: "shared", min_watchers: 2 }));
    expect(
      screen.queryByRole("button", { name: overrideName(/recent releases/) }),
    ).not.toBeInTheDocument();
  });
});

describe("RowEditor — which days a row appears", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows every day by default and says so", () => {
    renderEditor(row({ show_days: [] }));

    expect(
      screen.getByRole("button", { name: "Every day" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByText(/This row appears every day/i),
    ).toBeInTheDocument();
  });

  it("names the days it is hidden, which is the question people actually have", () => {
    renderEditor(row({ show_days: [1, 3, 5] }));

    expect(
      screen.getByText(/Hidden on Tuesday, Thursday, Saturday and Sunday/i),
    ).toBeInTheDocument();
  });

  it("round-trips chosen days into the PATCH body as ISO weekdays", async () => {
    renderEditor(row({ show_days: [1] }));

    await userEvent.click(screen.getByRole("button", { name: "Wed" }));
    await userEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.show_days).toEqual([1, 3]);
  });

  it("clears the schedule back to every day", async () => {
    renderEditor(row({ show_days: [1, 3] }));

    await userEvent.click(screen.getByRole("button", { name: "Every day" }));
    await userEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.show_days).toEqual([]);
  });

  it("refuses to deselect the last remaining day", async () => {
    // [] means EVERY day to the API, so letting the last chip off would turn "only Mondays" into
    // "always on" — the exact opposite of the click. Clearing is what "Every day" is for.
    renderEditor(row({ show_days: [1] }));

    await userEvent.click(screen.getByRole("button", { name: "Mon" }));

    expect(screen.getByRole("button", { name: "Mon" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("RowEditor — seasons", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("round-trips the seasons and their days into the PATCH body", async () => {
    renderEditor(row({ name: "{season} picks", seasons: ["christmas"] }));

    await userEvent.click(await screen.findByRole("checkbox", { name: /Halloween/ }));
    await userEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.seasons).toEqual(["halloween", "christmas"]);
    expect(body.season_lead_days).toBe(30);
  });

  it("tells the owner AI web search sits out on a seasonal row", () => {
    // The engine drops it there, so a sources control that still offers it without a word would
    // promise searches the row never makes.
    renderEditor(row({ seasons: ["christmas"] }));

    expect(screen.getByText(/AI web search isn.t used on a seasonal row/i)).toBeInTheDocument();
  });

  it("says nothing about seasons on an ordinary row's sources", () => {
    renderEditor(row());

    expect(screen.queryByText(/AI web search isn.t used on a seasonal row/i)).toBeNull();
  });

  it("does not offer seasons on the default row", () => {
    // Its title is the global template every person's row renders, which follows no season; the server
    // refuses seasons there, so the group would only offer a save that fails.
    renderEditor(row({ slug: "picked", name: "✨ {library_name} Picked for You" }));

    expect(screen.queryByRole("checkbox", { name: /Halloween/ })).toBeNull();
    expect(screen.queryByText("Not seasonal")).toBeNull();
  });
});

describe("RowEditor — switching to a day schedule does not guess at today", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("ticks the whole week, which still means every day", async () => {
    // It used to seed the BROWSER's weekday. Days turn over on the server's clock, so with the two
    // either side of midnight that pre-selected a day which was not today on the server — and saving
    // straight away hid the row. Seen live: browser on Wednesday, server already on Thursday.
    renderEditor(row({ show_days: [] }));

    await userEvent.click(
      screen.getByRole("button", { name: "Only on these days" }),
    );

    for (const name of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    }
  });

  it("narrows only when you untick a day", async () => {
    renderEditor(row({ show_days: [] }));

    await userEvent.click(
      screen.getByRole("button", { name: "Only on these days" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Sun" }));
    await userEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.show_days).toEqual([1, 2, 3, 4, 5, 6]);
  });
});


describe("RowEditor — audience availability", () => {
  it("does not present a failed roster as an empty audience", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const retry = vi.fn();
    render(<MemoryRouter><QueryClientProvider client={client}><RowEditor collection={row()} users={[]} audienceState="error" onRetryAudience={retry} onClose={() => {}} /></QueryClientProvider></MemoryRouter>);
    expect(screen.queryByText(/No users yet/)).not.toBeInTheDocument();
    const audience = within(screen.getByRole("region", { name: "Who gets it" }));
    expect(audience.getByRole("alert")).toHaveTextContent("Couldn’t load the audience. Your saved audience is unchanged.");
    await userEvent.click(audience.getByRole("button", { name: "Retry audience" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});


it("asks before a rename discards unsaved row settings", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rename = vi.fn();
  render(<MemoryRouter><QueryClientProvider client={client}><RowEditor collection={row()} users={[]} onClose={() => {}} onRename={rename} /></QueryClientProvider></MemoryRouter>);
  await userEvent.type(screen.getByLabelText("Description"), "a draft description");
  await userEvent.click(screen.getByRole("button", { name: "Rename on Plex…" }));
  await userEvent.type(screen.getByLabelText("New name"), " updated");
  await userEvent.click(screen.getByRole("button", { name: "Rename on Plex" }));
  expect(screen.getByRole("dialog", { name: "Discard unsaved settings and rename?" })).toBeInTheDocument();
  expect(rename).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(screen.getByLabelText("Description")).toHaveValue("a draft description");
});

describe("RowEditor — the overview a row opens with", () => {
  beforeEach(() => {
    settingsData.current = {};
    librariesData.current = [
      { key: "1", title: "Movies", type: "movie" },
      { key: "2", title: "TV Shows", type: "show" },
    ];
  });

  it("names the row as written and lists what it is called in each library", async () => {
    renderEditor(row({ name: "✨ {library_name} Picked for You", name_template: "✨ {library_name} Picked for You" }));

    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent("✨ Picked for You");
    expect(heading).not.toHaveTextContent("library name");
    expect(await screen.findByText("✨ Movies Picked for You")).toBeInTheDocument();
    expect(screen.getByText("✨ TV Shows Picked for You")).toBeInTheDocument();
  });

  it("counts a row's overrides beside the section they sit in, and says when it follows the server", () => {
    renderEditor(row({ min_rating: 6.5, refresh_days: 3 }));

    const nav = screen.getByRole("navigation", { name: "Row settings sections" });
    expect(within(nav).getByRole("link", { name: /What goes in/ })).toHaveTextContent("2 overrides");
    expect(within(nav).getByRole("link", { name: /Schedule/ })).not.toHaveTextContent("override");
    expect(within(nav).getByRole("link", { name: /Requests/ })).toHaveTextContent("server default");
  });

  it("shows an inherited setting as one line with its server value, and an override as such", async () => {
    settingsData.current = { "recommendations.refresh_days": 8 };
    renderEditor(row({ refresh_days: null }));

    const field = document.querySelector("[data-setting='refresh_days']") as HTMLElement;
    expect(within(field).getByText("server default")).toBeInTheDocument();
    await userEvent.click(within(field).getByRole("button", { name: overrideName(/refresh every/) }));
    expect(within(field).getByText("overridden here")).toBeInTheDocument();
  });

  it("offers the usual row sizes as one tap", async () => {
    renderEditor(row({ size: 15 }));

    await userEvent.click(screen.getByRole("button", { name: "30 titles" }));
    expect(screen.getByRole("button", { name: "30 titles" })).toHaveAttribute("aria-pressed", "true");
  });

  it("reads placement back as what the people who get it see and what the owner sees", () => {
    renderEditor(row({ placement: "library", placement_friends: "home" }), [
      { id: 4, username: "sarah", display_name: "sarah", enabled: true, user_type: "shared", prefs: {} } as unknown as User,
    ]);

    const placement = screen.getByRole("region", { name: "Placement" });
    expect(within(placement).getByText("What sarah sees")).toBeInTheDocument();
    expect(within(placement).getByText(/Their own row on the Home screen\./)).toBeInTheDocument();
    expect(within(placement).getByText(/Your own row on the Recommended shelf\./)).toBeInTheDocument();
  });
});
