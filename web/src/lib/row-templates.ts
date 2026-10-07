import { AI_KIND_META, KIND_META, ROW_KINDS, type RowKind } from "@/lib/row-kind-meta";
import {
  GENERATED_AI_TEMPLATES,
  GENERATED_ROW_TEMPLATES,
} from "@/lib/row-templates.generated";
import type { CollectionInput } from "@/lib/types";

/** A template's kind: one of the six, or "ai" (#138), which the kind picker never offers. */
export type TemplateKind = RowKind | "ai";

export interface RowTemplate {
  id: string;
  kind: TemplateKind;
  emoji: string;
  title: string;
  summary: string;
  blurb: string;
  /** The settings this template changes, in plain English. */
  highlights: string[];
  /** A partial starting point; every omitted field keeps `blankInput()`'s default. */
  values: Partial<CollectionInput>;
}

/** The gallery's six headings, in the kind picker's order, each with the kind's description. */
export const ROW_TEMPLATE_GROUPS: {
  kind: RowKind;
  heading: string;
  description: string;
}[] = ROW_KINDS.map((kind) => ({
  kind,
  heading: KIND_META[kind].title,
  description: KIND_META[kind].description,
}));

/** The AI group's heading, in the same shape as the six kinds'. */
export const AI_TEMPLATE_GROUP: {
  kind: "ai";
  heading: string;
  description: string;
} = {
  kind: "ai",
  heading: AI_KIND_META.title,
  description: AI_KIND_META.description,
};

/** The gallery's headings: the six kinds in the picker's order, then AI. */
export const GALLERY_GROUPS = [...ROW_TEMPLATE_GROUPS, AI_TEMPLATE_GROUP];

/** Ready-made ordinary rows generated from the backend's authoritative catalog. */
export const ROW_TEMPLATES: RowTemplate[] = GENERATED_ROW_TEMPLATES;

/** AI row starting points generated from the same backend catalog. */
export const AI_TEMPLATES: RowTemplate[] = GENERATED_AI_TEMPLATES;

export function findRowTemplate(id: string): RowTemplate | undefined {
  return [...ROW_TEMPLATES, ...AI_TEMPLATES].find(
    (template) => template.id === id,
  );
}

/** Names a highlight may start with that keep their capital mid-sentence: the apps, and every season
 *  the engine ships (`shortlist/engine/seasons.py`), matched on the first word. */
const PROPER_NOUNS = new Set([
  "Overseerr",
  "Radarr",
  "Sonarr",
  "Plex",
  "TMDB",
  "Trakt",
  "TV",
  "Halloween",
  "Christmas",
  "Valentine's",
]);

/**
 * The highlights as they read joined into one sentence: each starts lowercase, unless its first
 * word is a proper noun or an acronym (a run of two or more capitals).
 */
export function sentenceCaseHighlights(highlights: string[]): string[] {
  return highlights.map((highlight) => {
    const firstWord = highlight.split(/[\s/,.-]/, 1)[0] ?? "";
    const keepsCapital =
      PROPER_NOUNS.has(firstWord) || /^[A-Z]{2,}/.test(firstWord);
    return keepsCapital
      ? highlight
      : highlight.charAt(0).toLowerCase() + highlight.slice(1);
  });
}
