import {
  AlertTriangle,
  Bell,
  Cable,
  Inbox,
  KeyRound,
  ListOrdered,
  type LucideIcon,
  Rows3,
  SlidersHorizontal,
  Sparkles,
} from "lucide-react";

/** The clusters the sections fall into, in display order — so the 9-item list reads as four intents
 *  ("connect things → build the rows → optional add-ons → system") instead of one flat wall. */
export const SETTINGS_GROUPS = [
  "Connect",
  "Rows",
  "Add-ons",
  "System",
] as const;
export type SettingsGroup = (typeof SETTINGS_GROUPS)[number];

export type NavSection = {
  id: string;
  label: string;
  icon: LucideIcon;
  group: SettingsGroup;
};

/**
 * The Settings page sections, in the order a new owner works down them: connect things → decide
 * where titles come from → row defaults → where rows sit → optional requests → advanced → API
 * access → danger. Each carries the `group` it renders under (headers in the sidebar sub-nav).
 * Schedules are per-row now (each row's editor), not a global Settings section. Shared by the page
 * (which renders each section's content, keyed by `id`) and the sidebar sub-nav (which lists them,
 * grouped, and jumps to `#id`). Keep entries contiguous by group — the sub-nav emits a group header
 * on each change and assumes a group's items don't reappear later.
 */
export const SETTINGS_SECTIONS: NavSection[] = [
  { id: "connections", label: "Connections", icon: Cable, group: "Connect" },
  {
    id: "recommendations",
    label: "Finding titles",
    icon: Sparkles,
    group: "Rows",
  },
  { id: "defaults", label: "Row defaults", icon: Rows3, group: "Rows" },
  { id: "placement", label: "Row placement", icon: ListOrdered, group: "Rows" },
  { id: "requests", label: "Requests", icon: Inbox, group: "Add-ons" },
  {
    id: "notifications",
    label: "Notifications",
    icon: Bell,
    group: "System",
  },
  {
    id: "advanced",
    label: "Advanced",
    icon: SlidersHorizontal,
    group: "System",
  },
  { id: "api-access", label: "API access", icon: KeyRound, group: "System" },
  {
    id: "danger",
    label: "Danger zone",
    icon: AlertTriangle,
    group: "System",
  },
];

/** Existing links can target a specific control rather than the section heading. */
export function settingsSectionForHash(hash: string): string {
  const id = hash.replace(/^#/, "");
  if (SETTINGS_SECTIONS.some((section) => section.id === id)) return id;
  if (id === "row-defaults") return "defaults";
  if (["recs-heading", "watched-pct", "refresh-days", "idle-hold-days", "recency", "max-seeds", "recent-count", "use-plex-ratings", "dislike-threshold", "min-history", "cold-start", "rating-source"].includes(id)) return "recommendations";
  const heading = id.replace(/-heading$/, "");
  return SETTINGS_SECTIONS.some((section) => section.id === heading) ? heading : "connections";
}
