import { describe, expect, it } from "vitest";

import { blankInput, toInput } from "@/lib/collections";
import { SEASON, SEASON_EMOJI, TOP_SEED } from "@/lib/placeholders";
import {
  applyRowKind,
  describeKindChange,
  FIELD_SETTING,
  FILL_META,
  hiddenButRead,
  baselineTakes,
  KIND_FIELDS,
  KIND_META,
  kindBaseline,
  kindSwitchBase,
  kindDisabledReason,
  namedRowSeeds,
  NIGHTLY_LINE,
  normalizeKindResult,
  renameAt,
  renameProblem,
  SEED_NAME_IN_SETTINGS,
  ROW_FILLS,
  ROW_KINDS,
  ROW_SETTING_KEYS,
  rowKindOf,
  takeTurnsEnabled,
  visibleSettings,
  type RowFill,
  type RowKindChoice,
  type RowKindContext,
  type RowSettingKey,
} from "@/lib/row-kinds";
import { ROW_TEMPLATE_GROUPS } from "@/lib/row-templates";
import type { CollectionInput } from "@/lib/types";
import { FIXTURES as SAVED_ROWS } from "@/test/row-kind-fixtures";

const CTX: RowKindContext = {
  isDefault: false,
  globalMaxSeeds: 30,
  defaultRowName: "✨ {library_name} Picked for You",
  globalSources: ["tmdb_similar", "tmdb_discover"],
  seasonCatalogue: ["valentines", "halloween", "christmas"],
};
const DEFAULT_CTX: RowKindContext = { ...CTX, isDefault: true };

const PICKS_NAME = "✨ {library_name} Picks";
const BYW_NAME = "🎯 Because you watched {top_seed}";
const AGAIN_NAME = "☕ {library_name} you've already seen";
const POPULAR_NAME = "👥 Popular {library_name} on this server";

function named(name: string): Pick<CollectionInput, "name" | "name_template"> {
  return { name, name_template: name };
}

function row(patch: Partial<CollectionInput> = {}): CollectionInput {
  return { ...blankInput(), ...named(PICKS_NAME), ...patch };
}

/** One row per way of being each kind. `bywCount` is a Because you watched row by watch count alone. */
const FIXTURES = {
  picked: row(),
  bywNamed: row({ ...named(BYW_NAME), max_seeds: 2, refresh_days: 1 }),
  bywCount: row({ max_seeds: 2 }),
  again: row({ ...named(AGAIN_NAME), rewatch: true, watched_pct: 1 }),
  popular: row({ ...named(POPULAR_NAME), build: "shared", min_watchers: 3 }),
} satisfies Record<string, CollectionInput>;

const FIXTURE_FILL: Record<keyof typeof FIXTURES, RowFill> = {
  picked: "picked",
  bywNamed: "byw",
  bywCount: "byw",
  again: "again",
  popular: "popular",
};

const MEDIA = ["movie", "show", "both"] as const;

function seasonal(input: CollectionInput): CollectionInput {
  return { ...input, seasons: ["halloween"] };
}

/** Every target a picker can ask for: four plain kinds, and Seasonal with each fill. */
const TARGETS: RowKindChoice[] = [
  ...ROW_FILLS.map((fill) => ({ kind: fill, fill })),
  ...ROW_FILLS.map((fill) => ({ kind: "seasonal" as const, fill })),
];

describe("kind metadata", () => {
  it("lists the five kinds in the picker's order with the design's copy", () => {
    expect(ROW_KINDS).toEqual(["picked", "byw", "again", "seasonal", "popular"]);
    expect(KIND_META).toEqual({
      picked: {
        title: "Picked for You",
        description: "Titles they haven't seen yet, matched to everything they like.",
      },
      byw: {
        title: "Because you watched",
        description:
          'More like one thing they watched recently. Named after it, like "Because you watched Dune".',
      },
      again: {
        title: "Watch it again",
        description: "Favourites they've already finished, ready to rewatch.",
      },
      seasonal: {
        title: "Seasonal",
        description:
          "Only appears around the holidays you pick, like Halloween or Christmas. Filled in any of the ways above.",
      },
      popular: {
        title: "Popular on this server",
        description: "What lots of people here are watching. Everyone sees the same row.",
      },
    });
  });

  it("offers every kind but Seasonal as a fill, titled as the kind is", () => {
    expect(ROW_FILLS).toEqual(["picked", "byw", "again", "popular"]);
    for (const fill of ROW_FILLS) expect(FILL_META[fill]).toEqual(KIND_META[fill]);
  });
});

describe("the gallery", () => {
  it("groups its templates under the kinds' own titles and descriptions", () => {
    expect(ROW_TEMPLATE_GROUPS.map((group) => group.kind)).toEqual(ROW_KINDS);
    for (const group of ROW_TEMPLATE_GROUPS) {
      expect(group.heading).toBe(KIND_META[group.kind].title);
      expect(group.description).toBe(KIND_META[group.kind].description);
    }
  });
});

describe("namedRowSeeds", () => {
  it("is 2 for a movies-and-TV row and 1 for a single-media row", () => {
    expect(namedRowSeeds("both")).toBe(2);
    expect(namedRowSeeds("movie")).toBe(1);
    expect(namedRowSeeds("show")).toBe(1);
  });
});

describe("rowKindOf", () => {
  it.each(MEDIA)("reads each fixture as its kind on a %s row", (media) => {
    for (const [name, input] of Object.entries(FIXTURES)) {
      const fill = FIXTURE_FILL[name as keyof typeof FIXTURES];
      expect(rowKindOf({ ...input, media }, CTX), name).toEqual({ kind: fill, fill });
      expect(rowKindOf(seasonal({ ...input, media }), CTX), name).toEqual({ kind: "seasonal", fill });
    }
  });

  it("reads row 2's live state as a Because you watched blend", () => {
    const row2 = row({
      ...named(BYW_NAME),
      max_seeds: 3,
      seed_window: 1,
      media: "both",
      refresh_days: 1,
      build: "per_person",
      rewatch: false,
      seasons: [],
    });
    expect(rowKindOf(row2, CTX)).toEqual({ kind: "byw", fill: "byw" });
    expect(takeTurnsEnabled(row2, CTX)).toBe(false);
  });

  it("checks seasons, then shared, then rewatch, then the name and watch count", () => {
    const seed = named(BYW_NAME);
    expect(rowKindOf(row({ seasons: ["christmas"], build: "shared", rewatch: true }), CTX)).toEqual({
      kind: "seasonal",
      fill: "popular",
    });
    expect(rowKindOf(row({ seasons: ["christmas"], rewatch: true, ...seed }), CTX)).toEqual({
      kind: "seasonal",
      fill: "again",
    });
    expect(rowKindOf(row({ build: "shared", rewatch: true, ...seed, max_seeds: 1 }), CTX).kind).toBe("popular");
    expect(rowKindOf(row({ rewatch: true, ...seed, max_seeds: 1 }), CTX).kind).toBe("again");
    // A {top_seed} name wins over a large watch count: row 2 is a blend its owner calls Because you watched.
    expect(rowKindOf(row({ ...seed, max_seeds: 30 }), CTX).kind).toBe("byw");
  });

  it("reads an explicit watch count of 1 or 2 as Because you watched, and 3 or more as Picked for You", () => {
    expect(rowKindOf(row({ max_seeds: 1 }), CTX).kind).toBe("byw");
    expect(rowKindOf(row({ max_seeds: 2 }), CTX).kind).toBe("byw");
    expect(rowKindOf(row({ max_seeds: 3 }), CTX).kind).toBe("picked");
    expect(rowKindOf(row({ max_seeds: null }), CTX).kind).toBe("picked");
  });

  it("reads an inherited watch count through the global", () => {
    expect(rowKindOf(row({ max_seeds: null }), { ...CTX, globalMaxSeeds: 2 }).kind).toBe("byw");
    expect(rowKindOf(row({ max_seeds: null }), { ...CTX, globalMaxSeeds: 1 }).kind).toBe("byw");
    expect(rowKindOf(row({ max_seeds: null }), { ...CTX, globalMaxSeeds: 3 }).kind).toBe("picked");
    // An explicit value is the row's own, whatever the global says.
    expect(rowKindOf(row({ max_seeds: 10 }), { ...CTX, globalMaxSeeds: 2 }).kind).toBe("picked");
  });

  it("reads a new row's typed name when it has no saved template yet", () => {
    expect(rowKindOf(row({ name: BYW_NAME, name_template: "" }), CTX).kind).toBe("byw");
  });

  describe("on the default row", () => {
    const defaultRow = row({ name: "Picked for You", name_template: "" });

    it("reads the name from the global template, not the row's empty column", () => {
      expect(rowKindOf(defaultRow, DEFAULT_CTX).kind).toBe("picked");
      expect(rowKindOf(defaultRow, { ...DEFAULT_CTX, defaultRowName: BYW_NAME }).kind).toBe("byw");
    });

    it("ignores a {top_seed} in the row's own name column, which the engine never renders", () => {
      expect(rowKindOf({ ...defaultRow, name: BYW_NAME }, DEFAULT_CTX).kind).toBe("picked");
    });

    it.each(MEDIA)("reads inherited and explicit watch counts on a %s row", (media) => {
      const ctx2 = { ...DEFAULT_CTX, globalMaxSeeds: 2 };
      expect(rowKindOf({ ...defaultRow, media }, ctx2).kind).toBe("byw");
      expect(rowKindOf({ ...defaultRow, media, max_seeds: 5 }, ctx2).kind).toBe("picked");
      expect(rowKindOf({ ...defaultRow, media, max_seeds: 1 }, DEFAULT_CTX).kind).toBe("byw");
    });
  });
});

describe("kindDisabledReason", () => {
  const defaultRow = row({ name: "Picked for You", name_template: "" });

  it("refuses Seasonal on the default row and allows every other kind", () => {
    expect(kindDisabledReason("seasonal", defaultRow, DEFAULT_CTX)).toBe("The default row can't be seasonal");
    for (const kind of ROW_FILLS) expect(kindDisabledReason(kind, defaultRow, DEFAULT_CTX)).toBeNull();
  });

  it("refuses Seasonal until the season catalogue is known", () => {
    expect(kindDisabledReason("seasonal", row(), { ...CTX, seasonCatalogue: [] })).not.toBeNull();
    expect(kindDisabledReason("seasonal", row(), CTX)).toBeNull();
  });

  it("refuses only the kinds a {top_seed} name can't have on a default row whose Settings name follows one watch", () => {
    // Its name lives in Settings, so no switch made here can take the {top_seed} out of it. Picked for
    // You would read straight back as Because you watched, and a shared row has no watch to fill it
    // with; Watch it again is named after a watch by the engine too (`rows._names_a_seed`).
    const seedCtx = { ...DEFAULT_CTX, defaultRowName: BYW_NAME };
    for (const kind of ["picked", "popular"] as const) {
      expect(kindDisabledReason(kind, defaultRow, seedCtx), kind).toBe(SEED_NAME_IN_SETTINGS);
    }
    for (const kind of ["byw", "again"] as const) {
      expect(kindDisabledReason(kind, defaultRow, seedCtx), kind).toBeNull();
    }
    expect(kindDisabledReason("seasonal", defaultRow, seedCtx)).toBe("The default row can't be seasonal");
    expect(SEED_NAME_IN_SETTINGS).toBe(
      "This row's name (set in Settings) follows one watch. Change it in Settings first.",
    );
  });

  it("allows every kind on an ordinary row named after a watch, which the rename screen can rename", () => {
    for (const kind of ROW_FILLS) expect(kindDisabledReason(kind, FIXTURES.bywNamed, CTX)).toBeNull();
  });
});

// Literal on purpose: these are the design's §5 lists, not a copy of the module's tables.
const ALWAYS: RowSettingKey[] = [
  "kind",
  "name",
  "description",
  "poster",
  "audience",
  "libraries",
  "size",
  "pick_order",
  "schedule",
  "placement",
  "hub_anchor",
  "show_days",
  "sort_title_prefix",
  "enabled",
];

const EXTRA: Record<keyof typeof FIXTURES, RowSettingKey[]> = {
  picked: [
    "max_seeds",
    "cold_start",
    "candidate_sources",
    "watched_pct",
    "unstarted_only",
    "recency",
    "refresh_days",
    "idle_hold_days",
    "requests",
  ],
  // Named after a watch, so the engine forces nightly and How often it changes is hidden.
  bywNamed: [
    "based_on",
    "seed_window",
    "cold_start",
    "fallback_name",
    "candidate_sources",
    "watched_pct",
    "unstarted_only",
    "recency",
    "idle_hold_days",
    "requests",
  ],
  // Not named after a watch and not rotating: the engine honours its cadence, so the control stays.
  bywCount: [
    "based_on",
    "seed_window",
    "cold_start",
    "fallback_name",
    "candidate_sources",
    "watched_pct",
    "unstarted_only",
    "recency",
    "refresh_days",
    "idle_hold_days",
    "requests",
  ],
  again: [
    "rewatch_cooldown_days",
    "cold_start",
    "max_seeds",
    "candidate_sources",
    "recency",
    "refresh_days",
    "idle_hold_days",
    "requests",
  ],
  popular: ["min_watchers"],
};

function expected(
  fixture: keyof typeof FIXTURES,
  media: (typeof MEDIA)[number],
  opts: { isDefault?: boolean; seasonal?: boolean } = {},
): RowSettingKey[] {
  return [
    ...ALWAYS.filter((key) => !(opts.isDefault && key === "size")),
    ...EXTRA[fixture].filter((key) => !(media === "movie" && key === "unstarted_only")),
    ...(opts.seasonal ? (["seasons"] as const) : []),
  ].sort();
}

function visible(input: CollectionInput, ctx: RowKindContext = CTX): RowSettingKey[] {
  return [...visibleSettings(input, ctx)].sort();
}

describe("visibleSettings", () => {
  const fixtureNames = Object.keys(FIXTURES) as (keyof typeof FIXTURES)[];

  describe.each(MEDIA)("on a %s row", (media) => {
    it.each(fixtureNames)("shows exactly the %s settings", (name) => {
      expect(visible({ ...FIXTURES[name], media })).toEqual(expected(name, media));
    });

    it.each(fixtureNames)("shows exactly the %s settings on the default row, without size", (name) => {
      const defaultRow = { ...FIXTURES[name], media, name_template: "" };
      // The default row's name is the global's; point it at the fixture's name so the kind is the same.
      const ctx = { ...DEFAULT_CTX, defaultRowName: FIXTURES[name].name_template };
      expect(visible(defaultRow, ctx)).toEqual(expected(name, media, { isDefault: true }));
    });

    it.each(fixtureNames)("shows the %s fill's settings plus Which seasons on a seasonal row", (name) => {
      expect(visible(seasonal({ ...FIXTURES[name], media }))).toEqual(expected(name, media, { seasonal: true }));
    });
  });

  it("shows Rated by only under Highest rated", () => {
    expect(visibleSettings(row({ pick_order: "rating" }), CTX).has("rated_by")).toBe(true);
    expect(visibleSettings(row({ pick_order: "best" }), CTX).has("rated_by")).toBe(false);
  });

  it("shows Recent watches for AI web search only when that source is on, from the row or the global", () => {
    const web = ["tmdb_similar", "llm_web"];
    expect(visibleSettings(row({ candidate_sources: web }), CTX).has("recent_count")).toBe(true);
    expect(visibleSettings(row(), { ...CTX, globalSources: web }).has("recent_count")).toBe(true);
    expect(visibleSettings(row(), CTX).has("recent_count")).toBe(false);
    // A row's own list replaces the global, so the global's web search doesn't reach it.
    expect(
      visibleSettings(row({ candidate_sources: ["tmdb_similar"] }), { ...CTX, globalSources: web }).has("recent_count"),
    ).toBe(false);
    expect(visibleSettings(FIXTURES.again, { ...CTX, globalSources: web }).has("recent_count")).toBe(true);
    expect(visibleSettings(FIXTURES.bywNamed, { ...CTX, globalSources: web }).has("recent_count")).toBe(true);
    expect(visibleSettings(FIXTURES.popular, { ...CTX, globalSources: web }).has("recent_count")).toBe(false);
  });

  it("hides Recent watches for AI web search on a seasonal row, which the engine drops that source from", () => {
    const web = ["tmdb_similar", "llm_web"];
    expect(visibleSettings(seasonal(row({ candidate_sources: web })), CTX).has("recent_count")).toBe(false);
  });

  it("hides the hold on a Because you watched row that takes turns", () => {
    const turns = row({ max_seeds: 2, seed_window: 3 });
    const shown = visibleSettings(turns, CTX);
    expect(shown.has("idle_hold_days")).toBe(false);
    expect(shown.has("seed_window")).toBe(true);
    // Rotating forces nightly too.
    expect(shown.has("refresh_days")).toBe(false);
  });

  it("offers a fallback name on any kind whose name follows a watch, since the engine reads it there", () => {
    const againNamed = row({ ...named(BYW_NAME), rewatch: true });
    const shown = visibleSettings(againNamed, CTX);
    expect(rowKindOf(againNamed, CTX).kind).toBe("again");
    expect(shown.has("fallback_name")).toBe(true);
    expect(shown.has("refresh_days")).toBe(false);
  });

  it("follows the global name for the default row's fallback name and cadence", () => {
    const defaultRow = row({ name_template: "", name: "Picked for You" });
    const shown = visibleSettings(defaultRow, { ...DEFAULT_CTX, defaultRowName: BYW_NAME });
    expect(shown.has("fallback_name")).toBe(true);
    expect(shown.has("refresh_days")).toBe(false);
  });

  it("never changes the row it reads", () => {
    const input = row({ seed_window: 4, pick_order: "rating" });
    const before = structuredClone(input);
    visibleSettings(input, CTX);
    rowKindOf(input, CTX);
    hiddenButRead(input, CTX);
    expect(input).toEqual(before);
  });
});

describe("the setting register", () => {
  const NOT_EDITOR_CONTROLLED: Partial<Record<keyof CollectionInput, string>> = {
    defer_rename: "Always false here; only the rename screen defers a rename.",
    sort_order: "Row order is set on the Rows page, not in the editor.",
    req_min_votes: "No control; follows Settings > Requests (design §7).",
    req_auto_min_demand: "No control; follows Settings > Requests (design §7).",
    req_auto_min_rating: "No control; follows Settings > Requests (design §7).",
    req_min_rating_other: "No control; the language toggle only clears it back to Settings (design §7).",
  };

  // Every state the fixtures can be in, so "visible somewhere" is checked against real rows.
  const STATES: [CollectionInput, RowKindContext][] = [
    ...Object.values(FIXTURES).flatMap((input) => [
      [input, CTX] as [CollectionInput, RowKindContext],
      [seasonal(input), CTX] as [CollectionInput, RowKindContext],
      [{ ...input, pick_order: "rating" }, CTX] as [CollectionInput, RowKindContext],
      [{ ...input, candidate_sources: ["llm_web"] }, CTX] as [CollectionInput, RowKindContext],
    ]),
  ];

  it("names every field of the editor's input, derived from blankInput", () => {
    expect(Object.keys(FIELD_SETTING).sort()).toEqual(Object.keys(blankInput()).sort());
  });

  it.each(Object.keys(blankInput()) as (keyof CollectionInput)[])(
    "%s is either shown for some kind or listed as not editor-controlled",
    (field) => {
      const setting = FIELD_SETTING[field];
      if (setting === null) {
        expect(NOT_EDITOR_CONTROLLED[field], `${field} needs a reason`).toBeTruthy();
        return;
      }
      expect(NOT_EDITOR_CONTROLLED[field]).toBeUndefined();
      expect(ROW_SETTING_KEYS).toContain(setting);
      expect(STATES.some(([input, ctx]) => visibleSettings(input, ctx).has(setting))).toBe(true);
    },
  );

  it("shows every setting key for at least one kind or state", () => {
    const seen = new Set(STATES.flatMap(([input, ctx]) => [...visibleSettings(input, ctx)]));
    expect([...seen].sort()).toEqual([...ROW_SETTING_KEYS].sort());
  });
});

describe("hiddenButRead", () => {
  it("flags a rotation the kind hides but the engine still applies", () => {
    expect(hiddenButRead(row({ seed_window: 3 }), CTX)).toEqual(["seed_window"]);
    expect(hiddenButRead({ ...FIXTURES.again, seed_window: 2 }, CTX)).toEqual(["seed_window"]);
    expect(hiddenButRead(seasonal(row({ seed_window: 2 })), CTX)).toEqual(["seed_window"]);
  });

  it("flags a rotation shown disabled on a blend, where it still can't be changed", () => {
    expect(hiddenButRead({ ...FIXTURES.bywNamed, max_seeds: 3, seed_window: 3 }, CTX)).toEqual(["seed_window"]);
    expect(hiddenButRead({ ...FIXTURES.bywNamed, max_seeds: null, seed_window: 2 }, CTX)).toEqual(["seed_window"]);
  });

  it("stays quiet when the value is the default or the setting is usable", () => {
    expect(hiddenButRead(row(), CTX)).toEqual([]);
    expect(hiddenButRead({ ...FIXTURES.bywNamed, seed_window: 3 }, CTX)).toEqual([]);
    expect(hiddenButRead({ ...FIXTURES.bywNamed, max_seeds: null, seed_window: 3 }, { ...CTX, globalMaxSeeds: 2 })).toEqual(
      [],
    );
  });

  it("ignores a rotation on a shared row, which the shared builder never reads", () => {
    expect(hiddenButRead({ ...FIXTURES.popular, seed_window: 3 }, CTX)).toEqual([]);
  });

  it("ignores hidden values the engine doesn't read", () => {
    expect(hiddenButRead(row({ min_watchers: 9, rewatch_cooldown_days: 5 }), CTX)).toEqual([]);
    expect(hiddenButRead({ ...FIXTURES.popular, max_seeds: 1, cold_start: "skip", watched_pct: 1 }, CTX)).toEqual([]);
    expect(hiddenButRead({ ...FIXTURES.again, watched_pct: 0 }, CTX)).toEqual([]);
  });
});

describe("takeTurnsEnabled", () => {
  it("allows rotation only on a row built from one or two watches", () => {
    expect(takeTurnsEnabled(row({ max_seeds: 1 }), CTX)).toBe(true);
    expect(takeTurnsEnabled(row({ max_seeds: 2 }), CTX)).toBe(true);
    expect(takeTurnsEnabled(row({ max_seeds: 3 }), CTX)).toBe(false);
    expect(takeTurnsEnabled(row({ max_seeds: null }), CTX)).toBe(false);
    expect(takeTurnsEnabled(row({ max_seeds: null }), { ...CTX, globalMaxSeeds: 2 })).toBe(true);
  });
});

describe("applyRowKind", () => {
  it("→ Picked for You: per person, no rewatch, no rotation, inherit the watch count", () => {
    const from = { ...FIXTURES.bywCount, seed_window: 3 };
    const out = applyRowKind(from, { kind: "picked", fill: "picked" }, CTX);
    expect(out).toEqual({ ...from, build: "per_person", rewatch: false, seed_window: 1, max_seeds: null });
  });

  it.each([1, 2])("→ Picked for You sets 3 watches when the global is %i, or it would read back", (global) => {
    const ctx = { ...CTX, globalMaxSeeds: global };
    for (const from of [FIXTURES.again, FIXTURES.bywCount, row({ max_seeds: 1 })]) {
      const out = applyRowKind(from, { kind: "picked", fill: "picked" }, ctx);
      expect(out.max_seeds).toBe(3);
      expect(rowKindOf(out, ctx).kind).toBe("picked");
    }
  });

  it("→ Picked for You keeps an explicit watch count of 3 or more", () => {
    const blend = row({ ...named(BYW_NAME), max_seeds: 3 });
    const blendOut = applyRowKind(blend, { kind: "picked", fill: "picked" }, CTX);
    expect(blendOut.max_seeds).toBe(3);
    expect(rowKindOf({ ...blendOut, ...named(PICKS_NAME) }, CTX).kind).toBe("picked");

    const again = { ...FIXTURES.again, max_seeds: 10 };
    const againOut = applyRowKind(again, { kind: "picked", fill: "picked" }, CTX);
    expect(againOut).toEqual({ ...again, rewatch: false });
    expect(rowKindOf(againOut, CTX).kind).toBe("picked");
  });

  it("→ Picked for You leaves an inherited count alone when the global is 3 or more", () => {
    const out = applyRowKind(FIXTURES.again, { kind: "picked", fill: "picked" }, { ...CTX, globalMaxSeeds: 3 });
    expect(out.max_seeds).toBeNull();
    expect(rowKindOf(out, { ...CTX, globalMaxSeeds: 3 }).kind).toBe("picked");
  });

  it.each([
    ["both", 2],
    ["movie", 1],
    ["show", 1],
  ] as const)("→ Because you watched on a %s row: %i watches, rotation kept", (media, seeds) => {
    const from = { ...FIXTURES.again, media, seed_window: 4 };
    const out = applyRowKind(from, { kind: "byw", fill: "byw" }, CTX);
    expect(out).toEqual({ ...from, build: "per_person", rewatch: false, max_seeds: seeds });
  });

  it("→ Because you watched on a row that already is one changes nothing but the seasons", () => {
    const blend = row({ ...named(BYW_NAME), max_seeds: 3, seed_window: 1 });
    expect(applyRowKind(seasonal(blend), { kind: "byw", fill: "byw" }, CTX)).toEqual(blend);
    expect(applyRowKind(blend, { kind: "seasonal", fill: "byw" }, CTX)).toEqual({ ...blend, seasons: CTX.seasonCatalogue });
    expect(applyRowKind(blend, { kind: "byw", fill: "byw" }, CTX)).toEqual(blend);
  });

  it("→ Watch it again: rewatch on, series they've started allowed, no rotation", () => {
    const from = row({ watched_pct: 0, unstarted_only: true, seed_window: 2, media: "show" });
    const out = applyRowKind(from, { kind: "again", fill: "again" }, CTX);
    expect(out).toEqual({
      ...from,
      build: "per_person",
      rewatch: true,
      unstarted_only: false,
      seed_window: 1,
    });
  });

  it("→ Watch it again leaves the already-watched cap alone, 0% included", () => {
    // The kind hides the slider and the engine ignores the cap on a rewatch row, so raising it did
    // nothing but lose the value the row goes back to.
    for (const watched_pct of [0, null, 0.5, 1]) {
      expect(applyRowKind(row({ watched_pct }), { kind: "again", fill: "again" }, CTX).watched_pct).toBe(watched_pct);
    }
  });

  it("→ Popular on this server: shared, request tag cleared, rotation untouched", () => {
    const from = row({ request_tag: "family", seed_window: 3, max_seeds: 10, cold_start: "skip" });
    const out = applyRowKind(from, { kind: "popular", fill: "popular" }, CTX);
    expect(out).toEqual({ ...from, build: "shared", request_tag: "" });
  });

  it("→ Seasonal: every season in the catalogue, days unchanged, fill kept", () => {
    const from = { ...FIXTURES.again, season_lead_days: 12, season_after_days: 3 };
    const out = applyRowKind(from, { kind: "seasonal", fill: "again" }, CTX);
    expect(out).toEqual({ ...from, seasons: ["valentines", "halloween", "christmas"] });
  });

  it("→ Seasonal with a new fill applies both", () => {
    const out = applyRowKind(FIXTURES.picked, { kind: "seasonal", fill: "popular" }, CTX);
    expect(out).toEqual({ ...FIXTURES.picked, seasons: CTX.seasonCatalogue, build: "shared", request_tag: "" });
  });

  it("switching only a seasonal row's fill leaves its seasons alone", () => {
    const from = { ...FIXTURES.picked, seasons: ["christmas"] };
    const out = applyRowKind(from, { kind: "seasonal", fill: "again" }, CTX);
    expect(out.seasons).toEqual(["christmas"]);
    expect(out.rewatch).toBe(true);
  });

  it("leaving Seasonal clears the seasons and keeps the days", () => {
    const from = { ...seasonal(FIXTURES.picked), season_lead_days: 9 };
    expect(applyRowKind(from, { kind: "picked", fill: "picked" }, CTX)).toEqual({ ...from, seasons: [] });
  });

  it("ignores the fill on a kind that isn't Seasonal", () => {
    expect(applyRowKind(FIXTURES.picked, { kind: "picked", fill: "popular" }, CTX)).toEqual(FIXTURES.picked);
  });

  it("leaves hidden settings the engine ignores as they were, so switching back restores them", () => {
    const tuned = row({ max_seeds: 12, cold_start: "skip", watched_pct: 0.3, recency: 0.2, refresh_days: 5 });
    const shared = applyRowKind(tuned, { kind: "popular", fill: "popular" }, CTX);
    const back = applyRowKind(shared, { kind: "picked", fill: "picked" }, CTX);
    expect(back).toEqual(tuned);

    const again = row({ ...named(AGAIN_NAME), rewatch: true, rewatch_cooldown_days: 7, watched_pct: 1 });
    const picked = applyRowKind(again, { kind: "picked", fill: "picked" }, CTX);
    expect(picked.rewatch_cooldown_days).toBe(7);
    expect(picked.watched_pct).toBe(1);
  });

  it("refuses Seasonal on the default row and before the catalogue loads", () => {
    expect(() => applyRowKind(row(), { kind: "seasonal", fill: "picked" }, DEFAULT_CTX)).toThrow();
    expect(() => applyRowKind(row(), { kind: "seasonal", fill: "picked" }, { ...CTX, seasonCatalogue: [] })).toThrow();
  });

  it("returns a new object and never mutates its input", () => {
    const input = row({ seed_window: 3, request_tag: "x" });
    const before = structuredClone(input);
    for (const target of TARGETS) {
      const out = applyRowKind(input, target, CTX);
      expect(out).not.toBe(input);
    }
    expect(input).toEqual(before);
  });

  describe("touches nothing outside its documented patch", () => {
    const FILL_FIELDS: Record<RowFill, (keyof CollectionInput)[]> = {
      picked: ["build", "rewatch", "seed_window", "max_seeds"],
      byw: ["build", "rewatch", "max_seeds"],
      again: ["build", "rewatch", "unstarted_only", "seed_window"],
      popular: ["build", "request_tag"],
    };
    const froms = Object.entries(FIXTURES).flatMap(([name, input]) =>
      MEDIA.flatMap((media) => {
        const base = { ...input, media, seed_window: 2, request_tag: "tag", watched_pct: 0, unstarted_only: true };
        return [
          [`${name}/${media}`, base],
          [`seasonal ${name}/${media}`, seasonal(base)],
        ] as [string, CollectionInput][];
      }),
    );

    it.each(froms)("from %s", (_, from) => {
      const current = rowKindOf(from, CTX);
      for (const target of TARGETS) {
        const out = applyRowKind(from, target, CTX);
        const targetFill = target.kind === "seasonal" ? target.fill : target.kind;
        const allowed = new Set<keyof CollectionInput>([
          ...((target.kind === "seasonal") !== (current.kind === "seasonal") ? (["seasons"] as const) : []),
          ...(targetFill !== current.fill ? FILL_FIELDS[targetFill] : []),
        ]);
        for (const field of Object.keys(from) as (keyof CollectionInput)[]) {
          if (allowed.has(field)) continue;
          expect(JSON.stringify(out[field]), `${field} → ${target.kind}/${target.fill}`).toBe(
            JSON.stringify(from[field]),
          );
        }
      }
    });

    it("KIND_FIELDS lists exactly the fields some switch writes, no more and no fewer", () => {
      const written = new Set<keyof CollectionInput>();
      for (const [, from] of froms) {
        for (const ctx of [CTX, { ...CTX, globalMaxSeeds: 2 }]) {
          for (const target of TARGETS) {
            const out = applyRowKind(from, target, ctx);
            for (const field of Object.keys(from) as (keyof CollectionInput)[]) {
              if (JSON.stringify(out[field]) !== JSON.stringify(from[field])) written.add(field);
            }
          }
        }
      }
      expect([...written].sort()).toEqual([...KIND_FIELDS].sort());
    });
  });
});

describe("switching from the kind baseline", () => {
  const OWN = (input: CollectionInput, ctx: RowKindContext): RowKindChoice => rowKindOf(input, ctx);
  const PICKED = { kind: "picked", fill: "picked" } as const;

  it("keeps the kind fields and the name as the baseline, and nothing else", () => {
    const input = row({ ...named(BYW_NAME), max_seeds: 2, request_tag: "x", seasons: ["christmas"], size: 9 });
    expect(Object.keys(kindBaseline(input)).sort()).toEqual([...KIND_FIELDS, "name", "name_template"].sort());
    const drifted = { ...input, build: "shared" as const, max_seeds: 30, size: 12, name: "Other", name_template: "" };
    expect(kindSwitchBase(drifted, kindBaseline(input), PICKED, CTX)).toEqual({ ...input, size: 12 });
  });

  it("keeps the seasons on screen while the row stays seasonal", () => {
    const loaded = row();
    const narrowed = { ...applyRowKind(loaded, { kind: "seasonal", fill: "picked" }, CTX), seasons: ["halloween"] };
    const base = kindSwitchBase(narrowed, kindBaseline(loaded), { kind: "seasonal", fill: "again" }, CTX);
    expect(applyRowKind(base, { kind: "seasonal", fill: "again" }, CTX).seasons).toEqual(["halloween"]);
  });

  it("enters Seasonal from the kind on screen, keeping what it shows", () => {
    const loaded = row();
    const onScreen = { ...applyRowKind(loaded, { kind: "byw", fill: "byw" }, CTX), seed_window: 5 };
    const choice = { kind: "seasonal", fill: "byw" } as const;
    const out = applyRowKind(kindSwitchBase(onScreen, kindBaseline(loaded), choice, CTX), choice, CTX);
    expect(out).toEqual({ ...onScreen, seasons: CTX.seasonCatalogue });
  });

  it("enters Seasonal with the loaded seasons when the row was loaded seasonal", () => {
    const loaded = row({ seasons: ["christmas"] });
    const away = applyRowKind(loaded, { kind: "again", fill: "again" }, CTX);
    const choice = { kind: "seasonal", fill: "again" } as const;
    expect(applyRowKind(kindSwitchBase(away, kindBaseline(loaded), choice, CTX), choice, CTX).seasons).toEqual([
      "christmas",
    ]);
  });

  it.each(SAVED_ROWS)("%s: every kind and back to its own leaves the row exactly as it was", (_, collection, ctx) => {
    const loaded = toInput(collection);
    const baseline = kindBaseline(loaded);
    const own = OWN(loaded, ctx);
    for (const target of TARGETS) {
      if (kindDisabledReason(target.kind, loaded, ctx) !== null) continue;
      let at = applyRowKind(kindSwitchBase(loaded, baseline, target, ctx), target, ctx);
      // Seasonal from the picker keeps the kind on screen; its fill is then picked under "How it's filled".
      const steps: RowKindChoice[] =
        own.kind === "seasonal" ? [{ kind: "seasonal", fill: rowKindOf(at, ctx).fill }, own] : [own];
      for (const step of steps) at = applyRowKind(kindSwitchBase(at, baseline, step, ctx), step, ctx);
      expect(JSON.stringify(at), `→ ${target.kind}/${target.fill} and back`).toBe(JSON.stringify(loaded));
    }
  });

  it.each(SAVED_ROWS)("%s: a switch away from the baseline's fill ignores the one before it", (_, collection, ctx) => {
    const loaded = toInput(collection);
    const baseline = kindBaseline(loaded);
    const allowed = TARGETS.filter((target) => kindDisabledReason(target.kind, loaded, ctx) === null);
    for (const first of allowed) {
      const away = applyRowKind(kindSwitchBase(loaded, baseline, first, ctx), first, ctx);
      for (const last of allowed) {
        // Entering Seasonal on the fill already on screen is the one switch that builds on the last.
        if (last.kind === "seasonal" && rowKindOf(away, ctx).kind !== "seasonal" && last.fill === rowKindOf(away, ctx).fill)
          continue;
        expect(
          applyRowKind(kindSwitchBase(away, baseline, last, ctx), last, ctx),
          `${first.kind}/${first.fill} then ${last.kind}/${last.fill}`,
        ).toEqual(applyRowKind(loaded, last, ctx));
      }
    }
  });
});

describe("normalizeKindResult: a switched row holds to the API's pairing rules", () => {
  // `collections._validate_pairing` refuses unstarted_only with rewatch, and on a movies-only row.
  const cells = [false, true].flatMap((rewatch) =>
    MEDIA.flatMap((media) => [false, true].map((unstarted_only) => ({ rewatch, media, unstarted_only }))),
  );

  it.each(cells)("rewatch $rewatch, $media, unstarted_only $unstarted_only", (cell) => {
    const input = row(cell);
    const refused = cell.unstarted_only && (cell.rewatch || cell.media === "movie");
    expect(normalizeKindResult(input)).toEqual(refused ? { ...input, unstarted_only: false } : input);
  });

  it("never changes the row it reads", () => {
    const input = row({ rewatch: true, unstarted_only: true });
    const before = structuredClone(input);
    normalizeKindResult(input);
    expect(input).toEqual(before);
  });
});

describe("baselineTakes: which hand edits become the baseline", () => {
  const loaded = row();

  it("takes an edit made through a setting the baseline kind shows too", () => {
    const onScreen = { ...loaded, unstarted_only: false };
    expect(baselineTakes("unstarted_only", onScreen, loaded, CTX)).toBe(true);
    expect(baselineTakes("name", onScreen, loaded, CTX)).toBe(true);
  });

  it("leaves out an edit made through a setting only the switched-to kind shows", () => {
    const byw = applyRowKind(loaded, { kind: "byw", fill: "byw" }, CTX);
    // Picked for You has no Take turns, and its watch count is a number, not Based on.
    expect(baselineTakes("seed_window", byw, loaded, CTX)).toBe(false);
    expect(baselineTakes("max_seeds", byw, loaded, CTX)).toBe(false);
    // Picked for You follows no seasons.
    const seasonalRow = applyRowKind(loaded, { kind: "seasonal", fill: "picked" }, CTX);
    expect(baselineTakes("seasons", seasonalRow, loaded, CTX)).toBe(false);
  });

  it("takes a Reset on a rotation the baseline kind shows only to reset it", () => {
    const stale = row({ seed_window: 3 });
    expect(baselineTakes("seed_window", stale, stale, CTX)).toBe(true);
  });
});

describe("round trip: the kind asked for is the kind read back", () => {
  const froms: [string, CollectionInput, RowKindContext][] = Object.entries(FIXTURES).flatMap(([name, input]) =>
    MEDIA.flatMap((media) => {
      const base = { ...input, media };
      const defaultRow = { ...base, name_template: "" };
      const defaultCtx = { ...DEFAULT_CTX, defaultRowName: input.name_template };
      return [
        [`${name}/${media}`, base, CTX],
        [`seasonal ${name}/${media}`, seasonal(base), CTX],
        [`${name}/${media} with a global of 2 watches`, base, { ...CTX, globalMaxSeeds: 2 }],
        [`default ${name}/${media}`, defaultRow, defaultCtx],
      ] as [string, CollectionInput, RowKindContext][];
    }),
  );

  /** Applies the switch, and the rename the dialog asks for when it asks for one. */
  function switchKind(
    from: CollectionInput,
    target: RowKindChoice,
    ctx: RowKindContext,
  ): [CollectionInput, RowKindContext] {
    const change = describeKindChange(from, target, ctx);
    const out = applyRowKind(from, target, ctx);
    if (change.rename?.required) {
      return [{ ...out, name: change.rename.proposed, name_template: change.rename.proposed }, ctx];
    }
    if (change.renameInSettings?.required) {
      return [out, { ...ctx, defaultRowName: change.renameInSettings.proposed }];
    }
    return [out, ctx];
  }

  it.each(froms)("from %s", (_, from, ctx) => {
    for (const target of TARGETS) {
      if (kindDisabledReason(target.kind, from, ctx) !== null) continue;
      const [out, outCtx] = switchKind(from, target, ctx);
      const want = { kind: target.kind, fill: target.kind === "seasonal" ? target.fill : target.kind };
      expect(rowKindOf(out, outCtx), `→ ${target.kind}/${target.fill}`).toEqual(want);
    }
  });

  it.each(ROW_FILLS)(
    "holds for a seasonal %s row named after the season, under any new name the rename accepts",
    (fill) => {
      const base = row({
        ...named("{season_emoji} {season} picks"),
        ...(fill === "byw" ? { max_seeds: 2 } : fill === "again" ? { rewatch: true } : {}),
        ...(fill === "popular" ? { build: "shared" as const } : {}),
      });
      const from = seasonal(base);
      for (const target of TARGETS.filter((choice) => choice.kind !== "seasonal")) {
        const rename = describeKindChange(from, target, CTX).rename;
        if (!rename) throw new Error(`no rename leaving Seasonal for ${target.kind}`);
        for (const name of [rename.proposed, "Halloween all year", `Because you watched ${TOP_SEED}`]) {
          if (renameProblem(name, rename.mustNotUse) !== null) continue;
          const out = { ...applyRowKind(from, target, CTX), name, name_template: name };
          expect(rowKindOf(out, CTX), `→ ${target.kind} named "${name}"`).toEqual({
            kind: target.kind,
            fill: target.kind,
          });
        }
      }
    },
  );

  it("holds for a {top_seed} row leaving Because you watched only once it is renamed", () => {
    const from = FIXTURES.bywNamed;
    const out = applyRowKind(from, { kind: "picked", fill: "picked" }, CTX);
    expect(rowKindOf(out, CTX).kind).toBe("byw");
    const change = describeKindChange(from, { kind: "picked", fill: "picked" }, CTX);
    expect(change.rename).toEqual({ required: true, proposed: PICKS_NAME, mustNotUse: [TOP_SEED], because: [TOP_SEED] });
    const renamed = { ...out, name: PICKS_NAME, name_template: PICKS_NAME };
    expect(rowKindOf(renamed, CTX).kind).toBe("picked");
  });

  it("holds for the default row named after a watch only once Settings renames it", () => {
    const from = row({ name_template: "", name: "Picked for You" });
    const ctx = { ...DEFAULT_CTX, defaultRowName: BYW_NAME };
    const out = applyRowKind(from, { kind: "picked", fill: "picked" }, ctx);
    expect(rowKindOf(out, ctx).kind).toBe("byw");
    const change = describeKindChange(from, { kind: "picked", fill: "picked" }, ctx);
    expect(change.rename).toBeNull();
    expect(change.renameInSettings).toEqual({
      required: true,
      proposed: PICKS_NAME,
      mustNotUse: [TOP_SEED],
      because: [TOP_SEED],
    });
    expect(rowKindOf(out, { ...ctx, defaultRowName: PICKS_NAME }).kind).toBe("picked");
  });
});

describe("describeKindChange", () => {
  const PER_PERSON_TO_SHARED =
    "Saving removes everyone's own copy of this row from Plex right away. One shared row, the same for everyone who gets it, is built the next time the row runs, if enough people have watched titles in common. Until then, nobody has this row.";
  const SHARED_TO_PER_PERSON =
    "Saving removes the shared row from Plex right away. Each person gets their own private row the next time the row runs. Until then, nobody has this row.";
  const SEASONS =
    "Saving applies the season dates on Plex straight away. If today is outside them, the row is hidden now.";
  const NEXT_RUN = "Nothing changes on Plex until you save and the row next runs.";
  const LEAVING_SEASONS =
    "If it's hidden between seasons right now, saving puts it back on Plex straight away, still holding the titles from its last season.";

  describe("the Plex note", () => {
    it("warns that per person → shared deletes everyone's copy at save", () => {
      expect(describeKindChange(FIXTURES.picked, { kind: "popular", fill: "popular" }, CTX).plexNote).toBe(
        PER_PERSON_TO_SHARED,
      );
    });

    it("warns that shared → per person deletes the shared row at save", () => {
      expect(describeKindChange(FIXTURES.popular, { kind: "picked", fill: "picked" }, CTX).plexNote).toBe(
        SHARED_TO_PER_PERSON,
      );
    });

    it("gives only the build note when one switch changes the build and the seasons", () => {
      // `plan_row_changes` returns straight after a build flip, so no season pass runs at that save.
      expect(describeKindChange(FIXTURES.again, { kind: "seasonal", fill: "popular" }, CTX).plexNote).toBe(
        PER_PERSON_TO_SHARED,
      );
      expect(describeKindChange(seasonal(FIXTURES.popular), { kind: "byw", fill: "byw" }, CTX).plexNote).toBe(
        SHARED_TO_PER_PERSON,
      );
    });

    it("says the season dates apply at save when switching to Seasonal", () => {
      expect(describeKindChange(FIXTURES.picked, { kind: "seasonal", fill: "picked" }, CTX).plexNote).toBe(SEASONS);
    });

    it("says a row hidden between seasons comes back at save when it stops being seasonal", () => {
      // The seasons change queues `rows.visibility` for this row at save; the row is no longer dormant,
      // so promotion puts its existing collections back on the shelves its placement asks for.
      expect(describeKindChange(seasonal(FIXTURES.again), { kind: "again", fill: "again" }, CTX).plexNote).toBe(
        LEAVING_SEASONS,
      );
    });

    it("compares with the row as saved, not with a switch the owner hasn't saved", () => {
      const saved = FIXTURES.picked;
      const unsavedShared = applyRowKind(saved, { kind: "popular", fill: "popular" }, CTX);
      // Back to what is saved: saving changes nothing on Plex, whatever the form went through.
      expect(describeKindChange(saved, { kind: "picked", fill: "picked" }, CTX, { saved }).plexNote).toBe(NEXT_RUN);
      expect(
        describeKindChange(unsavedShared, { kind: "seasonal", fill: "popular" }, CTX, { saved }).plexNote,
      ).toBe(PER_PERSON_TO_SHARED);
    });

    it("says a switched-off row isn't on Plex, build flip included, without promising nothing happens at save", () => {
      // An off row has no collections and doesn't run, but a build flip still queues a share-filter
      // pass at save (`plan_row_changes`), so the note can't say nothing changes on Plex.
      const OFF = "This row is switched off, so it isn't on Plex. These settings apply when you turn it back on.";
      for (const [from, choice] of [
        [FIXTURES.picked, { kind: "popular", fill: "popular" }],
        [FIXTURES.picked, { kind: "seasonal", fill: "picked" }],
        [seasonal(FIXTURES.picked), { kind: "picked", fill: "picked" }],
        [FIXTURES.picked, { kind: "byw", fill: "byw" }],
      ] as [CollectionInput, RowKindChoice][]) {
        const off = { ...from, enabled: false };
        expect(describeKindChange(off, choice, CTX).plexNote, `${choice.kind}/${choice.fill}`).toBe(OFF);
      }
    });

    it("adds that runs are paused to every note that waits on a run or a Plex pass", () => {
      const PAUSED = " (Runs are paused in Settings, so this waits until they're resumed.)";
      const paused = { ...CTX, pausedAll: true };
      expect(describeKindChange(FIXTURES.picked, { kind: "byw", fill: "byw" }, paused).plexNote).toBe(NEXT_RUN + PAUSED);
      expect(describeKindChange(FIXTURES.picked, { kind: "popular", fill: "popular" }, paused).plexNote).toBe(
        PER_PERSON_TO_SHARED + PAUSED,
      );
      expect(describeKindChange(FIXTURES.picked, { kind: "seasonal", fill: "picked" }, paused).plexNote).toBe(
        SEASONS + PAUSED,
      );
      expect(describeKindChange(seasonal(FIXTURES.again), { kind: "again", fill: "again" }, paused).plexNote).toBe(
        LEAVING_SEASONS + PAUSED,
      );
    });

    it("says nothing changes until the next run for every other switch", () => {
      expect(describeKindChange(FIXTURES.picked, { kind: "byw", fill: "byw" }, CTX).plexNote).toBe(NEXT_RUN);
      expect(describeKindChange(FIXTURES.bywNamed, { kind: "again", fill: "again" }, CTX).plexNote).toBe(NEXT_RUN);
      expect(
        describeKindChange(seasonal(FIXTURES.picked), { kind: "seasonal", fill: "again" }, CTX).plexNote,
      ).toBe(NEXT_RUN);
      expect(describeKindChange(FIXTURES.popular, { kind: "popular", fill: "popular" }, CTX).plexNote).toBe(NEXT_RUN);
    });
  });

  describe("the rename", () => {
    it.each([
      ["picked", PICKS_NAME],
      ["popular", POPULAR_NAME],
    ] as const)("asks a {top_seed} row switched to %s for that kind's template name", (fill, name) => {
      expect(describeKindChange(FIXTURES.bywNamed, { kind: fill, fill }, CTX).rename).toEqual({
        required: true,
        proposed: name,
        mustNotUse: [TOP_SEED],
        because: [TOP_SEED],
      });
      expect(describeKindChange(FIXTURES.bywNamed, { kind: "seasonal", fill }, CTX).rename).toEqual({
        required: true,
        proposed: name,
        mustNotUse: [TOP_SEED],
        because: [TOP_SEED],
      });
    });

    describe("a name that uses the season, on a row leaving Seasonal", () => {
      // The API refuses {season} or {season_emoji} on a row that follows no season
      // (`collections._reject_season_name_without_seasons`), so the old name can't stay.
      const SEASON_NAME = "{season_emoji} {season} picks";

      it.each([
        ["picked", PICKS_NAME],
        ["byw", BYW_NAME],
        ["again", AGAIN_NAME],
        ["popular", POPULAR_NAME],
      ] as const)("requires a new name on the way to %s, proposing that kind's template name", (fill, name) => {
        const from = seasonal(row({ ...named(SEASON_NAME), ...(fill === "picked" ? { rewatch: true } : {}) }));
        // {top_seed} would make Picked for You read back as Because you watched, and a shared row has no
        // watch to fill it with (`rows._shared_row`), so those two refuse it as well.
        expect(describeKindChange(from, { kind: fill, fill }, CTX).rename).toEqual({
          required: true,
          proposed: name,
          mustNotUse: [...(fill === "picked" || fill === "popular" ? [TOP_SEED] : []), SEASON, SEASON_EMOJI],
          because: [SEASON, SEASON_EMOJI],
        });
      });

      it("requires one even when the fill stays the same", () => {
        const from = seasonal(row(named(SEASON_NAME)));
        expect(describeKindChange(from, { kind: "picked", fill: "picked" }, CTX).rename).toEqual({
          required: true,
          proposed: PICKS_NAME,
          mustNotUse: [TOP_SEED, SEASON, SEASON_EMOJI],
          because: [SEASON, SEASON_EMOJI],
        });
      });

      it("asks nothing while the row stays seasonal", () => {
        const from = seasonal(row(named(SEASON_NAME)));
        expect(describeKindChange(from, { kind: "seasonal", fill: "again" }, CTX).rename).toBeNull();
      });

      it("keeps {top_seed} allowed on the way to Because you watched and Watch it again, and refuses both elsewhere", () => {
        const from = seasonal(row({ ...named("🎯 {season}: because you watched {top_seed}"), max_seeds: 2 }));
        for (const [fill, proposed] of [
          ["byw", BYW_NAME],
          ["again", AGAIN_NAME],
        ] as const) {
          expect(describeKindChange(from, { kind: fill, fill }, CTX).rename, fill).toEqual({
            required: true,
            proposed,
            mustNotUse: [SEASON, SEASON_EMOJI],
            because: [SEASON, SEASON_EMOJI],
          });
        }
        for (const [fill, proposed] of [
          ["picked", PICKS_NAME],
          ["popular", POPULAR_NAME],
        ] as const) {
          expect(describeKindChange(from, { kind: fill, fill }, CTX).rename, fill).toEqual({
            required: true,
            proposed,
            mustNotUse: [TOP_SEED, SEASON, SEASON_EMOJI],
            because: [TOP_SEED, SEASON, SEASON_EMOJI],
          });
        }
      });

      it("lets a Watch it again row leaving Seasonal keep {top_seed}, and says what each name does", () => {
        const from = seasonal(row({ ...named("{season} rewatch: {top_seed}"), rewatch: true, watched_pct: 1 }));
        const choice = { kind: "again", fill: "again" } as const;
        const rename = describeKindChange(from, choice, CTX).rename;
        expect(rename?.mustNotUse).toEqual([SEASON, SEASON_EMOJI]);
        expect(renameProblem("Rewatch: {top_seed}", rename?.mustNotUse ?? [])).toBeNull();

        const kept = describeKindChange(from, choice, CTX, { renameTo: "Rewatch: {top_seed}" }).lines;
        expect(kept).toContain(NIGHTLY_LINE);
        const proposed = describeKindChange(from, choice, CTX).lines;
        expect(proposed).not.toContain(NIGHTLY_LINE);
        expect(proposed.join(" ")).toContain("How often it changes");
      });

      it("reads a new row's typed name the same way, since the Name box takes the proposal", () => {
        const from = seasonal(row({ name: SEASON_NAME, name_template: "" }));
        expect(describeKindChange(from, { kind: "picked", fill: "picked" }, CTX).rename?.proposed).toBe(PICKS_NAME);
      });
    });

    it("keeps a {top_seed} row's name on the way to Watch it again, which the engine names after a watch too", () => {
      // `rows._names_a_seed` reads the name whatever the fill, so the row still follows a watch and
      // rebuilds nightly, and the dialog says so.
      for (const choice of [
        { kind: "again", fill: "again" },
        { kind: "seasonal", fill: "again" },
      ] as RowKindChoice[]) {
        const change = describeKindChange(FIXTURES.bywNamed, choice, CTX);
        expect(change.rename, choice.kind).toBeNull();
        expect(change.lines, choice.kind).toContain(NIGHTLY_LINE);
      }
    });

    it("keeps the name when a {top_seed} row stays Because you watched", () => {
      expect(describeKindChange(FIXTURES.bywNamed, { kind: "seasonal", fill: "byw" }, CTX).rename).toBeNull();
      expect(describeKindChange(seasonal(FIXTURES.bywNamed), { kind: "byw", fill: "byw" }, CTX).rename).toBeNull();
    });

    it("offers an optional Because you watched name to a row switched to it without one", () => {
      expect(describeKindChange(FIXTURES.picked, { kind: "byw", fill: "byw" }, CTX).rename).toEqual({
        required: false,
        proposed: BYW_NAME,
        mustNotUse: [],
        because: [],
      });
      expect(describeKindChange(FIXTURES.again, { kind: "seasonal", fill: "byw" }, CTX).rename).toEqual({
        required: false,
        proposed: BYW_NAME,
        mustNotUse: [],
        because: [],
      });
    });

    it("offers nothing to a row already Because you watched by its watch count", () => {
      expect(describeKindChange(FIXTURES.bywCount, { kind: "seasonal", fill: "byw" }, CTX).rename).toBeNull();
    });

    it("offers nothing when the name has nothing to do with the switch", () => {
      expect(describeKindChange(FIXTURES.picked, { kind: "again", fill: "again" }, CTX).rename).toBeNull();
      expect(describeKindChange(FIXTURES.again, { kind: "popular", fill: "popular" }, CTX).rename).toBeNull();
    });

    it("points the default row to Settings instead of offering a rename", () => {
      const defaultRow = row({ name_template: "", name: "Picked for You" });
      const seedCtx = { ...DEFAULT_CTX, defaultRowName: BYW_NAME };
      const leaving = describeKindChange(defaultRow, { kind: "picked", fill: "picked" }, seedCtx);
      expect(leaving.rename).toBeNull();
      expect(leaving.renameInSettings).toEqual({
        required: true,
        proposed: PICKS_NAME,
        mustNotUse: [TOP_SEED],
        because: [TOP_SEED],
      });

      const joining = describeKindChange(defaultRow, { kind: "byw", fill: "byw" }, DEFAULT_CTX);
      expect(joining.rename).toBeNull();
      expect(joining.renameInSettings).toEqual({ required: false, proposed: BYW_NAME, mustNotUse: [], because: [] });

      expect(describeKindChange(defaultRow, { kind: "again", fill: "again" }, DEFAULT_CTX).renameInSettings).toBeNull();
    });

    it("never points an ordinary row to Settings", () => {
      expect(describeKindChange(FIXTURES.bywNamed, { kind: "picked", fill: "picked" }, CTX).renameInSettings).toBeNull();
    });
  });

  describe("the title", () => {
    it("names the fill on screen when Seasonal is entered from a switched kind", () => {
      const loaded = row();
      const again = applyRowKind(loaded, { kind: "again", fill: "again" }, CTX);
      const change = describeKindChange(again, { kind: "seasonal", fill: "again" }, CTX, {
        baseline: kindBaseline(loaded),
        saved: loaded,
      });
      expect(change.title).toBe("Change this row to Seasonal (Watch it again)?");
      expect(change.lines).toEqual(["Follows the calendar with all 3 seasons picked. Narrow them down under Which seasons."]);
    });

    it("names the kind, and the fill for Seasonal", () => {
      expect(describeKindChange(FIXTURES.picked, { kind: "again", fill: "again" }, CTX).title).toBe(
        "Change this row to Watch it again?",
      );
      expect(describeKindChange(FIXTURES.picked, { kind: "seasonal", fill: "picked" }, CTX).title).toBe(
        "Change this row to Seasonal (Picked for You)?",
      );
    });
  });

  describe("the lines", () => {
    it("describes row 2 becoming Picked for You", () => {
      const row2 = row({ ...named(BYW_NAME), max_seeds: 3, seed_window: 1, refresh_days: 1 });
      // Its 3 watches are the owner's own and stay; only the settings on screen change.
      expect(describeKindChange(row2, { kind: "picked", fill: "picked" }, CTX).lines).toEqual([
        "Adds a setting this kind uses: How often it changes.",
        "Hides settings this kind doesn't use: Take turns between their last watches and Name for someone who's new. What they're set to is kept, so switching back restores it.",
      ]);
    });

    it("describes a Picked for You row becoming Popular on this server", () => {
      const from = row({ request_tag: "family" });
      expect(describeKindChange(from, { kind: "popular", fill: "popular" }, CTX).lines).toEqual([
        "Makes it one shared row, the same for everyone who gets it.",
        "Clears the request tag “family”.",
        "A shared row never asks for missing titles.",
        "Adds a setting this kind uses: How many people must have watched a title.",
        "Hides settings this kind doesn't use: How many recent watches to match, When someone hasn't watched enough, Sources, Already-watched titles, Only series they haven't started, Recent releases, How often it changes and Hold when they aren't watching. What they're set to is kept, so switching back restores it.",
      ]);
    });

    it("describes a switch to Because you watched without claiming a nightly rebuild", () => {
      // Only a {top_seed} name or rotation makes the engine rebuild nightly, and the optional rename
      // is the owner's choice — so the dialog can't promise it.
      expect(describeKindChange(row({ media: "movie" }), { kind: "byw", fill: "byw" }, CTX).lines).toEqual([
        "Based on their latest film.",
        "Adds settings this kind uses: Take turns between their last watches and Name for someone who's new.",
      ]);
    });

    it("says when a row stops leading with rewatches", () => {
      const lines = describeKindChange(FIXTURES.again, { kind: "byw", fill: "byw" }, CTX).lines;
      expect(lines.slice(0, 2)).toEqual([
        "Stops leading the row with titles they've already finished.",
        "Based on their latest film and their latest show.",
      ]);
    });

    it("says nothing for the kind the row already is", () => {
      const defaultRow = row({ name_template: "", name: "Picked for You", refresh_days: 8 });
      const ctx = { ...DEFAULT_CTX, defaultRowName: BYW_NAME };
      expect(describeKindChange(defaultRow, { kind: "byw", fill: "byw" }, ctx).lines).toEqual([]);
    });

    it("describes a switch to Watch it again with every field it changes", () => {
      const from = row({ watched_pct: 0, unstarted_only: true, seed_window: 3, media: "show" });
      // Resetting the rotation also brings back the cadence and hold, which rotation had overridden.
      expect(describeKindChange(from, { kind: "again", fill: "again" }, CTX).lines).toEqual([
        "Leads the row with favourites they've already finished, then fills it with new picks.",
        "Stops taking turns between their last 3 watches.",
        "Turns off Only series they haven't started, which can't be combined with Watch it again.",
        "Adds settings this kind uses: Skip titles finished recently, How often it changes and Hold when they aren't watching.",
        // Its 0% cap is kept, not raised, so it is hidden like any other setting the kind doesn't use.
        "Hides a setting this kind doesn't use: Already-watched titles. What it's set to is kept, so switching back restores it.",
      ]);
    });

    it("describes following and leaving the calendar", () => {
      expect(describeKindChange(FIXTURES.picked, { kind: "seasonal", fill: "picked" }, CTX).lines).toEqual([
        "Follows the calendar with all 3 seasons picked. Narrow them down under Which seasons.",
      ]);
      expect(describeKindChange(seasonal(FIXTURES.picked), { kind: "picked", fill: "picked" }, CTX).lines).toEqual([
        "Stops following the calendar, so the row can show all year.",
      ]);
    });

    it("describes a shared row going back to per person", () => {
      const lines = describeKindChange(FIXTURES.popular, { kind: "picked", fill: "picked" }, CTX).lines;
      expect(lines[0]).toBe("Gives each person their own private row, built from what they watch.");
      expect(lines).toContain(
        "Hides a setting this kind doesn't use: How many people must have watched a title. What it's set to is kept, so switching back restores it.",
      );
    });

    it("gives the reason when Picked for You needs its own watch count", () => {
      const lines = describeKindChange(FIXTURES.again, { kind: "picked", fill: "picked" }, { ...CTX, globalMaxSeeds: 2 })
        .lines;
      expect(lines).toContain(
        "Matches picks to their last 3 watches. The global default (2) would make it a Because you watched row.",
      );
    });

    it("states the inherited count a Because you watched row goes back to", () => {
      expect(describeKindChange(FIXTURES.bywCount, { kind: "picked", fill: "picked" }, CTX).lines[0]).toBe(
        "Matches picks to their last 30 watches (the global default).",
      );
    });

    it("says nothing about a watch count the switch keeps", () => {
      const lines = describeKindChange({ ...FIXTURES.again, max_seeds: 10 }, { kind: "picked", fill: "picked" }, CTX)
        .lines;
      expect(lines.join(" ")).not.toContain("Matches picks");
    });

    it("is empty when nothing changes", () => {
      expect(describeKindChange(FIXTURES.again, { kind: "again", fill: "again" }, CTX).lines).toEqual([]);
    });

    describe("from a baseline, describe what the switch does to the row on screen", () => {
      it("says Take turns goes back when a switch restores the baseline's rotation", () => {
        const loaded = row();
        const onScreen = { ...applyRowKind(loaded, { kind: "byw", fill: "byw" }, CTX), seed_window: 5 };
        const lines = describeKindChange(onScreen, { kind: "picked", fill: "picked" }, CTX, {
          baseline: kindBaseline(loaded),
          saved: loaded,
        }).lines;
        expect(lines).toContain("Stops taking turns between their last 5 watches.");
        expect(lines).toContain("Matches picks to their last 30 watches (the global default).");
        expect(lines).not.toHaveLength(0);
      });

      it("describes values a switch puts back, in both directions", () => {
        const loaded = row({ unstarted_only: true, request_tag: "family", seed_window: 1, max_seeds: 2 });
        const again = applyRowKind(loaded, { kind: "again", fill: "again" }, CTX);
        const shared = applyRowKind(loaded, { kind: "popular", fill: "popular" }, CTX);
        const opts = { baseline: kindBaseline(loaded), saved: loaded };
        expect(describeKindChange(again, { kind: "byw", fill: "byw" }, CTX, opts).lines).toContain(
          "Turns Only series they haven't started back on.",
        );
        expect(describeKindChange(shared, { kind: "byw", fill: "byw" }, CTX, opts).lines).toContain(
          "Sets the request tag back to “family”.",
        );
      });

      it("doesn't promise Only series they haven't started back on a row narrowed to movies meanwhile", () => {
        // The API refuses the flag on a movies-only row (`collections._validate_pairing`), so the switch
        // can't restore it, and the dialog mustn't say it does.
        const loaded = row({ media: "show", unstarted_only: true });
        const narrowed = { ...applyRowKind(loaded, { kind: "again", fill: "again" }, CTX), media: "movie" as const };
        const lines = describeKindChange(narrowed, { kind: "picked", fill: "picked" }, CTX, {
          baseline: kindBaseline(loaded),
          saved: loaded,
        }).lines;
        expect(lines.join(" ")).not.toContain("Only series they haven't started");
      });
    });

    describe("says it picks new titles every night whenever the row it ends up as follows a watch", () => {
      it("names the line in the kind block's own words", () => {
        expect(NIGHTLY_LINE).toBe("Picks new titles every night, so it keeps up with their latest watch.");
      });

      it("when the optional {top_seed} name is taken, and not when it isn't", () => {
        const choice = { kind: "byw", fill: "byw" } as const;
        expect(describeKindChange(FIXTURES.picked, choice, CTX, { renameTo: BYW_NAME }).lines).toContain(NIGHTLY_LINE);
        expect(describeKindChange(FIXTURES.picked, choice, CTX, { renameTo: null }).lines).not.toContain(NIGHTLY_LINE);
        expect(describeKindChange(FIXTURES.picked, choice, CTX).lines).not.toContain(NIGHTLY_LINE);
      });

      it("when a row named after a watch only leaves or joins Seasonal", () => {
        expect(describeKindChange(seasonal(FIXTURES.bywNamed), { kind: "byw", fill: "byw" }, CTX).lines).toEqual([
          "Stops following the calendar, so the row can show all year.",
          NIGHTLY_LINE,
        ]);
        expect(describeKindChange(FIXTURES.bywNamed, { kind: "seasonal", fill: "byw" }, CTX).lines).toContain(
          NIGHTLY_LINE,
        );
      });

      it("when the row keeps taking turns", () => {
        const turns = row({ max_seeds: 2, seed_window: 3 });
        expect(describeKindChange(turns, { kind: "seasonal", fill: "byw" }, CTX).lines).toContain(NIGHTLY_LINE);
      });

      it("not for a shared row, which never follows anyone's watch", () => {
        const lines = describeKindChange(FIXTURES.bywNamed, { kind: "popular", fill: "popular" }, CTX).lines;
        expect(lines).not.toContain(NIGHTLY_LINE);
      });

      it("not when the name it's given stops following one", () => {
        const lines = describeKindChange(FIXTURES.bywNamed, { kind: "picked", fill: "picked" }, CTX).lines;
        expect(lines).not.toContain(NIGHTLY_LINE);
      });
    });
  });

  it("never changes the row it describes", () => {
    const input = structuredClone(FIXTURES.bywNamed);
    for (const target of TARGETS) describeKindChange(input, target, CTX);
    expect(input).toEqual(FIXTURES.bywNamed);
  });
});

describe("renameAt: where a kind switch's new name shows up once saved", () => {
  const saved = row(named("{season} picks"));

  it("uses the rename screen when neither name follows a watch", () => {
    expect(renameAt(saved, saved, "Holiday picks")).toBe("rename_screen");
  });

  it("waits for the next run when either name has {top_seed}, which the rename screen can't render", () => {
    expect(renameAt(row(named(BYW_NAME)), row(), PICKS_NAME)).toBe("next_run");
    expect(renameAt(saved, saved, BYW_NAME)).toBe("next_run");
  });

  it("waits for the row to be switched on and run when it is off", () => {
    expect(renameAt({ ...saved, enabled: false }, saved, "Holiday picks")).toBe("turned_on");
    expect(renameAt({ ...saved, enabled: false }, { ...saved, build: "shared" }, "Holiday picks")).toBe("turned_on");
  });

  it("is used when the row is rebuilt when the build flips", () => {
    expect(renameAt(saved, { ...saved, build: "shared" }, BYW_NAME)).toBe("rebuild");
  });
});

describe("renameProblem", () => {
  it("wants a name, without the placeholders the new kind can't take", () => {
    expect(renameProblem("  ", [TOP_SEED])).toBe("Give the row a name.");
    // Only Picked for You and Popular refuse it, and neither follows a watch: Picked for You would
    // read back as Because you watched, and a shared row has no watch to fill it with.
    expect(renameProblem("Picks {top_seed}", [TOP_SEED])).toBe(
      "A name with {top_seed} names the watch a row follows, and this kind doesn't follow one.",
    );
    expect(renameProblem("{season_emoji} picks", [SEASON, SEASON_EMOJI])).toBe(
      "A name with {season_emoji} only works on a seasonal row.",
    );
    expect(renameProblem("Fine", [TOP_SEED, SEASON])).toBeNull();
  });

  it("keeps whether the name follows a watch as the switch confirmed it, once one is given", () => {
    // The editor reads the row's settings from the confirmed name, so a name typed after it that
    // gains or loses {top_seed} would save a row that rebuilds on a different cadence than shown.
    expect(renameProblem("Hidden Gems again", [], BYW_NAME)).toBe(
      "Keep {top_seed} in the name: the change you confirmed names the row after their latest watch.",
    );
    expect(renameProblem("Gems from {top_seed}", [], "Halloween hits")).toBe(
      "The change you confirmed doesn't name the row after a watch, so the name can't use {top_seed}.",
    );
    expect(renameProblem("Gems from {top_seed}", [], BYW_NAME)).toBeNull();
    expect(renameProblem("Halloween forever", [], "Halloween hits")).toBeNull();
    // A placeholder the kind refuses outright is the reason given, not the confirmed name's.
    expect(renameProblem("Picks {top_seed}", [TOP_SEED], PICKS_NAME)).toBe(
      "A name with {top_seed} names the watch a row follows, and this kind doesn't follow one.",
    );
  });
});

describe("Watch it again's new picks can take turns", () => {
  const again = row({ ...named(AGAIN_NAME), rewatch: true, watched_pct: 1 });

  it("shows Take turns while the fill-up matches 1 or 2 watches, as the engine reads it then", () => {
    expect(visibleSettings({ ...again, max_seeds: 2 }, CTX).has("seed_window")).toBe(true);
    expect(visibleSettings({ ...again, max_seeds: 1 }, CTX).has("seed_window")).toBe(true);
    expect(visibleSettings({ ...again, max_seeds: null }, { ...CTX, globalMaxSeeds: 2 }).has("seed_window")).toBe(true);
    expect(visibleSettings({ ...again, max_seeds: 3 }, CTX).has("seed_window")).toBe(false);
    expect(visibleSettings(again, CTX).has("seed_window")).toBe(false);
  });

  it("leaves a rotation it shows out of hiddenButRead, and flags one it doesn't", () => {
    expect(hiddenButRead({ ...again, max_seeds: 2, seed_window: 3 }, CTX)).toEqual([]);
    expect(hiddenButRead({ ...again, max_seeds: 5, seed_window: 3 }, CTX)).toEqual(["seed_window"]);
  });

  it("shows exactly the Watch it again settings plus Take turns, which forces nightly", () => {
    const turns = { ...again, max_seeds: 2, seed_window: 3 };
    expect(visible(turns)).toEqual(
      [...expected("again", "both").filter((key) => key !== "refresh_days" && key !== "idle_hold_days"), "seed_window"].sort(),
    );
  });
});
