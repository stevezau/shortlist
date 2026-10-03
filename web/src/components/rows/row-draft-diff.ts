import { hasUnsavedChanges, toInput } from "@/lib/collections";
import { describeCron } from "@/lib/cron";
import { SETTING_LABELS, type RowSettingKey } from "@/lib/row-kinds";
import { showDaysSummary } from "@/lib/show-days";
import type { Collection, CollectionInput } from "@/lib/types";

/** One line of the save bar: the setting, and old → new when the value says something in a few words. */
export type DraftChange = { label: string; from?: string; to?: string };

type Key = keyof CollectionInput;

/** The editor's own words for each field, so the save bar names the control that was touched. */
const LABELS: Partial<Record<Key, string>> = {
  name: "Name",
  name_template: "Name",
  description: "Description",
  poster: "Artwork",
  audience: "Who gets it",
  audience_user_ids: "Who gets it",
  library_keys: "Libraries",
  media: "Libraries",
  size: "How many titles",
  pick_order: "Order",
  schedule: "Runs on…",
  refresh_days: "Titles refresh every…",
  idle_hold_days: "Hold when they aren't watching",
  placement: "Where it shows",
  placement_friends: "Where it shows",
  hub_anchor: "Shelf position",
  pin_top: "Shelf position",
  show_days: "Show this row",
  sort_title_prefix: "Sort title prefix",
  build: "Row type",
  rewatch: "Row type",
  requests_row: "Row type",
  seasons: "Seasons",
  season_lead_days: "Seasons",
  season_after_days: "Seasons",
  fallback_name: "Name for someone who's new",
  ai_instructions: "AI instructions",
};

const AI_INSTRUCTIONS_MODE: Record<CollectionInput["ai_instructions"]["mode"], string> = {
  default: "Use the default",
  add: "Add to the default",
  own: "Write your own",
};

const PICK_ORDER: Record<CollectionInput["pick_order"], string> = {
  best: "Best match",
  rating: "Highest rated",
  newest: "Newest released",
  shuffle: "Shuffled",
  new_first: "Just added",
  rotate: "Taking turns",
};

function labelFor(key: Key): string {
  if (LABELS[key]) return LABELS[key];
  if (key === "request_tag" || key.startsWith("req_")) return "Requests";
  if (key in SETTING_LABELS) return SETTING_LABELS[key as RowSettingKey];
  return key.replace(/_/g, " ");
}

function days(value: number | null, zero: string, one: string): string {
  if (value === null) return "global default";
  if (value <= 0) return zero;
  return value === 1 ? one : `${value} days`;
}

function quoted(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "empty";
  return trimmed.length > 24 ? `“${trimmed.slice(0, 23)}…”` : `“${trimmed}”`;
}

/** old → new in a few words, for the fields whose value reads well in one line; null otherwise. */
function describe(key: Key, input: CollectionInput): string | null {
  switch (key) {
    case "pick_order":
      return PICK_ORDER[input.pick_order];
    case "schedule":
      return input.schedule.trim() ? describeCron(input.schedule) || input.schedule : "Off";
    case "refresh_days":
      return days(input.refresh_days, "never", "every night");
    case "idle_hold_days":
      return days(input.idle_hold_days, "no hold", "1 day");
    case "size":
      return `${input.size} titles`;
    case "audience":
      return input.audience === "everyone" ? "Everyone" : "Chosen people";
    case "description":
      return quoted(input.description);
    case "sort_title_prefix":
      return input.sort_title_prefix.trim() ? quoted(input.sort_title_prefix) : "none";
    case "show_days":
      return showDaysSummary(input.show_days);
    case "ai_instructions":
      return AI_INSTRUCTIONS_MODE[input.ai_instructions.mode];
    default:
      return null;
  }
}

/**
 * What the form changes compared with the row as saved, one entry per setting the owner would name.
 *
 * Each field is tested with `hasUnsavedChanges` itself — the saved row with only that field swapped
 * in — so the save bar can never count a change the editor's own comparison doesn't see, or miss one
 * it does. `pendingRename` is the name a kind switch asked for: Save sends it, so it is a change.
 */
export function draftChanges(
  input: CollectionInput,
  saved: Collection | null,
  pendingRename: { name: string; from: string } | null = null,
): DraftChange[] {
  const changes = new Map<string, DraftChange>();
  if (pendingRename && pendingRename.name.trim() !== pendingRename.from.trim()) {
    changes.set("Name", { label: "Name", from: pendingRename.from, to: pendingRename.name.trim() });
  }
  if (!saved) return [...changes.values()];
  const before = toInput(saved);
  for (const key of Object.keys(input) as Key[]) {
    if (!hasUnsavedChanges({ ...before, [key]: input[key] }, saved)) continue;
    const label = labelFor(key);
    if (changes.has(label)) continue;
    const from = describe(key, before);
    const to = describe(key, input);
    changes.set(label, from !== null && to !== null && from !== to ? { label, from, to } : { label });
  }
  return [...changes.values()];
}

/** "Order: Best match → Shuffled, Description" — the first change in full, then the rest by name. */
export function changesSummary(changes: DraftChange[]): string {
  return changes
    .map((change, index) =>
      index === 0 && change.from !== undefined ? `${change.label}: ${change.from} → ${change.to}` : change.label,
    )
    .join(", ");
}
