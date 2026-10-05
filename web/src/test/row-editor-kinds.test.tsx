import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowEditor } from "@/components/rows/row-editor";
import { RowEnableToggle } from "@/components/rows/row-enable-toggle";
import { ApiError } from "@/lib/api";
import type * as ApiModule from "@/lib/api";
import { blankInput, toInput } from "@/lib/collections";
import {
  applyRowKind,
  describeKindChange,
  FILL_META,
  followsAWatch,
  hiddenButRead,
  KIND_META,
  NIGHTLY_LINE,
  rowKindOf,
  ROW_KINDS,
  visibleSettings,
  type RowKindChoice,
} from "@/lib/row-kinds";
import { findRowTemplate, ROW_TEMPLATES, type RowTemplate } from "@/lib/row-templates";
import type { Collection, CollectionInput } from "@/lib/types";
import { BYW_NAME, CTX, FIXTURES, named, row } from "@/test/row-kind-fixtures";
import { BUILTINS, CATALOGUE } from "@/test/season-fixtures";

// Mutable so a test can serve the owner's own seasons beside the built-ins.
const catalogueData = vi.hoisted(() => ({ current: [] as unknown[] }));

const { updateCollection, createCollection, settingsData, librariesData, rowSources } = vi.hoisted(() => ({
  updateCollection: vi.fn((id: number, body: unknown) =>
    Promise.resolve({ ...(body as object), id }),
  ),
  createCollection: vi.fn((body: unknown) =>
    Promise.resolve({ ...(body as object), id: 99 }),
  ),
  // Mutable so a test can serve a real server's globals; empty = the server's defaults.
  settingsData: { current: {} as Record<string, unknown> },
  // Mutable so a test can offer libraries to narrow a row to; empty = none listed.
  librariesData: { current: [] as { key: string; title: string; type: string }[] },
  // What the Your requests block's sources panel reads: one connected Overseerr, nothing else.
  rowSources: {
    overseerr: "connected",
    radarr: "off",
    sonarr: "off",
    complete: true,
    problems: [],
    seerr_requests: 3,
    seerr_requesters: 2,
    seerr_linked: 2,
    servers: [],
    tagged_movies: 0,
    tagged_shows: 0,
    people: [],
    tags: [],
  },
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      updateCollection: (id: number, body: unknown) => updateCollection(id, body),
      createCollection: (body: unknown) => createCollection(body),
      getSettings: () => Promise.resolve(settingsData.current),
      getLibraries: () => Promise.resolve(librariesData.current),
      getLibraryCollections: () => Promise.resolve([]),
      getSeasons: () => Promise.resolve(catalogueData.current),
      getSeasonPresets: () => Promise.resolve([]),
      getImageProvider: () => Promise.resolve({ capable: false, provider: "", reason: "" }),
      getRequestRowSources: () => Promise.resolve(rowSources),
      startRun: () => Promise.resolve({ run_id: 1 }),
    },
  };
});

function renderEditor(collection: Collection | null, template: RowTemplate | null = null) {
  const onClose = vi.fn();
  const onRename = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <RowEditor collection={collection} template={template} users={[]} onClose={onClose} onRename={onRename} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  document.querySelectorAll<HTMLDetailsElement>("details[data-settings-group], details[data-setting='kind']").forEach((group) => { group.open = true; });
  return { onClose, onRename };
}

const renderedSettings = () =>
  [...document.querySelectorAll("[data-setting]")].map((el) => el.getAttribute("data-setting")).sort();

const save = () => userEvent.click(screen.getByRole("button", { name: /Save changes|Add row/ }));

const kindRadio = (name: string) =>
  within(screen.getByRole("radiogroup", { name: "What kind of row is this?" })).getByRole("radio", {
    name,
  });

const buildRadio = (name: "Per person" | "Shared") =>
  within(screen.getByRole("radiogroup", { name: "One row each, or one for everyone?" })).getByRole("radio", { name });

/** Picks a kind on a saved row and confirms the dialog, taking whatever name it proposes. */
async function switchTo(kind: string) {
  const confirm = async () => {
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  };
  if (kind !== "Seasonal") {
    const mode = buildRadio(kind === "Popular on this server" ? "Shared" : "Per person");
    if (!(mode as HTMLInputElement).checked) {
      await userEvent.click(mode);
      await confirm();
    }
  }
  if (!(kindRadio(kind) as HTMLInputElement).checked) {
    await userEvent.click(kindRadio(kind));
    await confirm();
  }
}

const savedBody = async () => {
  await save();
  await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
  return updateCollection.mock.calls[0]?.[1] as CollectionInput;
};

beforeEach(() => {
  updateCollection.mockClear();
  createCollection.mockClear();
  settingsData.current = {};
  librariesData.current = [];
  catalogueData.current = BUILTINS;
});

describe("explicit Per person / Shared choice", () => {
  it.each(ROW_TEMPLATES)("$title exposes its mode before any section is opened", (template) => {
    renderEditor(null, template);
    document.querySelectorAll<HTMLDetailsElement>("details").forEach((group) => { group.open = false; });

    expect(buildRadio("Per person")).toBeVisible();
    expect(buildRadio("Shared")).toBeVisible();
    expect(buildRadio(template.values.build === "shared" ? "Shared" : "Per person")).toBeChecked();
  });

  it("a new seasonal row switches to shared without losing its season or other choices", async () => {
    const template = findRowTemplate("seasonal")!;
    renderEditor(null, { ...template, values: {
      ...template.values, build: "per_person", seasons: ["halloween"], season_lead_days: 17,
      season_after_days: 4, library_keys: ["1"], audience: "subset", audience_user_ids: [7],
    } });
    await userEvent.click(buildRadio("Shared"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(kindRadio("Seasonal")).toBeChecked();
    expect(buildRadio("Shared")).toBeChecked();
    await save();
    await waitFor(() => expect(createCollection).toHaveBeenCalledTimes(1));
    expect(createCollection.mock.calls[0]?.[0]).toMatchObject({
      build: "shared", seasons: ["halloween"], season_lead_days: 17,
      season_after_days: 4, library_keys: ["1"], audience: "subset", audience_user_ids: [7],
    });
  });

  it.each(["because-you-watched", "seen-it-already", "your-requests"])(
    "%s returns to its personal settings after trying Shared", async (id) => {
      const template = findRowTemplate(id)!;
      renderEditor(null, template);
      await userEvent.click(buildRadio("Shared"));
      await userEvent.click(buildRadio("Per person"));
      await save();
      await waitFor(() => expect(createCollection).toHaveBeenCalledTimes(1));
      expect(createCollection.mock.calls[0]?.[0]).toMatchObject(template.values);
    },
  );

  it("a saved row stays unchanged when Shared is cancelled", async () => {
    const collection = row({ rewatch: true, request_tag: "family" });
    renderEditor(collection);
    await userEvent.click(buildRadio("Shared"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Saving removes everyone's own copy/)).toBeInTheDocument();
    expect(updateCollection).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(buildRadio("Per person")).toBeChecked();
    expect(await savedBody()).toEqual(toInput(collection));
  });

  it("saved Shared confirms the name change and submits through the existing save", async () => {
    const { onRename } = renderEditor(row({ ...named(BYW_NAME), max_seeds: 2 }));
    await userEvent.click(buildRadio("Shared"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("New name")).toHaveValue("👥 Popular {library_name} on this server");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));
    expect(updateCollection).not.toHaveBeenCalled();
    expect(await savedBody()).toMatchObject({ build: "shared", defer_rename: false });
    expect(onRename).not.toHaveBeenCalled();
  });

  it("an existing shared seasonal row can become personal and keep its seasons", async () => {
    renderEditor(row({ ...named("{season} picks"), build: "shared", seasons: ["christmas"] }));
    await userEvent.click(buildRadio("Per person"));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Change it" }));
    expect(await savedBody()).toMatchObject({ build: "per_person", seasons: ["christmas"] });
  });

  it("cannot bypass the default row's watch-based name restriction", async () => {
    settingsData.current = { "row.name_template": BYW_NAME };
    renderEditor(row({ slug: "picked", name: "" }));
    await waitFor(() => expect(buildRadio("Shared")).toBeDisabled());
    expect(buildRadio("Shared")).toHaveAccessibleDescription(/Change it in Settings first/);
    expect(screen.getAllByRole("link", { name: "Settings › Row defaults" }).length).toBeGreaterThan(0);
  });

  it("Discard forgets an unsaved personal fill when reopening the saved shared draft", async () => {
    renderEditor(row({ build: "shared" }));
    await switchTo("Watch it again");
    await switchTo("Popular on this server");
    await userEvent.type(screen.getByLabelText("Description"), "Draft description");
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(updateCollection).not.toHaveBeenCalled();
    await userEvent.click(buildRadio("Per person"));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Change it" }));
    expect(await savedBody()).toMatchObject({ build: "per_person", rewatch: false, description: "" });
  });
});

describe("opening a row never changes it", () => {
  it.each(FIXTURES)("%s: no unsaved changes, and Save sends the row exactly as it was", async (_, collection) => {
    renderEditor(collection);
    await screen.findByRole("radiogroup", { name: "What kind of row is this?" });
    if (collection.seasons.length > 0) await screen.findAllByRole("checkbox");

    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
    await save();

    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    const [id, body] = updateCollection.mock.calls[0] ?? [];
    expect(id).toBe(collection.id);
    expect(body).toEqual(toInput(collection));
  });
});

describe("each kind shows exactly its settings", () => {
  it.each(FIXTURES)("%s", async (_, collection, ctx) => {
    renderEditor(collection);
    await screen.findByRole("radiogroup", { name: "What kind of row is this?" });

    const input = toInput(collection);
    const expected = new Set([...visibleSettings(input, ctx), ...hiddenButRead(input, ctx)]);
    expect(renderedSettings()).toEqual([...expected].sort());
  });

  it("keeps matching after a switch, with the settings of the new kind", async () => {
    renderEditor(null);
    await userEvent.click(buildRadio("Shared"));

    const after = applyRowKind(blankInput(), { kind: "popular", fill: "popular" }, CTX);
    expect(renderedSettings()).toEqual([...visibleSettings(after, CTX)].filter((key) => key !== "enabled").sort());
  });
});

describe("the kind picker", () => {
  it("lists the personal kinds with what viewers see, the row's own kind checked", async () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 2 }));
    const group = screen.getByRole("radiogroup", { name: "What kind of row is this?" });
    const titles = [
      "Picked for You",
      "Because you watched",
      "Watch it again",
      "Your requests",
      "Seasonal",
    ];
    const radios = within(group).getAllByRole("radio");
    expect(radios).toHaveLength(titles.length);
    radios.forEach((radio, i) => expect(radio).toHaveAccessibleName(titles[i]));
    expect(kindRadio("Because you watched")).toBeChecked();
    expect(kindRadio("Because you watched")).toHaveAccessibleDescription(
      'More like one thing they watched recently. Named after it, like "Because you watched Dune".',
    );
  });

  it("disables Seasonal on the default row and says why", async () => {
    renderEditor(row({ slug: "picked", name: "✨ {library_name} Picked for You" }));
    expect(kindRadio("Seasonal")).toBeDisabled();
    expect(kindRadio("Seasonal")).toHaveAccessibleDescription(/The default row can't be seasonal/);
    expect(buildRadio("Shared")).toBeEnabled();
  });

  it("switches a NEW row with no dialog", async () => {
    renderEditor(null);
    await userEvent.type(screen.getByLabelText("Name"), "Again");
    await userEvent.click(kindRadio("Watch it again"));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(kindRadio("Watch it again")).toBeChecked();
    await save();
    await waitFor(() => expect(createCollection).toHaveBeenCalled());
    const body = createCollection.mock.calls[0]?.[0] as CollectionInput;
    expect(body.rewatch).toBe(true);
    expect(body.seed_window).toBe(1);
  });

  it("turning it on follows every season, a month ahead", async () => {
    // Moved from row-seasons-field.test.tsx: "Follow the calendar" is the Seasonal kind now.
    renderEditor(null);
    await userEvent.type(screen.getByLabelText("Name"), "{{season} picks");
    await waitFor(() => expect(kindRadio("Seasonal")).toBeEnabled());
    await userEvent.click(kindRadio("Seasonal"));
    await save();

    await waitFor(() => expect(createCollection).toHaveBeenCalled());
    const body = createCollection.mock.calls[0]?.[0] as CollectionInput;
    expect(body.seasons).toEqual(["valentines", "halloween", "christmas"]);
    expect(body.season_lead_days).toBe(30);
  });

  it("turning it on leaves the owner's own seasons unticked: they are opt-in, row by row (#137)", async () => {
    catalogueData.current = CATALOGUE;
    renderEditor(null);
    await userEvent.type(screen.getByLabelText("Name"), "{{season} picks");
    await waitFor(() => expect(kindRadio("Seasonal")).toBeEnabled());
    await userEvent.click(kindRadio("Seasonal"));
    await save();

    await waitFor(() => expect(createCollection).toHaveBeenCalled());
    const body = createCollection.mock.calls[0]?.[0] as CollectionInput;
    expect(body.seasons).toEqual(["valentines", "halloween", "christmas"]);
  });

  it("renames a new {top_seed} row as it leaves Because you watched, or it would read straight back", async () => {
    renderEditor(null);
    await userEvent.type(screen.getByLabelText("Name"), "Because you watched {{top_seed}");
    expect(kindRadio("Because you watched")).toBeChecked();

    await userEvent.click(kindRadio("Picked for You"));

    expect(kindRadio("Picked for You")).toBeChecked();
    expect(screen.getByLabelText("Name")).toHaveValue("✨ {library_name} Picks");
  });

  it("switches a seasonal row's fill under How it's filled", async () => {
    const collection = row({ ...named("{season} picks"), seasons: ["halloween"] });
    renderEditor(collection);
    const fills = screen.getByRole("radiogroup", { name: "How it's filled" });

    await userEvent.click(within(fills).getByRole("radio", { name: "Watch it again" }));
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));

    expect(within(fills).getByRole("radio", { name: "Watch it again" })).toBeChecked();
    expect(kindRadio("Seasonal")).toBeChecked();
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(updateCollection.mock.calls[0]?.[1]).toEqual(
      applyRowKind(toInput(collection), { kind: "seasonal", fill: "again" }, CTX),
    );
  });
});

describe("the confirm dialog on a saved row", () => {
  const choice: RowKindChoice = { kind: "popular", fill: "popular" };

  it("says what the switch will do, in describeKindChange's words", async () => {
    const collection = row({ request_tag: "family" });
    renderEditor(collection);
    await userEvent.click(buildRadio("Shared"));

    const dialog = await screen.findByRole("dialog");
    const change = describeKindChange(toInput(collection), choice, CTX);
    expect(within(dialog).getByRole("heading", { name: change.title })).toBeInTheDocument();
    const lines = within(within(dialog).getByRole("list", { name: "What this changes" })).getAllByRole("listitem");
    expect(lines.map((line) => line.textContent)).toEqual(change.lines);
    expect(within(dialog).getByText(change.plexNote)).toBeInTheDocument();
  });

  it("changes nothing on Cancel", async () => {
    const collection = row();
    renderEditor(collection);
    await userEvent.click(buildRadio("Shared"));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(kindRadio("Picked for You")).toBeChecked();
    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(updateCollection.mock.calls[0]?.[1]).toEqual(toInput(collection));
  });

  it("applies exactly applyRowKind once confirmed", async () => {
    const collection = row({ request_tag: "family", seed_window: 1 });
    renderEditor(collection);
    await userEvent.click(buildRadio("Shared"));
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));

    expect(kindRadio("Popular on this server")).toBeChecked();
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(updateCollection.mock.calls[0]?.[1]).toEqual(applyRowKind(toInput(collection), choice, CTX));
  });

  it("saves a {top_seed} row's new name with its switch, and leaves the Plex title to the row's next run", async () => {
    const collection = row({ ...named(BYW_NAME), max_seeds: 3 });
    const { onRename } = renderEditor(collection);
    await userEvent.click(kindRadio("Picked for You"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("New name")).toHaveValue("✨ {library_name} Picks");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    // Read as it will be once renamed, not snapped back by the old name.
    expect(kindRadio("Picked for You")).toBeChecked();
    expect(onRename).not.toHaveBeenCalled();
    await save();

    // The rename screen renders a {top_seed} title as "" with no picks, so it would rename nothing
    // (`collection_reconcile._renamed_titles`); the row's next delivery retitles it instead.
    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(onRename).not.toHaveBeenCalled();
    const body = updateCollection.mock.calls[0]?.[1] as CollectionInput;
    expect(body).toEqual({
      ...applyRowKind(toInput(collection), { kind: "picked", fill: "picked" }, CTX),
      name: "✨ {library_name} Picks",
      name_template: "✨ {library_name} Picks",
      defer_rename: true,
    });
  });

  it("won't take a new name that still follows a watch", async () => {
    // Popular, not Watch it again: the engine names a Watch it again row after a watch too, but a
    // shared row has none to fill {top_seed} with (`rows._shared_row`).
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 2 }));
    await userEvent.click(buildRadio("Shared"));
    const dialog = await screen.findByRole("dialog");
    const name = within(dialog).getByLabelText("New name");
    await userEvent.clear(name);
    await userEvent.type(name, "Again {{top_seed}");

    expect(within(dialog).getByRole("button", { name: "Change it" })).toBeDisabled();
  });

  it("points the default row to Settings for its name instead of the rename screen", async () => {
    // Its name lives in Settings. A default row whose Settings name follows a watch can't leave
    // Because you watched at all (see the picker test below), so this is the other direction.
    const { onRename } = renderEditor(row({ slug: "picked", name: "✨ {library_name} Picked for You" }));
    await userEvent.click(kindRadio("Because you watched"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("New name")).toBeNull();
    expect(within(dialog).queryByRole("switch", { name: /Name it after their latest watch/i })).toBeNull();
    expect(within(dialog).getByRole("link", { name: /change it in Settings/i })).toHaveAttribute(
      "href",
      "/settings#defaults",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(onRename).not.toHaveBeenCalled();
  });

  it("offers an optional rename when a row becomes Because you watched", async () => {
    const collection = row();
    const { onRename } = renderEditor(collection);
    await userEvent.click(kindRadio("Because you watched"));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).queryByLabelText("New name")).toBeNull();
    await userEvent.click(within(dialog).getByRole("switch", { name: /Name it after their latest watch/i }));
    expect(within(dialog).getByLabelText("New name")).toHaveValue(BYW_NAME);
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    const body = await savedBody();
    expect(body.name_template).toBe(BYW_NAME);
    expect(body.defer_rename).toBe(true);
    // Its new name follows a watch, which only a run can fill in.
    expect(onRename).not.toHaveBeenCalled();
  });
});

describe("a setting the kind hides but the engine still reads", () => {
  it("shows Take turns with a note and Reset, and Reset puts it back to 1", async () => {
    renderEditor(row({ ...named("✨ {library_name} Picks"), seed_window: 3 }));

    const field = screen.getByLabelText(/Take turns between their last/i);
    expect(field).toBeDisabled();
    expect(screen.getByText(/Picked for You rows don't take turns/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reset" }));

    expect(screen.queryByLabelText(/Take turns between their last/i)).toBeNull();
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect((updateCollection.mock.calls[0]?.[1] as CollectionInput).seed_window).toBe(1);
  });

  it("disables Take turns on a blend, says why, and still offers Reset for a rotation left on", async () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 3, seed_window: 3 }));

    expect(screen.getByLabelText(/Take turns between their last/i)).toBeDisabled();
    expect(screen.getByText(/Doesn't work with a blend of 3 or more/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset" })).toBeInTheDocument();
  });
});

describe("Because you watched: Based on", () => {
  it("offers a film and a show, the last watch, or a blend on a movies-and-TV row", () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 3 }));
    const basedOn = screen.getByRole("radiogroup", { name: "Based on" });

    expect(within(basedOn).getByRole("radio", { name: "Their latest film and their latest show" })).not.toBeChecked();
    expect(within(basedOn).getByRole("radio", { name: "Only the very last thing they watched" })).not.toBeChecked();
    expect(within(basedOn).getByRole("radio", { name: /A blend of their last/ })).toBeChecked();
    expect(within(basedOn).getByLabelText("How many watches to blend")).toHaveValue(3);
  });

  it("offers their latest show or a blend of shows on a TV-only row", () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 1, media: "show" }));
    const basedOn = screen.getByRole("radiogroup", { name: "Based on" });

    expect(within(basedOn).getAllByRole("radio")).toHaveLength(2);
    expect(within(basedOn).getByRole("radio", { name: "Their latest show" })).toBeChecked();
    expect(within(basedOn).getByLabelText("How many shows to blend")).toBeInTheDocument();
  });

  it("writes the typed blend and stops taking turns", async () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 2, seed_window: 2 }));
    const blend = screen.getByLabelText("How many watches to blend");
    await userEvent.clear(blend);
    await userEvent.type(blend, "5");
    await userEvent.tab();
    await save();

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls[0]?.[1] as CollectionInput;
    expect(body.max_seeds).toBe(5);
    expect(body.seed_window).toBe(1);
  });

  it("changes nothing when you only tab past the blend box", async () => {
    const collection = row({ ...named(BYW_NAME), max_seeds: 2 });
    renderEditor(collection);
    screen.getByLabelText("How many watches to blend").focus();
    await userEvent.tab();
    await save();

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(updateCollection.mock.calls[0]?.[1]).toEqual(toInput(collection));
  });

  it("won't blend a row whose name doesn't follow a watch, which would make it Picked for You", () => {
    renderEditor(row({ max_seeds: 2 }));
    expect(screen.getByRole("radio", { name: /A blend of their last/ })).toBeDisabled();
    expect(screen.getByText(/To blend more, switch the kind to Picked for You/)).toBeInTheDocument();
  });

  it("says it picks new titles every night only when it follows a watch", () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 2 }));
    expect(screen.getByText(/Picks new titles every night, so it keeps up/)).toBeInTheDocument();
  });

  it("puts the name for someone new directly under when someone hasn't watched enough", () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 2 }));
    const coldStart = document.querySelector('[data-setting="cold_start"]');
    const fallback = document.querySelector('[data-setting="fallback_name"]');
    expect(coldStart?.nextElementSibling).toBe(fallback);
  });
});

describe("wording", () => {
  it("counts someone as new by the live history threshold, and links to where it's set", async () => {
    settingsData.current = { "recommendations.min_history": 12 };
    renderEditor(row());

    expect(await screen.findByText(/Someone counts as new until they’ve watched 12 titles/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "change it in Settings" })).toHaveAttribute("href", "/settings#min-history");
  });

  it("says Rated by is shared with every row and with requests", () => {
    renderEditor(row({ pick_order: "rating" }));
    expect(screen.getByText(/Shared by every row and by requests. Changes save immediately/)).toBeInTheDocument();
  });

  it("groups a Watch it again row's fill-up settings under their own heading", () => {
    renderEditor(row({ rewatch: true, watched_pct: 1 }));
    const fillUp = screen.getByRole("region", { name: "When their finished titles run out" });
    for (const key of ["max_seeds", "candidate_sources", "recency"]) {
      expect(fillUp.querySelector(`[data-setting="${key}"]`)).not.toBeNull();
    }
  });
});

describe("requests", () => {
  it("drops the request tag when Overseerr files the requests", async () => {
    settingsData.current = { "requests.target": "overseerr", "requests.enabled": true };
    renderEditor(row());
    await waitFor(() => expect(screen.queryByLabelText(/Request tag/)).toBeNull());
  });

  it("keeps the request tag for Radarr and Sonarr", async () => {
    settingsData.current = { "requests.target": "arr", "requests.enabled": true };
    renderEditor(row());
    expect(await screen.findByLabelText(/Request tag/)).toBeInTheDocument();
  });

  it("has no Requests section on a Your requests row, which has nothing there to set", async () => {
    // The folded group read "None — shared rows never ask for missing titles" under a row that is
    // not shared. With nothing to configure, an empty section is only a place to be wrong.
    renderEditor(row({ requests_row: true }));
    await screen.findByText("Which requests show up");
    expect(screen.queryByRole("region", { name: "Requests" })).toBeNull();
  });

  it("says a Popular row never asks for anything", () => {
    renderEditor(row({ build: "shared" }));
    const group = screen.getByRole("region", { name: "Requests" });
    expect(within(group).getByText(/A shared row never asks for missing titles/)).toBeInTheDocument();
    expect(group.querySelector('[data-setting="requests"]')).toBeNull();
  });
});

describe("switching kind is reversible, and only the last switch counts", () => {
  it("drops the rename an earlier switch queued when a later one takes the row back", async () => {
    const collection = row({ ...named(BYW_NAME), max_seeds: 2 });
    const { onRename } = renderEditor(collection);

    await switchTo("Picked for You");
    expect(screen.getByText(/to match its new kind/i)).toBeInTheDocument();

    await userEvent.click(kindRadio("Because you watched"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("New name")).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    expect(kindRadio("Because you watched")).toBeChecked();
    expect(screen.queryByText(/to match its new kind/i)).toBeNull();
    // The Name box exists only while a switch's rename is pending; back to the saved name, it is gone.
    expect(screen.queryByLabelText("Name")).toBeNull();
    expect(await savedBody()).toEqual(toInput(collection));
    expect(onRename).not.toHaveBeenCalled();
  });

  it("puts a saved row back exactly as it was loaded when it goes to Watch it again and back", async () => {
    const collection = row({ ...named("✨ {library_name} Picks"), max_seeds: 10, unstarted_only: true, watched_pct: 0 });
    renderEditor(collection);

    await switchTo("Watch it again");
    await switchTo("Picked for You");

    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
    expect(await savedBody()).toEqual(toInput(collection));
  });

  it("counts only the last kind when arrowing through the picker on a new row", async () => {
    const template = findRowTemplate("more-tv");
    if (!template) throw new Error("the More TV to watch template is gone");
    renderEditor(null, template);
    await waitFor(() => expect(kindRadio("Seasonal")).toBeEnabled());

    kindRadio("Picked for You").focus();
    // Down through the personal kinds to Seasonal.
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}{ArrowDown}");
    expect(kindRadio("Seasonal")).toBeChecked();
    await save();

    await waitFor(() => expect(createCollection).toHaveBeenCalled());
    const body = createCollection.mock.calls[0]?.[0] as CollectionInput;
    expect(body.unstarted_only).toBe(true);
    expect(body.watched_pct).toBe(0);
    // Your requests cannot be seasonal, so Seasonal starts with Picked for You.
    expect(body).toEqual(
      applyRowKind({ ...blankInput(), ...template.values }, { kind: "seasonal", fill: "picked" }, CTX),
    );
  });

  it("keeps a new row's hand edit made in its own kind through a round trip", async () => {
    const template = findRowTemplate("more-tv");
    if (!template) throw new Error("the More TV to watch template is gone");
    renderEditor(null, template);
    await userEvent.click(screen.getByRole("switch", { name: "Only series they have not started" }));

    await userEvent.click(kindRadio("Watch it again"));
    await userEvent.click(kindRadio("Picked for You"));
    await save();

    await waitFor(() => expect(createCollection).toHaveBeenCalled());
    expect((createCollection.mock.calls[0]?.[0] as CollectionInput).unstarted_only).toBe(false);
  });

  it("puts back what a hand edit changed only in the kind switched to, and says so", async () => {
    // Picked for You has no Take turns, so turning it up on Because you watched is part of that
    // switch, not of the row: switching back undoes it.
    const collection = row(named("✨ {library_name} Picks"));
    renderEditor(collection);
    await switchTo("Because you watched");
    const turns = screen.getByLabelText(/Take turns between their last/i);
    await userEvent.clear(turns);
    await userEvent.type(turns, "5");
    await userEvent.tab();

    await userEvent.click(kindRadio("Picked for You"));
    const dialog = await screen.findByRole("dialog");
    const lines = within(within(dialog).getByRole("list", { name: "What this changes" }))
      .getAllByRole("listitem")
      .map((line) => line.textContent);
    expect(lines).toContain("Stops taking turns between their last 5 watches.");
    expect(lines).not.toContain("Change nothing else about the row.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    expect(await savedBody()).toEqual(toInput(collection));
  });

  it("keeps seasons narrowed on screen when only the Seasonal fill changes", async () => {
    renderEditor(row(named("✨ {library_name} Picks")));
    await waitFor(() => expect(kindRadio("Seasonal")).toBeEnabled());
    await switchTo("Seasonal");
    await userEvent.click(screen.getByRole("checkbox", { name: /Valentine/ }));

    const fills = screen.getByRole("radiogroup", { name: "How it's filled" });
    await userEvent.click(within(fills).getByRole("radio", { name: "Watch it again" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Change it" }));

    const body = await savedBody();
    expect(body.seasons).toEqual(["halloween", "christmas"]);
    expect(body.rewatch).toBe(true);
  });

  it("enters Seasonal with the kind on screen as its fill", async () => {
    renderEditor(row(named("✨ {library_name} Picks")));
    await waitFor(() => expect(kindRadio("Seasonal")).toBeEnabled());
    await switchTo("Watch it again");
    await userEvent.click(kindRadio("Seasonal"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Change this row to Seasonal (Watch it again)?" })).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    const fills = screen.getByRole("radiogroup", { name: "How it's filled" });
    expect(within(fills).getByRole("radio", { name: "Watch it again" })).toBeChecked();
  });

  it("keeps the name typed on a new row, and gives it back when the switch that replaced it is undone", async () => {
    renderEditor(null);
    await userEvent.type(screen.getByLabelText("Name"), "{{season_emoji} {{season} picks");
    await waitFor(() => expect(kindRadio("Seasonal")).toBeEnabled());
    await userEvent.click(kindRadio("Seasonal"));

    await userEvent.click(kindRadio("Picked for You"));
    // The API refuses {season} on a row that follows no season.
    expect(screen.getByLabelText("Name")).toHaveValue("✨ {library_name} Picks");

    await userEvent.click(kindRadio("Seasonal"));
    expect(screen.getByLabelText("Name")).toHaveValue("{season_emoji} {season} picks");
  });

  it.each(FIXTURES)("%s: every other kind and back saves the row exactly as loaded", async (_, collection, ctx) => {
    const { onRename } = renderEditor(collection);
    await screen.findByRole("radiogroup", { name: "What kind of row is this?" });
    if (!ctx.isDefault) await waitFor(() => expect(kindRadio("Seasonal")).toBeEnabled());
    const ownKind = rowKindOf(toInput(collection), ctx);
    const own = KIND_META[ownKind.kind].title;
    const ownFill = FILL_META[ownKind.fill].title;

    for (const kind of ROW_KINDS.map((k) => KIND_META[k].title)) {
      const available = within(screen.getByRole("radiogroup", { name: "What kind of row is this?" })).queryByRole("radio", { name: kind });
      if (kind === own || available?.hasAttribute("disabled") || (kind === "Popular on this server" && buildRadio("Shared").hasAttribute("disabled"))) continue;
      await switchTo(kind);
      await switchTo(own);
      expect(kindRadio(own), `back from ${kind}`).toBeChecked();
      // Seasonal keeps the kind on screen as its fill; the row's own fill is then picked under it.
      if (ownKind.kind === "seasonal") {
        const mode = buildRadio(ownKind.fill === "popular" ? "Shared" : "Per person");
        if (!(mode as HTMLInputElement).checked) {
          await userEvent.click(mode);
          await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Change it" }));
          await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
        }
        if (ownKind.fill === "popular") continue;
        const fill = within(screen.getByRole("radiogroup", { name: "How it's filled" })).getByRole("radio", {
          name: ownFill,
        });
        if (!(fill as HTMLInputElement).checked) {
          await userEvent.click(fill);
          await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Change it" }));
          await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
        }
      }
    }

    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
    expect(await savedBody()).toEqual(toInput(collection));
    expect(onRename).not.toHaveBeenCalled();
  }, 30_000);
});

describe("the default row named after a watch in Settings", () => {
  it("disables every kind it can't become from here, and links to its name in Settings", async () => {
    settingsData.current = { "row.name_template": BYW_NAME };
    renderEditor(row({ slug: "picked", name: BYW_NAME }));
    await waitFor(() => expect(kindRadio("Picked for You")).toBeDisabled());

    // Your requests has no watch to fill a {top_seed} name with either.
    for (const kind of ["Picked for You", "Your requests"]) {
      expect(kindRadio(kind), kind).toBeDisabled();
      expect(kindRadio(kind), kind).toHaveAccessibleDescription(
        /This row's name \(set in Settings\) follows one watch\. Change it in Settings first\./,
      );
    }
    expect(kindRadio("Watch it again")).toBeEnabled();
    expect(kindRadio("Because you watched")).toBeChecked();
    const links = within(screen.getByRole("radiogroup", { name: "What kind of row is this?" })).getAllByRole("link");
    expect(links).toHaveLength(2);
    for (const link of links) expect(link).toHaveAttribute("href", "/settings#defaults");
  });

  it("can become Watch it again, which the engine names after a watch too, keeping its Settings name", async () => {
    settingsData.current = { "row.name_template": BYW_NAME };
    const { onRename } = renderEditor(row({ slug: "picked", name: BYW_NAME }));
    await waitFor(() => expect(kindRadio("Picked for You")).toBeDisabled());
    await userEvent.click(kindRadio("Watch it again"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("link", { name: /change it in Settings/i })).toBeNull();
    expect(within(dialog).getByText(NIGHTLY_LINE)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    expect(kindRadio("Watch it again")).toBeChecked();
    const body = await savedBody();
    expect(body.rewatch).toBe(true);
    expect(body.name_template).toBe("");
    expect(onRename).not.toHaveBeenCalled();
  });
});

describe("a seasonal row whose name uses the season", () => {
  it("won't leave Seasonal without a new name, and hands that name to the rename screen", async () => {
    const collection = row({ ...named("{season_emoji} {season} picks"), seasons: ["halloween"] });
    const { onRename } = renderEditor(collection);
    await userEvent.click(kindRadio("Picked for You"));

    const dialog = await screen.findByRole("dialog");
    const name = within(dialog).getByLabelText("New name");
    expect(name).toHaveValue("✨ {library_name} Picks");
    expect(within(dialog).getByText(/only a seasonal row can fill/i)).toBeInTheDocument();

    await userEvent.clear(name);
    await userEvent.type(name, "{{season} picks");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("only works on a seasonal row");
    expect(within(dialog).getByRole("button", { name: "Change it" })).toBeDisabled();

    await userEvent.clear(name);
    await userEvent.type(name, "Halloween all year");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    // One PATCH carries both: the API refuses the season name on a row with no seasons, so saving the
    // settings first under the old name could never succeed.
    const body = await savedBody();
    expect(body.seasons).toEqual([]);
    expect(body.name).toBe("Halloween all year");
    expect(body.name_template).toBe("Halloween all year");
    expect(body.defer_rename).toBe(true);
    await waitFor(() =>
      expect(onRename).toHaveBeenCalledWith("Halloween all year", { oldTemplate: "{season_emoji} {season} picks" }),
    );
  });

  it("lets a name with {top_seed} and the season keep {top_seed} on the way to Because you watched", async () => {
    const collection = row({
      ...named("🎯 {season}: because you watched {top_seed}"),
      seasons: ["christmas"],
      max_seeds: 2,
    });
    const { onRename } = renderEditor(collection);
    await userEvent.click(kindRadio("Because you watched"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("New name")).toHaveValue(BYW_NAME);
    expect(within(dialog).getByRole("button", { name: "Change it" })).toBeEnabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));
    const body = await savedBody();
    expect(body.name_template).toBe(BYW_NAME);
    expect(body.seasons).toEqual([]);
    expect(body.defer_rename).toBe(true);
    // Both names follow a watch: the next run retitles it, the rename screen can't.
    expect(onRename).not.toHaveBeenCalled();
  });
});

describe("saving a kind switch that renames the row", () => {
  it("saves the name with the switch and applies it when the row is rebuilt, if the switch changes the build", async () => {
    // A build flip deletes the old build's collections at save (`plan_row_changes`), so there is
    // nothing left on Plex to rename: no rename screen, and no deferred rename.
    const collection = row({ ...named(BYW_NAME), max_seeds: 2 });
    const { onClose, onRename } = renderEditor(collection);
    await userEvent.click(buildRadio("Shared"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("The new name is used when the row is rebuilt.")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    const body = await savedBody();
    expect(body.build).toBe("shared");
    expect(body.name).toBe("👥 Popular {library_name} on this server");
    expect(body.name_template).toBe("👥 Popular {library_name} on this server");
    expect(body.defer_rename).toBe(false);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onRename).not.toHaveBeenCalled();
  });

  it("says the rename screen renames it on Plex when neither name follows a watch", async () => {
    renderEditor(row({ ...named("{season} picks"), seasons: ["christmas"] }));
    await userEvent.click(kindRadio("Picked for You"));
    expect(
      within(await screen.findByRole("dialog")).getByText(
        "Saving opens the rename screen with this name, which renames the row on Plex for everyone who has it.",
      ),
    ).toBeInTheDocument();
  });

  it("says a name that follows a watch reaches Plex at the next run, before and after saving", async () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 2 }));
    await userEvent.click(kindRadio("Picked for You"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("The new name appears on Plex the next time the row runs.")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    expect(screen.getByText(/The new name appears on Plex the next time the row runs\./)).toBeInTheDocument();
  });

  it("says a switched-off row takes its new name when it's turned back on and runs", async () => {
    const { onRename } = renderEditor(row({ ...named(BYW_NAME), max_seeds: 2, enabled: false }));
    await userEvent.click(kindRadio("Picked for You"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("The new name appears on Plex when you turn it back on and it runs.")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "This row is switched off, so it isn't on Plex. These settings apply when you turn it back on.",
      ),
    ).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    expect((await savedBody()).defer_rename).toBe(true);
    expect(onRename).not.toHaveBeenCalled();
  });

  it("stays put with the rename still pending when the save is refused, and takes a fixed name from the Name box", async () => {
    const collection = row({ ...named("{season} picks"), seasons: ["christmas"] });
    const { onClose, onRename } = renderEditor(collection);
    await switchTo("Picked for You");
    updateCollection.mockRejectedValueOnce(new ApiError(422, "That name is already used by another row."));

    await save();
    expect(await screen.findByText("That name is already used by another row.")).toHaveAttribute("role", "alert");
    expect(onClose).not.toHaveBeenCalled();
    expect(onRename).not.toHaveBeenCalled();
    const name = screen.getByLabelText("Name");
    expect(name).toHaveValue("✨ {library_name} Picks");

    await userEvent.clear(name);
    await userEvent.type(name, "Christmas all year");
    await save();
    await waitFor(() =>
      expect(onRename).toHaveBeenCalledWith("Christmas all year", { oldTemplate: "{season} picks" }),
    );
    expect(updateCollection).toHaveBeenCalledTimes(2);
    expect((updateCollection.mock.calls[1]?.[1] as CollectionInput).name_template).toBe("Christmas all year");
  });

  it("checks a pending rename typed in the Name box the way the dialog does", async () => {
    renderEditor(row({ ...named("{season} picks"), seasons: ["christmas"] }));
    await switchTo("Picked for You");
    const name = screen.getByLabelText("Name");

    await userEvent.clear(name);
    await userEvent.type(name, "{{season} forever");
    expect(screen.getByText("A name with {season} only works on a seasonal row.")).toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rename on Plex…" })).toBeDisabled();
  });
});

describe("the dialog's nightly line", () => {
  it("appears once the optional {top_seed} name is switched on", async () => {
    renderEditor(row());
    await userEvent.click(kindRadio("Because you watched"));
    const dialog = await screen.findByRole("dialog");
    const lines = () =>
      within(within(dialog).getByRole("list", { name: "What this changes" }))
        .getAllByRole("listitem")
        .map((line) => line.textContent);

    expect(lines()).not.toContain(NIGHTLY_LINE);
    await userEvent.click(within(dialog).getByRole("switch", { name: /Name it after their latest watch/i }));
    expect(lines()).toContain(NIGHTLY_LINE);
  });
});

describe("focus after the dialog", () => {
  it("goes back to the kind that was picked when the dialog is cancelled", async () => {
    renderEditor(row());
    await userEvent.click(buildRadio("Shared"));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(buildRadio("Shared")).toHaveFocus());
  });

  it("goes back to the kind that was picked when the dialog is closed with Escape", async () => {
    renderEditor(row());
    await userEvent.click(kindRadio("Watch it again"));
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(kindRadio("Watch it again")).toHaveFocus());
  });
});

describe("Picked for You's watch count with a global of 1 or 2", () => {
  it("won't follow the global, which would make it a Because you watched row, and says why", async () => {
    settingsData.current = { "recommendations.max_seeds": 2 };
    renderEditor(row({ max_seeds: 5 }));
    const toggle = screen.getByRole("switch", { name: /global default for how many recent watches to match/i });

    await waitFor(() => expect(toggle).toBeDisabled());
    expect(toggle).toHaveAccessibleDescription(
      "Following the global default (2) would make this a Because you watched row.",
    );
  });

  it("follows a global of 3 or more as before", async () => {
    settingsData.current = { "recommendations.max_seeds": 3 };
    renderEditor(row({ max_seeds: null }));
    // Only once settings have loaded does the toggle name the global it follows.
    expect(await screen.findByText("3 watches")).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: /global default for how many recent watches to match/i });
    expect(toggle).toBeEnabled();
    expect(toggle).toBeChecked();
  });
});

describe("a Because you watched blend named after a watch", () => {
  it("offers the global default, and keeps a row that follows it following it", async () => {
    settingsData.current = { "recommendations.max_seeds": 30 };
    const collection = row({ ...named(BYW_NAME), max_seeds: null });
    renderEditor(collection);
    const basedOn = screen.getByRole("radiogroup", { name: "Based on" });

    expect(within(basedOn).getByRole("radio", { name: /A blend of their last/ })).toBeChecked();
    const inherit = await within(basedOn).findByRole("switch", { name: "Use the global default (30)" });
    expect(inherit).toBeChecked();
    expect((await savedBody()).max_seeds).toBeNull();
  });

  it("sets the row's own count at the global's value when it stops following it", async () => {
    settingsData.current = { "recommendations.max_seeds": 30 };
    renderEditor(row({ ...named(BYW_NAME), max_seeds: null }));
    await userEvent.click(await screen.findByRole("switch", { name: "Use the global default (30)" }));

    expect((await savedBody()).max_seeds).toBe(30);
  });

  it("follows the global again when switched back on", async () => {
    settingsData.current = { "recommendations.max_seeds": 30 };
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 5, seed_window: 1 }));
    await userEvent.click(await screen.findByRole("switch", { name: "Use the global default (30)" }));

    const body = await savedBody();
    expect(body.max_seeds).toBeNull();
    expect(body.seed_window).toBe(1);
  });

  it("isn't offered on a row whose name doesn't follow a watch, where following a large global reads as Picked for You", () => {
    renderEditor(row({ max_seeds: 2 }));
    expect(screen.queryByRole("switch", { name: /Use the global default \(/ })).toBeNull();
  });

  it("explains a blend in neutral words, without telling the owner to change it", () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 5 }));
    const help = screen.getByText(
      "Picks mix all of these watches, but the name only mentions the latest one, and it can't take turns while blending.",
    );
    expect(help.className).not.toMatch(/warning/);
    expect(help).not.toHaveAttribute("role");
    expect(screen.queryByText(/Set it to/)).toBeNull();
  });
});

describe("Take turns, disabled", () => {
  it("shows only why, not what the number would do", () => {
    renderEditor(row({ ...named(BYW_NAME), max_seeds: 3 }));
    expect(screen.getByLabelText(/Take turns between their last/i)).toBeDisabled();
    expect(screen.getByText(/Doesn't work with a blend of 3 or more/)).toBeInTheDocument();
    expect(screen.queryByText(/Always the last thing they finished/)).toBeNull();
  });
});

describe("Watch it again's new picks", () => {
  it("can take turns when they match 1 or 2 watches, set under When their finished titles run out", async () => {
    renderEditor(row({ rewatch: true, watched_pct: 1, max_seeds: 2, seed_window: 3 }));
    const fillUp = screen.getByRole("region", { name: "When their finished titles run out" });
    const turns = within(fillUp).getByLabelText(/Take turns between their last/i);
    expect(turns).toBeEnabled();
    expect(turns).toHaveValue(3);

    await userEvent.clear(turns);
    await userEvent.type(turns, "4");
    await userEvent.tab();
    expect((await savedBody()).seed_window).toBe(4);
  });
});

describe("a switch never leaves a pair the API refuses", () => {
  it.each(["Watch it again", "Popular on this server"])(
    "turns Only series they haven't started off on the way back from %s once the row only holds movies",
    async (away) => {
      // `collections._validate_pairing` refuses the flag on a movies-only row. The narrowing clears it
      // on screen, but the kind switched to doesn't show it, so the loaded value waits in the baseline.
      librariesData.current = [
        { key: "1", title: "Movies", type: "movie" },
        { key: "2", title: "TV Shows", type: "show" },
      ];
      renderEditor(row({ ...named("✨ {library_name} Picks"), unstarted_only: true }));
      await switchTo(away);
      await userEvent.click(await screen.findByRole("checkbox", { name: /TV Shows/ }));

      await userEvent.click(away === "Popular on this server" ? buildRadio("Per person") : kindRadio("Picked for You"));
      const dialog = await screen.findByRole("dialog");
      const lines = within(within(dialog).getByRole("list", { name: "What this changes" }))
        .getAllByRole("listitem")
        .map((line) => line.textContent);
      expect(lines.join(" ")).not.toContain("Only series they haven't started");
      await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

      const body = await savedBody();
      expect(body.media).toBe("movie");
      expect(body.unstarted_only).toBe(false);
      expect(body.rewatch).toBe(false);
    },
  );
});

describe("a hand edit's side effects stay with the kind they were made in", () => {
  it("keeps a Watch it again row's rotation when Only series they haven't started is turned on in Picked for You", async () => {
    const collection = row({
      ...named("☕ {library_name} you've already seen"),
      rewatch: true,
      watched_pct: 1,
      max_seeds: 2,
      seed_window: 3,
    });
    renderEditor(collection);
    await switchTo("Picked for You");
    await userEvent.click(screen.getByRole("switch", { name: "Only series they have not started" }));
    await switchTo("Watch it again");

    const body = await savedBody();
    expect(body.seed_window).toBe(3);
    expect(body).toEqual(toInput(collection));
  });

  it("keeps a Watch it again row's rotation when a blend is chosen in Because you watched", async () => {
    // Choosing a blend stops taking turns, but Based on is Because you watched's own setting: the
    // rotation it resets there belongs to that switch, not to the row as loaded.
    const collection = row({ ...named(BYW_NAME), rewatch: true, watched_pct: 1, media: "show", max_seeds: 2, seed_window: 3 });
    renderEditor(collection);
    await switchTo("Because you watched");
    const blend = screen.getByLabelText("How many shows to blend");
    await userEvent.clear(blend);
    await userEvent.type(blend, "4");
    await userEvent.tab();
    await switchTo("Watch it again");

    expect(await savedBody()).toEqual(toInput(collection));
  });
});

describe("a pending rename never changes the kind on screen", () => {
  it("keeps the kind the switch chose while the Name box holds a name it can't take, and switches back from there", async () => {
    const collection = row({ ...named(BYW_NAME), max_seeds: 2 });
    const { onRename } = renderEditor(collection);
    await switchTo("Picked for You");
    const name = screen.getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Picks {{top_seed}");

    expect(kindRadio("Picked for You")).toBeChecked();
    expect(screen.getByText(/and this kind doesn't follow one/)).toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    await switchTo("Because you watched");
    expect(kindRadio("Because you watched")).toBeChecked();
    expect(screen.queryByText(/to match its new kind/i)).toBeNull();
    // The Name box exists only while a switch's rename is pending; back to the saved name, it is gone.
    expect(screen.queryByLabelText("Name")).toBeNull();
    expect(await savedBody()).toEqual(toInput(collection));
    expect(onRename).not.toHaveBeenCalled();
  });
});

describe("the on/off switch at the top", () => {
  const OFF_NOTE = "This row is switched off, so it isn't on Plex. These settings apply when you turn it back on.";

  it("isn't undone by saving the page after switching the row off there", async () => {
    renderEditor(row());
    await userEvent.click(await screen.findByRole("switch", { name: /Enable Hidden Gems/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Turn it off" }));
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect((updateCollection.mock.calls[0]?.[1] as CollectionInput).enabled).toBe(false);

    await waitFor(() => expect(screen.getByText(/^Off — switching it off/)).toBeInTheDocument());
    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
    await userEvent.click(kindRadio("Watch it again"));
    expect(within(await screen.findByRole("dialog")).getByText(OFF_NOTE)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));

    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(2));
    expect((updateCollection.mock.calls[1]?.[1] as CollectionInput).enabled).toBe(false);
  });

  it("isn't undone by saving the page after switching the row on there", async () => {
    renderEditor(row({ enabled: false }));
    await userEvent.click(await screen.findByRole("switch", { name: /Enable Hidden Gems/ }));
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));

    await waitFor(() => expect(screen.queryByText(/^Off — switching it off/)).toBeNull());
    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
    await userEvent.click(kindRadio("Watch it again"));
    expect(within(await screen.findByRole("dialog")).queryByText(OFF_NOTE)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));

    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(2));
    expect((updateCollection.mock.calls[1]?.[1] as CollectionInput).enabled).toBe(true);
  });
});

describe("a pending rename keeps what the switch confirmed", () => {
  const SEASON_ROW = row({ ...named("{season_emoji} {season} picks"), seasons: ["halloween"] });

  /** Leaves Seasonal for Picked for You, confirming the given name in the dialog. */
  async function leaveSeasonalAs(newName: string) {
    await userEvent.click(kindRadio("Picked for You"));
    const dialog = await screen.findByRole("dialog");
    const box = within(dialog).getByLabelText("New name");
    await userEvent.clear(box);
    await userEvent.type(box, newName);
    return dialog;
  }

  it("says the season is the only reason a seasonal name must change on the way to Picked for You", async () => {
    renderEditor(SEASON_ROW);
    const dialog = await leaveSeasonalAs("Halloween all year");
    expect(within(dialog).getByText(/Its name uses the season/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/Its name follows one watch/)).toBeNull();
  });

  it("refuses {top_seed} on the way to Picked for You, in the dialog and in the Name box", async () => {
    renderEditor(SEASON_ROW);
    const dialog = await leaveSeasonalAs("Because you watched {{top_seed}");
    expect(within(dialog).getByRole("button", { name: "Change it" })).toBeDisabled();
    const box = within(dialog).getByLabelText("New name");
    await userEvent.clear(box);
    await userEvent.type(box, "Halloween all year");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const name = screen.getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Because you watched {{top_seed}");
    expect(kindRadio("Picked for You")).toBeChecked();
    expect(screen.getByText(/and this kind doesn't follow one/)).toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    await userEvent.clear(name);
    await userEvent.type(name, "Halloween forever");
    const body = await savedBody();
    expect(body.name_template).toBe("Halloween forever");
    expect(rowKindOf(body, CTX).kind).toBe("picked");
  });

  it("won't drop {top_seed} from a Because you watched name the switch confirmed", async () => {
    renderEditor(row({ ...named("Hidden Gems"), refresh_days: 7 }));
    await userEvent.click(kindRadio("Because you watched"));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("switch", { name: /Name it after their latest watch/i }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const name = screen.getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Hidden Gems again");
    expect(
      screen.getByText("Keep {top_seed} in the name: the change you confirmed names the row after their latest watch."),
    ).toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    // Kept, it saves the row on screen: Because you watched, rebuilt nightly as the editor says.
    await userEvent.clear(name);
    await userEvent.type(name, "Hidden Gems from {{top_seed}");
    expect(screen.getByText(/Changes every night/)).toBeInTheDocument();
    const body = await savedBody();
    expect(body.name_template).toBe("Hidden Gems from {top_seed}");
    expect(rowKindOf(body, CTX).kind).toBe("byw");
    expect(followsAWatch(body, CTX)).toBe(true);
  });
});

describe("the on/off switch and page Save never overlap", () => {
  /** Holds the next PATCH open until `release` is called. */
  function holdNextSave() {
    let release: () => void = () => {};
    updateCollection.mockImplementationOnce(
      (id: number, body: unknown) =>
        new Promise((resolve) => {
          release = () => resolve({ ...(body as object), id });
        }),
    );
    return () => release();
  }

  it("holds page Save and Rename on Plex… while the switch's change is being saved", async () => {
    renderEditor(row());
    expect(screen.getByRole("button", { name: "Rename on Plex…" })).toBeEnabled();

    const release = holdNextSave();
    await userEvent.click(await screen.findByRole("switch", { name: /Enable Hidden Gems/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Turn it off" }));
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rename on Plex…" })).toBeDisabled();

    release();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Rename on Plex…" })).toBeEnabled();
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(2));
    expect((updateCollection.mock.calls[1]?.[1] as CollectionInput).enabled).toBe(false);
  });

  it("can't be switched while the page is being saved", async () => {
    renderEditor(row());
    const release = holdNextSave();
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("switch", { name: /Enable Hidden Gems/ })).toBeDisabled();

    release();
    await waitFor(() => expect(screen.getByRole("switch", { name: /Enable Hidden Gems/ })).toBeEnabled());
    expect(updateCollection).toHaveBeenCalledTimes(1);
  });
});

describe("a {top_seed} name on the way to Watch it again", () => {
  it("stays, since the engine names a Watch it again row after a watch too, and the dialog says it rebuilds nightly", async () => {
    const collection = row({ ...named(BYW_NAME), max_seeds: 2 });
    renderEditor(collection);
    await userEvent.click(kindRadio("Watch it again"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("New name")).toBeNull();
    expect(within(dialog).getByText(NIGHTLY_LINE)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));

    const body = await savedBody();
    expect(body.name_template).toBe(BYW_NAME);
    expect(rowKindOf(body, CTX).kind).toBe("again");
    expect(followsAWatch(body, CTX)).toBe(true);
  });

  it("can be kept by a row leaving Seasonal, and can't be dropped from the Name box after", async () => {
    renderEditor(row({ ...named("{season} rewatch: {top_seed}"), seasons: ["halloween"], rewatch: true, watched_pct: 1 }));
    await userEvent.click(kindRadio("Watch it again"));
    const dialog = await screen.findByRole("dialog");
    const lines = () =>
      within(within(dialog).getByRole("list", { name: "What this changes" }))
        .getAllByRole("listitem")
        .map((line) => line.textContent);
    expect(lines()).not.toContain(NIGHTLY_LINE);
    const box = within(dialog).getByLabelText("New name");
    await userEvent.clear(box);
    await userEvent.type(box, "Rewatch: {{top_seed}");
    expect(lines()).toContain(NIGHTLY_LINE);
    await userEvent.click(within(dialog).getByRole("button", { name: "Change it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const name = screen.getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Rewatch");
    expect(screen.getByText(/Keep \{top_seed\} in the name/)).toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    await userEvent.clear(name);
    await userEvent.type(name, "Rewatch again: {{top_seed}");
    const body = await savedBody();
    expect(body.name_template).toBe("Rewatch again: {top_seed}");
    expect(rowKindOf(body, CTX).kind).toBe("again");
    expect(followsAWatch(body, CTX)).toBe(true);
  });
});

describe("the on/off switch's Try again", () => {
  it("is held while the page is being saved", async () => {
    renderEditor(row({ description: "old" }));
    updateCollection.mockImplementationOnce(() => Promise.reject(new Error("boom")));
    await userEvent.click(await screen.findByRole("switch", { name: /Enable Hidden Gems/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Turn it off" }));
    const alert = (await screen.findByText(/This row is still on/)).closest("[role=alert]") as HTMLElement;
    const retry = within(alert).getByRole("button", { name: "Try again" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled());

    let release: () => void = () => {};
    updateCollection.mockImplementationOnce(
      (id: number, body: unknown) =>
        new Promise((resolve) => {
          release = () => resolve({ ...(body as object), id });
        }),
    );
    await save();
    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(2));
    expect(retry).toBeDisabled();
    await userEvent.click(retry);
    expect(updateCollection).toHaveBeenCalledTimes(2);
    release();
  });

  it("sends the row as it is now with only the switch changed, never the request that failed", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const at = (collection: Collection) => (
      <QueryClientProvider client={client}>
        <RowEnableToggle collection={collection} />
      </QueryClientProvider>
    );
    const { rerender } = render(at(row({ description: "old" })));
    updateCollection.mockImplementationOnce(() => Promise.reject(new Error("boom")));
    await userEvent.click(screen.getByRole("switch", { name: /Enable Hidden Gems/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Turn it off" }));
    await screen.findByText(/This row is still on/);

    // The row changed on the server since (a page Save, another tab); the list refetched it.
    const current = row({ description: "new", size: 25 });
    rerender(at(current));
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(updateCollection).toHaveBeenCalledTimes(2));
    expect(updateCollection.mock.calls[1]?.[1]).toEqual({ ...toInput(current), enabled: false });
  });
});
