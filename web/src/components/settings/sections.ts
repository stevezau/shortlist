import {
  AlertTriangle,
  Cable,
  Clock,
  Inbox,
  type LucideIcon,
  Rows3,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Wand2,
} from "lucide-react";

export type NavSection = { id: string; label: string; icon: LucideIcon };

/**
 * The Settings page sections, in the order a new owner works down them: connect things → decide
 * where titles come from → how they're written → row/schedule defaults → optional requests →
 * privacy → advanced → danger. Shared by the page (which renders each section's content, keyed by
 * `id`) and the sidebar sub-nav (which lists them and jumps to `#id`).
 */
export const SETTINGS_SECTIONS: NavSection[] = [
  { id: "connections", label: "Connections", icon: Cable },
  { id: "recommendations", label: "Recommendations", icon: Sparkles },
  { id: "curation", label: "Curation", icon: Wand2 },
  { id: "defaults", label: "Row defaults", icon: Rows3 },
  { id: "schedule", label: "Schedule", icon: Clock },
  { id: "requests", label: "Requests", icon: Inbox },
  { id: "privacy", label: "Privacy", icon: ShieldCheck },
  { id: "advanced", label: "Advanced", icon: SlidersHorizontal },
  { id: "danger", label: "Danger zone", icon: AlertTriangle },
];
