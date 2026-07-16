import {
  DEFAULT_ROW_SLUG,
  freshnessBadgeLabel,
  TONE_LABELS,
  watchedBadgeLabel,
} from "@/lib/constants";
import { SOURCES, sourceBlockedReason, sourceShortLabel } from "@/lib/sources";
import type {
  Collection,
  CollectionInput,
  PlexLibrary,
  Settings,
  User,
} from "@/lib/types";

/** A fresh row definition with sensible defaults, for the "Add a row" editor. */
export function blankInput(): CollectionInput {
  return {
    name: "",
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    freshness: null,
    placement: "both",
    pin_top: false,
    hub_anchor: {},
    prompt: { tone: "", guidance: "", template: "" },
  };
}

/** Project a saved collection onto the editable input shape the editor and PATCH share. */
export function toInput(collection: Collection): CollectionInput {
  return {
    name: collection.name,
    build: collection.build,
    audience: collection.audience,
    audience_user_ids: collection.audience_user_ids,
    enabled: collection.enabled,
    size: collection.size,
    media: collection.media,
    sort_order: collection.sort_order,
    name_template: collection.name_template,
    min_watchers: collection.min_watchers,
    request_tag: collection.request_tag,
    candidate_sources: collection.candidate_sources,
    library_keys: collection.library_keys,
    watched_pct: collection.watched_pct ?? null,
    freshness: collection.freshness ?? null,
    placement: collection.placement ?? "both",
    pin_top: collection.pin_top ?? false,
    hub_anchor: collection.hub_anchor ?? {},
    prompt: {
      tone: collection.prompt.tone ?? "",
      guidance: collection.prompt.guidance ?? "",
      template: collection.prompt.template ?? "",
    },
  };
}

/** One-line "who sees this row" summary for a row card. */
export function audienceSummary(collection: Collection, users: User[]): string {
  if (collection.audience === "everyone") return "Everyone";
  const names = collection.audience_user_ids
    .map((id) => users.find((u) => u.id === id)?.username)
    .filter(Boolean);
  if (names.length === 0) return "No one yet";
  return names.length <= 2
    ? names.join(" & ")
    : `${names.slice(0, 2).join(", ")} +${names.length - 2}`;
}

/**
 * The ways this row departs from the global defaults, in plain English.
 *
 * Every one of these is editable per row, but a row card that only showed "everyone · 15 titles"
 * made the overrides invisible — an owner couldn't tell a Trakt-only, cinephile-toned row from a
 * stock one without opening the editor. Empty array = this row is entirely on the defaults.
 *
 * `libraries` is null while the library list is still loading or unavailable: a raw section key is
 * not a name an owner recognises, so the libraries part is withheld rather than guessed at.
 */
export function rowOverrides(
  collection: Collection,
  libraries: PlexLibrary[] | null,
  settings?: Settings,
): string[] {
  const parts: string[] = [];

  if (collection.candidate_sources.length > 0) {
    if (!settings) {
      // Settings not known yet — list every chosen source without judging what can run.
      parts.push(
        `Sources: ${collection.candidate_sources.map(sourceShortLabel).join(", ")}`,
      );
    } else {
      // Capability-aware: a source whose global dependency (key/curator) isn't met can't run, so it's
      // badged "Needs setup" rather than advertised as active — the card never claims a dead source.
      const runnable: string[] = [];
      const blocked: string[] = [];
      for (const id of collection.candidate_sources) {
        const source = SOURCES.find((s) => s.id === id);
        const isBlocked = source
          ? sourceBlockedReason(source, settings) !== null
          : false;
        (isBlocked ? blocked : runnable).push(sourceShortLabel(id));
      }
      if (runnable.length > 0) parts.push(`Sources: ${runnable.join(", ")}`);
      if (blocked.length > 0) parts.push(`Needs setup: ${blocked.join(", ")}`);
    }
  }

  if (collection.library_keys.length > 0 && libraries !== null) {
    const titles = collection.library_keys.map(
      (key) => libraries.find((l) => l.key === key)?.title ?? `Library ${key}`,
    );
    parts.push(`Libraries: ${titles.join(", ")}`);
  }

  // null inherits the global recommendations.watched_pct, so there's nothing to badge. Unlike the
  // prompt, this override IS honoured on the default row, so it isn't gated on the slug.
  if (collection.watched_pct !== null && collection.watched_pct !== undefined) {
    parts.push(watchedBadgeLabel(collection.watched_pct));
  }

  // null inherits the global freshness, so only badge a per-row override.
  if (collection.freshness !== null && collection.freshness !== undefined) {
    parts.push(freshnessBadgeLabel(collection.freshness));
  }

  // "both" is the default placement (Home + Library), so only badge a narrowed one.
  if (collection.placement === "home") parts.push("Shows on: Home");
  else if (collection.placement === "library") parts.push("Shows on: Library");
  if (collection.pin_top) parts.push("Pinned to top");

  // The default row's style is the GLOBAL recipe — the server discards its stored prompt — so
  // badging one here would advertise a setting no run will ever apply.
  if (collection.slug !== DEFAULT_ROW_SLUG) {
    const { tone, guidance, template } = collection.prompt ?? {};
    // A blank tone means the row inherits Settings → Curation style, so there's nothing to badge.
    const toneLabel = tone ? (TONE_LABELS[tone] ?? tone) : "";
    if (template) parts.push("Style: custom prompt");
    else if (guidance) parts.push(`Style: ${toneLabel || "Inherited"} + notes`);
    else if (tone) parts.push(`Style: ${toneLabel}`);
  }

  return parts;
}
