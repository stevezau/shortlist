import { SEASON_TOKENS, TOP_SEED, usesSeason } from "@/lib/placeholders";
import {
  FILL_META,
  KIND_META,
  type RowFill,
  type RowKind,
  type RowKindChoice,
} from "@/lib/row-kind-meta";
import { findRowTemplate } from "@/lib/row-templates";
import type { CollectionInput, RowSources } from "@/lib/types";

/**
 * Row kinds: what a row IS, worked out from the fields it already has (design:
 * `.claude/docs/plans/row-editor-cleanup-design.md` §3–§5, §11).
 *
 * No kind is stored. Everything that decides which settings the editor shows, what a kind switch
 * changes, and what the confirm dialog says reads this one module, so the three cannot disagree.
 * Only `applyRowKind` changes a field; every other function here is a read.
 */

export type { KindMeta, RowFill, RowKind, RowKindChoice } from "@/lib/row-kind-meta";
export { FILL_META, KIND_GROUP, KIND_META, ROW_FILLS, ROW_KINDS, SEASONAL_FILLS } from "@/lib/row-kind-meta";

/**
 * The server-wide values a row's kind depends on. Each function takes only the part it reads.
 */
export interface RowKindContext {
  /** The row is the default row (`slug == "picked"`). */
  isDefault: boolean;
  /** `recommendations.max_seeds`: what a row with `max_seeds: null` is built from. */
  globalMaxSeeds: number;
  /** `row.name_template`: the default row's name, which lives in Settings rather than on the row. */
  defaultRowName: string;
  /** `candidates.sources`: what a row with no sources of its own gathers from. */
  globalSources: readonly string[];
  /** Every season slug, in catalogue order: the built-ins and the owner's own (#137). */
  seasonCatalogue: readonly string[];
  /** The built-in seasons' slugs — what a row turned Seasonal starts with. A season the owner made is
   *  ticked row by row, never by default (#137). */
  builtinSeasons: readonly string[];
  /** Settings › Danger zone's `paused_all`: no run or Plex pass happens until it is lifted. */
  pausedAll?: boolean;
}

type KindGlobals = Pick<RowKindContext, "isDefault" | "globalMaxSeeds" | "defaultRowName">;

/** The tightest seed budget a row named after ONE title can actually use.
 *
 *  1 for a single-media row. 2 for a movies-and-TV row, because seeds are balanced across the media
 *  types present and a budget of 1 therefore yields one type only — a `both` row at 1 gathers no
 *  candidates for its other half, so that library's collection never builds. */
export function namedRowSeeds(media: string): number {
  return media === "both" ? 2 : 1;
}

/**
 * The name template the engine renders for this row (`delivery.raw_row_template`): the row's own,
 * else — for the default row only — the global `row.name_template`. A new row has no template yet,
 * so its typed name stands in.
 */
export function effectiveRowName(
  input: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "defaultRowName">,
): string {
  return input.name_template || (ctx.isDefault ? ctx.defaultRowName : input.name);
}

/** Whether the row's title claims one particular watch. Mirrors the engine's `_names_a_seed`. */
export function namesASeed(
  input: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "defaultRowName">,
): boolean {
  return effectiveRowName(input, ctx).includes(TOP_SEED);
}

export function effectiveMaxSeeds(input: CollectionInput, ctx: Pick<RowKindContext, "globalMaxSeeds">): number {
  return input.max_seeds ?? ctx.globalMaxSeeds;
}

/**
 * Whether the engine forces this row to rebuild nightly (`effective_refresh_days`): its name follows
 * a watch, or it takes turns between several. Its own cadence is ignored then.
 */
export function followsAWatch(
  input: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "defaultRowName">,
): boolean {
  return namesASeed(input, ctx) || input.seed_window > 1;
}

/** Whether "Take turns between their last N watches" can be used: only on a row built from 1 or 2. */
export function takeTurnsEnabled(input: CollectionInput, ctx: Pick<RowKindContext, "globalMaxSeeds">): boolean {
  const seeds = effectiveMaxSeeds(input, ctx);
  return seeds >= 1 && seeds <= 2;
}

function fillOf(input: CollectionInput, ctx: KindGlobals): RowFill {
  // First: a requests row is per person by validation, so the flag alone decides.
  if (input.requests_row) return "requests";
  if (input.build === "shared") return "popular";
  if (input.rewatch) return "again";
  // The name before the count: a {top_seed} row blending 3 watches (row 2) is still "Because you
  // watched" to its owner, and the name is what viewers see.
  if (namesASeed(input, ctx) || takeTurnsEnabled(input, ctx)) return "byw";
  return "picked";
}

/** The row's kind, and for a seasonal row how it's filled (design §3.1). Reads, never writes. */
export function rowKindOf(input: CollectionInput, ctx: KindGlobals): RowKindChoice {
  const fill = fillOf(input, ctx);
  return { kind: input.seasons.length > 0 ? "seasonal" : fill, fill };
}

/** Why a default row named after a watch in Settings can't leave Because you watched from the editor. */
export const SEED_NAME_IN_SETTINGS =
  "This row's name (set in Settings) follows one watch. Change it in Settings first.";

/** Where the default row's name is changed: Settings › Row defaults. */
export const DEFAULT_ROW_NAME_SETTINGS = "/settings#defaults";

/** Where the Overseerr, Radarr and Sonarr connections are made: Settings › Connections. */
export const CONNECTIONS_SETTINGS = "/settings#connections";

/** The one sentence, in the gallery and the editor, for a server with no request source at all. */
export const NO_REQUEST_SOURCE =
  "Needs a way to know who asked for what: an Overseerr or Jellyseerr connection, or Radarr/Sonarr with request tags.";

/**
 * Whether nothing on the server can say who asked for what, from the setup check's own states —
 * never from the wording of its `problems`, which are for people to read.
 */
export function noRequestSource(sources: Pick<RowSources, "overseerr" | "radarr" | "sonarr">): boolean {
  return sources.overseerr === "off" && sources.radarr === "off" && sources.sonarr === "off";
}

/** Why a kind can't be picked right now, in the picker's words; null when it can. */
export function kindDisabledReason(
  kind: RowKind,
  input: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "seasonCatalogue" | "defaultRowName">,
): string | null {
  if (kind === "seasonal") {
    // The server refuses seasons on the default row (`collections.py`); this only makes that visible.
    if (ctx.isDefault) return "The default row can't be seasonal";
    if (ctx.seasonCatalogue.length === 0) return "The list of seasons hasn't loaded yet.";
    return null;
  }
  // The default row's name lives in Settings, so a switch made here can't take a {top_seed} out of
  // it: only the kinds that can carry one are left (`renameFor`). Picked for You would read straight
  // back as Because you watched, and a shared row or a requests row has no watch to fill it with;
  // Watch it again is named after a watch by the engine too (`rows._names_a_seed`).
  if (ctx.isDefault && REFUSES_SEED.has(kind) && namesASeed(input, ctx)) {
    return SEED_NAME_IN_SETTINGS;
  }
  return null;
}

/** A global watch count that would make every inheriting row read as Because you watched. */
export function isNarrowGlobal(ctx: Pick<RowKindContext, "globalMaxSeeds">): boolean {
  return ctx.globalMaxSeeds === 1 || ctx.globalMaxSeeds === 2;
}

/** The fills whose rows have no watch to fill a {top_seed} name with (`renameFor`). */
const REFUSES_SEED: ReadonlySet<RowKind> = new Set(["picked", "popular", "requests"]);

function targetFill(choice: RowKindChoice): RowFill {
  return choice.kind === "seasonal" ? choice.fill : choice.kind;
}

/**
 * Every field a kind switch can write: the fill patches below, and the seasons. Nothing else about a row
 * changes when its kind does, which is what lets a switch be undone by restoring just these.
 */
export const KIND_FIELDS = [
  "build",
  "rewatch",
  "requests_row",
  "max_seeds",
  "seed_window",
  "unstarted_only",
  "request_tag",
  "seasons",
] as const satisfies readonly (keyof CollectionInput)[];

export type KindField = (typeof KIND_FIELDS)[number];

/** The fields a switch starts from: the kind fields, and the name a switch may have replaced. */
export const BASELINE_FIELDS = [...KIND_FIELDS, "name", "name_template"] as const;

export type KindBaseline = Pick<CollectionInput, (typeof BASELINE_FIELDS)[number]>;

/** The row's baseline as loaded (a saved row) or prefilled (a new row, from its template). */
export function kindBaseline(input: CollectionInput): KindBaseline {
  return Object.fromEntries(BASELINE_FIELDS.map((field) => [field, input[field]])) as KindBaseline;
}

/**
 * What a switch to `choice` is applied to (`applyRowKind`), given the row on screen and its baseline.
 *
 * The kind fields come back from the baseline, so a switch is reversible (back to the baseline's kind
 * gives back its values) and non-cumulative (a switch doesn't build on the one before). Two
 * exceptions keep what the owner is looking at: entering Seasonal on the fill already on screen only
 * adds seasons (the baseline's, if it had any, else every season), and staying seasonal keeps the
 * seasons on screen however they were narrowed. The name is always the baseline's, since the pending
 * rename a switch asked for is worked out again for every switch.
 */
export function kindSwitchBase(
  onScreen: CollectionInput,
  baseline: Partial<CollectionInput>,
  choice: RowKindChoice,
  ctx: KindGlobals,
): CollectionInput {
  const now = rowKindOf(onScreen, ctx);
  const toSeasonal = choice.kind === "seasonal";
  if (toSeasonal && now.kind !== "seasonal" && choice.fill === now.fill) {
    return {
      ...onScreen,
      name: baseline.name ?? onScreen.name,
      name_template: baseline.name_template ?? onScreen.name_template,
      seasons: baseline.seasons ?? [],
    };
  }
  return {
    ...onScreen,
    ...baseline,
    ...(toSeasonal && now.kind === "seasonal" ? { seasons: onScreen.seasons } : {}),
  };
}

/**
 * Whether the owner's hand edit of `field` becomes part of the baseline: only when the baseline's
 * kind shows it through the same setting the owner just used. An edit that only the switched-to kind
 * offers (Take turns after Picked for You → Because you watched) belongs to that switch, and switching
 * back undoes it.
 */
export function baselineTakes(
  field: keyof CollectionInput,
  onScreen: CollectionInput,
  baselineRow: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "globalMaxSeeds" | "defaultRowName" | "globalSources">,
): boolean {
  const showing = (row: CollectionInput) => {
    const keys = new Set<RowSettingKey>([...visibleSettings(row, ctx), ...hiddenButRead(row, ctx)]);
    return settingsFor(field).filter((key) => keys.has(key));
  };
  const now = showing(onScreen);
  return showing(baselineRow).some((key) => now.includes(key));
}

/** What each fill writes when a row switches to it (design §4). Only ever `KIND_FIELDS`. */
function fillPatch(input: CollectionInput, fill: RowFill, ctx: RowKindContext): Partial<KindBaseline> {
  switch (fill) {
    case "picked":
      return {
        build: "per_person",
        rewatch: false,
        requests_row: false,
        seed_window: 1,
        // Only a count of 1 or 2 reads back as Because you watched, so an owner's 3 or more stays.
        // Inheriting instead is only safe when the global itself is 3 or more.
        ...(takeTurnsEnabled(input, ctx) ? { max_seeds: isNarrowGlobal(ctx) ? 3 : null } : {}),
      };
    case "byw":
      return { build: "per_person", rewatch: false, requests_row: false, max_seeds: namedRowSeeds(input.media) };
    case "again":
      // The API refuses rewatch with unstarted_only. The already-watched cap is left alone: the kind
      // hides it and the engine ignores it on a rewatch row, so it only matters once the row leaves.
      return { build: "per_person", rewatch: true, requests_row: false, unstarted_only: false, seed_window: 1 };
    case "requests":
      // The API refuses a requests row that is shared, a rewatch row, or seasonal (`_validate_requests_row`).
      // Its titles are what they asked for, so there is no series filter and no watch to take turns on.
      return { build: "per_person", rewatch: false, requests_row: true, unstarted_only: false, seed_window: 1 };
    case "popular":
      // Shared rows never request, so a request tag on one is inert.
      return { build: "shared", requests_row: false, request_tag: "" };
  }
}

/**
 * The row switched to another kind (design §4). Returns a new object; the input is never changed.
 *
 * A fill the row already has writes nothing, so switching only in or out of Seasonal never touches
 * the fill's settings. Fields the new kind hides but the engine ignores are left as they are, so
 * switching back restores them.
 *
 * @throws Error when the kind can't be picked (see `kindDisabledReason`) — the picker disables it.
 */
export function applyRowKind(input: CollectionInput, choice: RowKindChoice, ctx: RowKindContext): CollectionInput {
  const current = rowKindOf(input, ctx);
  const fill = targetFill(choice);
  const toSeasonal = choice.kind === "seasonal";
  const wasSeasonal = current.kind === "seasonal";

  if (toSeasonal && !wasSeasonal) {
    const reason = kindDisabledReason("seasonal", input, ctx);
    if (reason) throw new Error(`Can't make this row seasonal: ${reason}`);
  }

  return {
    ...input,
    ...(fill !== current.fill ? fillPatch(input, fill, ctx) : {}),
    ...(toSeasonal && !wasSeasonal ? { seasons: [...ctx.builtinSeasons] } : {}),
    ...(!toSeasonal && wasSeasonal ? { seasons: [] } : {}),
  };
}

/**
 * The switched row held to the API's pairing rules (`collections._validate_pairing`): Only series they
 * haven't started is refused with rewatch and on a movies-only row. A switch can bring the flag back
 * from the baseline after the libraries narrowed to movies under a kind that doesn't show it, so
 * every switch's result goes through here. Returns the input itself when nothing is refused.
 */
export function normalizeKindResult(input: CollectionInput): CollectionInput {
  const refused = input.unstarted_only && (input.rewatch || input.media === "movie");
  return refused ? { ...input, unstarted_only: false } : input;
}

/** Every per-row setting the editor can show, in the editor's order. */
export const ROW_SETTING_KEYS = [
  "kind",
  "name",
  "description",
  "poster",
  "audience",
  "min_watchers",
  "seasons",
  "requests_window_days",
  "requests_sources",
  "requests_tag_pattern",
  "libraries",
  "size",
  "pick_order",
  "rated_by",
  "max_seeds",
  "based_on",
  "seed_window",
  "cold_start",
  // Directly under cold start: it is only used for someone with nothing watched (design §5).
  "fallback_name",
  "rewatch_cooldown_days",
  "candidate_sources",
  "recent_count",
  "ai_instructions",
  "watched_pct",
  "unstarted_only",
  "recency",
  "limits",
  "schedule",
  "refresh_days",
  "idle_hold_days",
  "placement",
  "hub_anchor",
  "show_days",
  "sort_title_prefix",
  "requests",
  "enabled",
] as const;

export type RowSettingKey = (typeof ROW_SETTING_KEYS)[number];

/** Short names for the dialog's lists. */
export const SETTING_LABELS: Readonly<Record<RowSettingKey, string>> = {
  kind: "What kind of row is this?",
  name: "Name",
  fallback_name: "Name for someone who's new",
  description: "Description",
  poster: "Poster",
  audience: "Who gets it",
  min_watchers: "How many people must have watched a title",
  seasons: "Seasons",
  requests_window_days: "Show titles that landed in the last",
  requests_sources: "Where requests are read from",
  requests_tag_pattern: "Use my own tags",
  libraries: "Libraries",
  size: "Row size",
  pick_order: "Pick order",
  rated_by: "Rated by",
  max_seeds: "How many recent watches to match",
  based_on: "Based on",
  seed_window: "Take turns between their last watches",
  cold_start: "When someone hasn't watched enough",
  rewatch_cooldown_days: "Skip titles finished recently",
  candidate_sources: "Sources",
  recent_count: "Recent watches for AI web search",
  ai_instructions: "AI instructions",
  watched_pct: "Already-watched titles",
  unstarted_only: "Only series they haven't started",
  recency: "Recent releases",
  limits: "Length, year and rating limits",
  schedule: "Runs on",
  refresh_days: "Titles refresh every",
  idle_hold_days: "Hold when they aren't watching",
  placement: "Where it shows",
  hub_anchor: "Position in the Recommended shelf",
  show_days: "Show this row",
  sort_title_prefix: "Sort prefix",
  requests: "Requests",
  enabled: "On or off",
};

/**
 * The settings with no line in "What this row will do" (design §8), each with why. Every other
 * setting the editor shows has exactly one line there.
 */
export const NO_FACT_LINE: Readonly<Partial<Record<RowSettingKey, string>>> = {
  name: "The Plex card beside the name field shows it, filled in for a sample person.",
  description: "The Plex card shows it under the name, filled in the same way.",
  poster: "The Plex card shows it: the uploaded image, the text poster's words, or Plex's own artwork.",
};

/**
 * Which setting controls each field of the editor's input; null for the few no editor control
 * writes. Typed over every key, so a new field doesn't compile until it is placed here.
 */
export const FIELD_SETTING: { readonly [K in keyof CollectionInput]-?: RowSettingKey | null } = {
  name: "name",
  name_template: "name",
  defer_rename: null, // only the rename screen defers
  build: "kind",
  rewatch: "kind",
  audience: "audience",
  audience_user_ids: "audience",
  enabled: "enabled",
  schedule: "schedule",
  size: "size",
  media: "libraries",
  library_keys: "libraries",
  sort_order: null, // set on the Rows page
  fallback_name: "fallback_name",
  description: "description",
  poster: "poster",
  sort_title_prefix: "sort_title_prefix",
  min_watchers: "min_watchers",
  candidate_sources: "candidate_sources",
  watched_pct: "watched_pct",
  rewatch_cooldown_days: "rewatch_cooldown_days",
  requests_row: "kind",
  requests_window_days: "requests_window_days",
  requests_tag_pattern: "requests_tag_pattern",
  unstarted_only: "unstarted_only",
  refresh_days: "refresh_days",
  idle_hold_days: "idle_hold_days",
  recency: "recency",
  recent_count: "recent_count",
  ai_instructions: "ai_instructions",
  max_seeds: "max_seeds",
  max_runtime: "limits",
  min_year: "limits",
  max_year: "limits",
  min_rating: "limits",
  cold_start: "cold_start",
  seed_window: "seed_window",
  pick_order: "pick_order",
  placement: "placement",
  placement_friends: "placement",
  show_days: "show_days",
  seasons: "seasons",
  season_lead_days: "seasons",
  season_after_days: "seasons",
  hub_anchor: "hub_anchor",
  pin_top: "hub_anchor", // legacy pin, consumed by the shelf-position control
  request_tag: "requests",
  req_min_rating: "requests",
  req_min_demand: "requests",
  req_min_year: "requests",
  req_max_year: "requests",
  req_auto_send: "requests",
  req_max_per_row: "requests",
  req_radarr_quality_profile_id: "requests",
  req_radarr_root_folder: "requests",
  req_sonarr_quality_profile_id: "requests",
  req_sonarr_root_folder: "requests",
  req_sonarr_monitor: "requests",
  req_language_mode: "requests",
  req_preferred_languages: "requests",
  req_auto_user_tag: "requests",
  // No control today; they follow Settings (design §7).
  req_min_votes: null,
  req_auto_min_demand: null,
  req_auto_min_rating: null,
  req_min_rating_other: null,
};

/** The settings that show a field: its own, and Based on for the watch count. */
function settingsFor(field: keyof CollectionInput): RowSettingKey[] {
  const own = FIELD_SETTING[field];
  return [...(own === null ? [] : [own]), ...(field === "max_seeds" ? (["based_on"] as const) : [])];
}

/** The fields a setting shows. "Based on" is `max_seeds` offered as choices on Because you watched. */
function fieldsOf(key: RowSettingKey): (keyof CollectionInput)[] {
  if (key === "based_on") return ["max_seeds"];
  return (Object.keys(FIELD_SETTING) as (keyof CollectionInput)[]).filter((field) => FIELD_SETTING[field] === key);
}

const ALWAYS_VISIBLE: readonly RowSettingKey[] = [
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

/** Each fill's own settings (design §5), before the conditions `visibleSettings` applies. */
const FILL_SETTINGS: Readonly<Record<RowFill, readonly RowSettingKey[]>> = {
  picked: [
    "max_seeds",
    "cold_start",
    "candidate_sources",
    "recent_count",
    "ai_instructions",
    "watched_pct",
    "unstarted_only",
    "recency",
    "limits",
    "refresh_days",
    "idle_hold_days",
    "requests",
  ],
  byw: [
    "based_on",
    "seed_window",
    "cold_start",
    "fallback_name",
    "candidate_sources",
    "recent_count",
    "ai_instructions",
    "watched_pct",
    "unstarted_only",
    "recency",
    "limits",
    "refresh_days",
    "idle_hold_days",
    "requests",
  ],
  // The fill-up for when finished titles run out still reads the watch count, sources and recency.
  // Already-watched is overridden by the engine, and the API refuses unstarted_only with rewatch.
  again: [
    "rewatch_cooldown_days",
    "cold_start",
    "max_seeds",
    "candidate_sources",
    "recent_count",
    "ai_instructions",
    "recency",
    "limits",
    "refresh_days",
    "idle_hold_days",
    "requests",
  ],
  // Built from the request ledger, not from watches: no seeds, sources, cap or cadence apply.
  requests: ["requests_window_days", "requests_sources", "requests_tag_pattern"],
  popular: ["min_watchers"],
};

/** The sources the engine gathers from for this row (`rows.effective_row_sources`). */
function rowSources(input: CollectionInput, ctx: Pick<RowKindContext, "globalSources">): readonly string[] {
  const sources = input.candidate_sources.length > 0 ? input.candidate_sources : ctx.globalSources;
  // Web search asks about one watched title at a time, which is not seasonal, so a seasonal row drops it.
  return input.seasons.length > 0 ? sources.filter((source) => source !== "llm_web") : sources;
}

/**
 * The settings the editor shows for this row as it stands (design §5). Reads, never writes.
 *
 * Beyond the kind's list, a setting the engine would ignore on this row is left out: a control
 * the engine ignores promises a behaviour.
 */
export function visibleSettings(
  input: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "globalMaxSeeds" | "defaultRowName" | "globalSources">,
): ReadonlySet<RowSettingKey> {
  const { kind, fill } = rowKindOf(input, ctx);
  const shown = new Set<RowSettingKey>([...ALWAYS_VISIBLE, ...FILL_SETTINGS[fill]]);

  // The default row's size is the global `row.size`.
  if (ctx.isDefault) shown.delete("size");
  if (kind === "seasonal") shown.add("seasons");
  // A requests row is newest arrival first, always: the engine never reads its pick order.
  if (fill === "requests") shown.delete("pick_order");
  if (input.pick_order === "rating" && shown.has("pick_order")) shown.add("rated_by");
  // The engine rotates the fill-up's seed list like any other (`history.py`), so Watch it again
  // offers rotation while its new picks match 1 or 2 watches.
  if (fill === "again" && takeTurnsEnabled(input, ctx)) shown.add("seed_window");
  // The engine renders the fallback whenever a {top_seed} name has no watch to fill it, whatever the kind.
  if (namesASeed(input, ctx)) shown.add("fallback_name");
  if (!rowSources(input, ctx).includes("llm_web")) shown.delete("recent_count");
  if (!rowSources(input, ctx).includes("llm_web")) shown.delete("ai_instructions");
  // The API refuses it on a movies-only row.
  if (input.media === "movie") shown.delete("unstarted_only");
  // `effective_refresh_days` forces nightly and `effective_idle_hold_days` forces no hold here.
  if (followsAWatch(input, ctx)) shown.delete("refresh_days");
  if (input.seed_window > 1) shown.delete("idle_hold_days");

  return shown;
}

/**
 * The row as it should be saved: blank AI instructions on a row that hides that field become the default,
 * since the server refuses a mode with no words and the owner has no field to write them in. Text the owner
 * did write is kept, so choosing web search again finds it.
 */
export function withoutHiddenInstructions(
  input: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "globalMaxSeeds" | "defaultRowName" | "globalSources">,
): CollectionInput {
  const { mode, text } = input.ai_instructions;
  if (mode === "default" || text.trim() !== "" || visibleSettings(input, ctx).has("ai_instructions")) return input;
  return { ...input, ai_instructions: { mode: "default", text: "" } };
}

/**
 * Settings the editor can't use as things stand but the engine still applies, holding a value other
 * than their default — shown with a Reset until they are back at it (design §4). Reads, never writes.
 *
 * Today that is only rotation: the engine rotates any per-person row's seed list. It counts when the
 * kind hides it, and when Because you watched shows it disabled on a blend of 3 or more watches.
 * A shared row is built by a separate builder that never reads it.
 */
export function hiddenButRead(
  input: CollectionInput,
  ctx: Pick<RowKindContext, "isDefault" | "globalMaxSeeds" | "defaultRowName" | "globalSources">,
): RowSettingKey[] {
  // A requests row is built from the ledger, so it has no seed list to rotate either.
  if (input.build === "shared" || input.requests_row || input.seed_window <= 1) return [];
  const usable = visibleSettings(input, ctx).has("seed_window") && takeTurnsEnabled(input, ctx);
  return usable ? [] : ["seed_window"];
}

export interface KindChangeRename {
  /** True when the row reads back as the wrong kind, or can't be named, under its old name. */
  required: boolean;
  /** The name to prefill: the new kind's template name. */
  proposed: string;
  /** The placeholders the new name can't carry: the ones that made the old name impossible, and
   *  {top_seed} on the way to Picked for You or Popular on this server (`renameFor`). */
  mustNotUse: string[];
  /** The placeholders that made the old name impossible: the dialog's reason for a required rename. */
  because: string[];
}

export interface KindChangeOptions {
  /** What every switch starts from (`kindSwitchBase`); none: the switch is applied to the row as given. */
  baseline?: Partial<CollectionInput>;
  /** The row as saved, for what saving does on Plex; the row the switch starts from when omitted. */
  saved?: CollectionInput;
  /** The name the owner chose in the dialog, or null for none. Omitted or null: a required rename
   *  takes its proposed name and an optional one isn't made. */
  renameTo?: string | null;
}

export interface KindChange {
  /** "Change this row to X?" */
  title: string;
  /** "This will:" — one plain sentence per consequence. */
  lines: string[];
  /** What saving does on Plex (design §4.1, §11). */
  plexNote: string;
  /** Where the new name reaches Plex once saved (`renameAtNote`); null when there's no new name. */
  renameNote: string | null;
  /** A rename to offer on the rename screen after saving; null when there's none to offer. */
  rename: KindChangeRename | null;
  /** The default row's version of `rename`: its name lives in Settings, so the dialog links there. */
  renameInSettings: KindChangeRename | null;
}

const PLEX_NOTE_TO_SHARED =
  "Saving removes everyone's own copy of this row from Plex right away. One shared row, the same for everyone who gets it, is built the next time the row runs, if enough people have watched titles in common. Until then, nobody has this row.";
const PLEX_NOTE_TO_PER_PERSON =
  "Saving removes the shared row from Plex right away. Each person gets their own private row the next time the row runs. Until then, nobody has this row.";
const PLEX_NOTE_SEASONS =
  "Saving applies the season dates on Plex straight away. If today is outside them, the row is hidden now.";
// The seasons change queues `rows.visibility` for this row at save, and with no seasons the row is no
// longer dormant, so promotion puts the collections it kept between seasons back on its shelves.
const PLEX_NOTE_LEAVING_SEASONS =
  "If it's hidden between seasons right now, saving puts it back on Plex straight away, still holding the titles from its last season.";
const PLEX_NOTE_NEXT_RUN = "Nothing changes on Plex until you save and the row next runs.";
// An off row has no collections (switching it off removed them) and doesn't run. Saving can still
// queue work — a build flip queues a share-filter pass (`plan_row_changes`) — so this says where the
// row is, not that nothing happens.
const PLEX_NOTE_OFF = "This row is switched off, so it isn't on Plex. These settings apply when you turn it back on.";
const PAUSED_NOTE = "(Runs are paused in Settings, so this waits until they're resumed.)";

function waitsOnARun(note: string, ctx: Pick<RowKindContext, "pausedAll">): string {
  return ctx.pausedAll ? `${note} ${PAUSED_NOTE}` : note;
}

/**
 * Where a kind switch's new name reaches Plex once saved (design §4.2):
 * - `rebuild`: the build flips, deleting the collections at save; the row is rebuilt under it.
 * - `turned_on`: the row is off, with nothing on Plex; it's named so when it next runs.
 * - `next_run`: either name has `{top_seed}`. A new one has no title until a run picks the seed, and the
 *   row's next delivery retitles the collection by its ledger key either way.
 * - `rename_screen`: the rename screen renames the collections from the old title.
 */
export type RenameAt = "rebuild" | "turned_on" | "next_run" | "rename_screen";

export function renameAt(saved: CollectionInput, after: Pick<CollectionInput, "build">, newName: string): RenameAt {
  if (!saved.enabled) return "turned_on";
  if (saved.build !== after.build) return "rebuild";
  const oldName = saved.name_template || saved.name;
  if (oldName.includes(TOP_SEED) || newName.includes(TOP_SEED)) return "next_run";
  return "rename_screen";
}

const RENAME_AT_NOTE: Readonly<Record<RenameAt, string>> = {
  rebuild: "The new name is used when the row is rebuilt.",
  turned_on: "The new name appears on Plex when you turn it back on and it runs.",
  next_run: "The new name appears on Plex the next time the row runs.",
  rename_screen:
    "Saving opens the rename screen with this name, which renames the row on Plex for everyone who has it.",
};

/** What happens to a kind switch's new name, in the dialog's and the Name box's words. */
export function renameAtNote(at: RenameAt, ctx: Pick<RowKindContext, "pausedAll">): string {
  return at === "rename_screen" ? RENAME_AT_NOTE[at] : waitsOnARun(RENAME_AT_NOTE[at], ctx);
}

/** Why a new name can't carry a placeholder the new kind can't take (`renameFor`). */
function refusal(token: string): string {
  return token === TOP_SEED
    ? `A name with ${TOP_SEED} names the watch a row follows, and this kind doesn't follow one.`
    : `A name with ${token} only works on a seasonal row.`;
}

/**
 * What's wrong with a kind switch's new name, or null: the dialog and the Name box ask the same.
 *
 * `confirmed`, in the Name box: the name the switch was confirmed with. The editor reads the row's
 * settings from it, so a name typed since can't gain or lose {top_seed} — that would save a row that
 * rebuilds nightly when the editor says it doesn't, or the other way round.
 */
export function renameProblem(name: string, mustNotUse: readonly string[], confirmed?: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) return "Give the row a name.";
  const kept = mustNotUse.find((token) => trimmed.includes(token));
  if (kept) return refusal(kept);
  if (confirmed === undefined || confirmed.includes(TOP_SEED) === trimmed.includes(TOP_SEED)) return null;
  return confirmed.includes(TOP_SEED)
    ? `Keep ${TOP_SEED} in the name: the change you confirmed names the row after their latest watch.`
    : `The change you confirmed doesn't name the row after a watch, so the name can't use ${TOP_SEED}.`;
}

/** The gallery template each fill's rename proposes. */
const FILL_TEMPLATE_ID: Readonly<Record<RowFill, string>> = {
  picked: "picked-for-you",
  byw: "because-you-watched",
  again: "seen-it-already",
  requests: "your-requests",
  popular: "popular-here",
};

function templateName(fill: RowFill): string {
  const name = findRowTemplate(FILL_TEMPLATE_ID[fill])?.values.name;
  if (!name) throw new Error(`No row template names the ${fill} kind`);
  return name;
}

function kindTitle(choice: RowKindChoice): string {
  return choice.kind === "seasonal"
    ? `${KIND_META.seasonal.title} (${FILL_META[choice.fill].title})`
    : KIND_META[choice.kind].title;
}

function listOf(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function watches(count: number): string {
  return `${count} watch${count === 1 ? "" : "es"}`;
}

/** "Based on …", in the words of the Based on choices (design §5). */
export function basedOn(maxSeeds: number, media: string): string {
  if (media === "both") {
    if (maxSeeds === 2) return "their latest film and their latest show";
    if (maxSeeds === 1) return "only the very last thing they watched";
    return `a blend of their last ${maxSeeds} watches`;
  }
  const noun = media === "movie" ? "film" : "show";
  return maxSeeds === 1 ? `their latest ${noun}` : `a blend of their last ${maxSeeds} ${noun}s`;
}

function maxSeedsLine(after: CollectionInput, fill: RowFill, ctx: RowKindContext): string {
  if (fill === "byw" && after.max_seeds !== null) return `Based on ${basedOn(after.max_seeds, after.media)}.`;
  if (after.max_seeds === null) {
    return `Matches picks to their last ${watches(ctx.globalMaxSeeds)} (the global default).`;
  }
  const edge = fill === "picked" && isNarrowGlobal(ctx);
  return `Matches picks to their last ${watches(after.max_seeds)}.${
    edge ? ` The global default (${ctx.globalMaxSeeds}) would make it a Because you watched row.` : ""
  }`;
}

/** One sentence per field the patch changed, in the order a reader thinks about them. */
function changeLines(
  before: CollectionInput,
  after: CollectionInput,
  fill: RowFill,
  ctx: RowKindContext,
): string[] {
  const lines: string[] = [];
  const changed = (field: keyof CollectionInput) => JSON.stringify(before[field]) !== JSON.stringify(after[field]);

  if (changed("seasons")) {
    const count = after.seasons.length;
    const all = count === ctx.seasonCatalogue.length ? `all ${count} seasons` : `${count} season${count === 1 ? "" : "s"}`;
    lines.push(
      count === 0
        ? "Stops following the calendar, so the row can show all year."
        : before.seasons.length === 0
          ? `Follows the calendar with ${all} picked. Narrow them down under Seasons.`
          : `Follows ${all} instead of the ${before.seasons.length} picked now.`,
    );
  }
  if (changed("build")) {
    lines.push(
      after.build === "shared"
        ? "Makes it one shared row, the same for everyone who gets it."
        : "Gives each person their own private row, built from what they watch.",
    );
  }
  if (changed("rewatch")) {
    lines.push(
      after.rewatch
        ? "Leads the row with favourites they've already finished, then fills it with new picks."
        : "Stops leading the row with titles they've already finished.",
    );
  }
  if (changed("requests_row")) {
    lines.push(
      after.requests_row
        ? "Fills the row with what they asked for that's now on Plex, newest first, instead of recommendations."
        : "Stops showing what they asked for, and fills the row with recommendations again.",
    );
  }
  if (changed("max_seeds")) lines.push(maxSeedsLine(after, fill, ctx));
  if (changed("seed_window")) {
    lines.push(
      after.seed_window <= 1
        ? `Stops taking turns between their last ${watches(before.seed_window)}.`
        : `Takes turns between their last ${watches(after.seed_window)}${
            before.seed_window > 1 ? `, not ${before.seed_window}` : ""
          }.`,
    );
  }
  if (changed("unstarted_only")) {
    lines.push(
      after.unstarted_only
        ? "Turns Only series they haven't started back on."
        : fill === "again"
          ? "Turns off Only series they haven't started, which can't be combined with Watch it again."
          : "Turns off Only series they haven't started.",
    );
  }
  if (changed("request_tag")) {
    lines.push(
      after.request_tag
        ? `Sets the request tag back to “${after.request_tag}”.`
        : `Clears the request tag “${before.request_tag}”.`,
    );
  }
  return lines;
}

/** What saving does on Plex, from the row as saved to the row as switched (`row_changes.plan_row_changes`). */
function plexNote(saved: CollectionInput, after: CollectionInput, ctx: Pick<RowKindContext, "pausedAll">): string {
  if (!saved.enabled) return PLEX_NOTE_OFF;
  // A build flip is planned alone — the plan returns straight after it — so it is all saving does.
  if (saved.build !== after.build) {
    return waitsOnARun(after.build === "shared" ? PLEX_NOTE_TO_SHARED : PLEX_NOTE_TO_PER_PERSON, ctx);
  }
  if (JSON.stringify(saved.seasons) === JSON.stringify(after.seasons)) return waitsOnARun(PLEX_NOTE_NEXT_RUN, ctx);
  return waitsOnARun(after.seasons.length > 0 ? PLEX_NOTE_SEASONS : PLEX_NOTE_LEAVING_SEASONS, ctx);
}

/** The rename a switch calls for (design §4.2), before deciding where it happens. */
function renameFor(
  before: CollectionInput,
  after: CollectionInput,
  currentFill: RowFill,
  fill: RowFill,
  ctx: RowKindContext,
): KindChangeRename | null {
  const seedName = namesASeed(before, ctx);
  // A {top_seed} name reads a Picked for You row straight back as Because you watched, and a shared
  // row has no watch to fill it with: its picks carry no seed (`rows._shared_row`), so
  // `delivery.render_row_name` renders no name and the row isn't built. The API accepts it all the
  // same. Watch it again keeps it: the engine names that row after a watch too (`rows._names_a_seed`
  // ignores rewatch), and rebuilds it nightly.
  const refusesSeed = REFUSES_SEED.has(fill);
  const dropSeed = fill !== currentFill && seedName && refusesSeed;
  // The API refuses {season} or {season_emoji} on a row that follows no season.
  const dropSeason = after.seasons.length === 0 && usesSeason(effectiveRowName(before, ctx));
  if (dropSeed || dropSeason) {
    return {
      required: true,
      proposed: templateName(fill),
      mustNotUse: [...(refusesSeed ? [TOP_SEED] : []), ...(dropSeason ? SEASON_TOKENS : [])],
      because: [...(dropSeed ? [TOP_SEED] : []), ...(dropSeason ? SEASON_TOKENS : [])],
    };
  }
  if (fill !== currentFill && !seedName && fill === "byw") {
    return { required: false, proposed: templateName("byw"), mustNotUse: [], because: [] };
  }
  return null;
}

/** The fact line for a row the engine rebuilds nightly because it follows a watch. */
export const NIGHTLY_LINE = "Picks new titles every night, so it keeps up with their latest watch.";

/**
 * What switching this row to another kind will do, for the confirm dialog (design §4.1, §4.2, §11).
 * Reads, never writes: once the owner confirms, the caller applies the same switch (`kindSwitchBase`,
 * `applyRowKind`, then `normalizeKindResult`).
 *
 * The lines describe the row as it will be under the name it ends up with — a required rename's, or
 * the one the owner typed (`renameTo`) — since that is what the dialog asks the owner to agree to.
 */
export function describeKindChange(
  onScreen: CollectionInput,
  choice: RowKindChoice,
  ctx: RowKindContext,
  { baseline, saved, renameTo = null }: KindChangeOptions = {},
): KindChange {
  // The switch is applied to `from`; the lines say what it does to the row the owner is looking at.
  const from = baseline ? kindSwitchBase(onScreen, baseline, choice, ctx) : onScreen;
  const savedRow = saved ?? from;
  const current = rowKindOf(onScreen, ctx);
  const fill = targetFill(choice);
  const after = normalizeKindResult(applyRowKind(from, choice, ctx));

  // Worked out from the saved (or typed) name, not a rename an earlier switch asked for.
  const wanted = renameFor(from, after, rowKindOf(from, ctx).fill, fill, ctx);
  const rename = ctx.isDefault ? null : wanted;
  const renameInSettings = ctx.isDefault ? wanted : null;

  let viewInput = after;
  let viewCtx = ctx;
  const newName = rename ? (renameTo ?? (rename.required ? rename.proposed : null)) : null;
  if (newName !== null) viewInput = { ...after, name: newName, name_template: newName };
  if (renameInSettings?.required) viewCtx = { ...ctx, defaultRowName: renameInSettings.proposed };

  const lines = changeLines(onScreen, after, fill, ctx);
  if (fill === "popular" && current.fill !== "popular") lines.push("A shared row never asks for missing titles.");
  lines.push(...settingLines(onScreen, after, visibleSettings(onScreen, ctx), visibleSettings(viewInput, viewCtx)));
  // `effective_refresh_days` forces nightly on any per-person row that follows a watch, whether or not
  // this switch is what made it follow one. A shared row reads neither the name's seed nor rotation.
  const switching = choice.kind !== current.kind || fill !== current.fill;
  if (switching && fill !== "popular" && followsAWatch(viewInput, viewCtx)) lines.push(NIGHTLY_LINE);

  return {
    title: `Change this row to ${kindTitle({ kind: choice.kind, fill })}?`,
    lines,
    plexNote: plexNote(savedRow, after, ctx),
    renameNote: newName === null ? null : renameAtNote(renameAt(savedRow, after, newName), ctx),
    rename,
    renameInSettings,
  };
}

/**
 * "Adds …" and "Hides …". A setting whose fields the patch changed is left to its change line, and
 * one whose fields stay on screen under another setting (Based on vs the watch count) isn't listed.
 */
function settingLines(
  before: CollectionInput,
  after: CollectionInput,
  shownBefore: ReadonlySet<RowSettingKey>,
  shownAfter: ReadonlySet<RowSettingKey>,
): string[] {
  const untouched = (key: RowSettingKey) =>
    fieldsOf(key).every((field) => JSON.stringify(before[field]) === JSON.stringify(after[field]));
  const fieldsShown = (shown: ReadonlySet<RowSettingKey>) => new Set([...shown].flatMap(fieldsOf));
  const fieldsBefore = fieldsShown(shownBefore);
  const fieldsAfter = fieldsShown(shownAfter);

  const added = ROW_SETTING_KEYS.filter(
    (key) =>
      shownAfter.has(key) &&
      !shownBefore.has(key) &&
      untouched(key) &&
      fieldsOf(key).some((field) => !fieldsBefore.has(field)),
  );
  const hidden = ROW_SETTING_KEYS.filter(
    (key) =>
      shownBefore.has(key) &&
      !shownAfter.has(key) &&
      untouched(key) &&
      fieldsOf(key).some((field) => !fieldsAfter.has(field)),
  );

  const lines: string[] = [];
  const labels = (keys: RowSettingKey[]) => listOf(keys.map((key) => SETTING_LABELS[key]));
  if (added.length > 0) {
    lines.push(`Adds ${added.length === 1 ? "a setting" : "settings"} this kind uses: ${labels(added)}.`);
  }
  if (hidden.length > 0) {
    const one = hidden.length === 1;
    lines.push(
      `Hides ${one ? "a setting" : "settings"} this kind doesn't use: ${labels(hidden)}. What ${
        one ? "it's" : "they're"
      } set to is kept, so switching back restores it.`,
    );
  }
  return lines;
}
