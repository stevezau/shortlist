import {
  AlertTriangle,
  Cable,
  Inbox,
  KeyRound,
  ListOrdered,
  type LucideIcon,
  Rows3,
  SlidersHorizontal,
  Sparkles,
  Wand2,
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
 * where titles come from → how they're written → row defaults → where rows sit → optional requests →
 * advanced → API access → danger. Each carries the `group` it renders under (headers in the sidebar
 * sub-nav). Schedules are per-row now (each row's editor), not a global Settings section. Shared by
 * the page (which renders each section's content, keyed by `id`) and the sidebar sub-nav (which
 * lists them, grouped, and jumps to `#id`). Keep entries contiguous by group — the sub-nav emits a
 * group header on each change and assumes a group's items don't reappear later.
 */
export const SETTINGS_SECTIONS: NavSection[] = [
  { id: "connections", label: "Connections", icon: Cable, group: "Connect" },
  {
    id: "recommendations",
    label: "Recommendations",
    icon: Sparkles,
    group: "Rows",
  },
  { id: "curation", label: "Curation style", icon: Wand2, group: "Rows" },
  { id: "defaults", label: "Row defaults", icon: Rows3, group: "Rows" },
  { id: "placement", label: "Row placement", icon: ListOrdered, group: "Rows" },
  { id: "requests", label: "Requests", icon: Inbox, group: "Add-ons" },
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
