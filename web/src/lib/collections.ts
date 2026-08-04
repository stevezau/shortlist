import { freshnessBadgeLabel, watchedBadgeLabel } from "@/lib/constants";
import { placementLabel } from "@/lib/placement";
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
    // The editor renames inline; only the dedicated rename page defers to its own stream.
    defer_rename: false,
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    schedule: "30 3 * * *", // new rows run nightly by default; clear to "Off"
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    rewatch: false,
    unstarted_only: false,
    freshness: null,
    recent_count: null,
    max_seeds: null,
    cold_start: null,
    seed_window: 1,
    pick_order: "best",
    placement: "both",
    placement_friends: "both",
    pin_top: false,
    hub_anchor: {},
    poster: { mode: "", title: "", subtitle: "", style: "" },
  };
}

/** Project a saved collection onto the editable input shape the editor and PATCH share. */
export function toInput(collection: Collection): CollectionInput {
  return {
    name: collection.name,
    defer_rename: false,
    build: collection.build,
    audience: collection.audience,
    audience_user_ids: collection.audience_user_ids,
    enabled: collection.enabled,
    schedule: collection.schedule ?? "",
    size: collection.size,
    media: collection.media,
    sort_order: collection.sort_order,
    name_template: collection.name_template,
    min_watchers: collection.min_watchers,
    request_tag: collection.request_tag,
    candidate_sources: collection.candidate_sources,
    library_keys: collection.library_keys,
    watched_pct: collection.watched_pct ?? null,
    rewatch: collection.rewatch ?? false,
    unstarted_only: collection.unstarted_only ?? false,
    freshness: collection.freshness ?? null,
    recent_count: collection.recent_count ?? null,
    max_seeds: collection.max_seeds ?? null,
    cold_start: collection.cold_start ?? null,
    seed_window: collection.seed_window ?? 1,
    pick_order: collection.pick_order ?? "best",
    placement: collection.placement ?? "both",
    placement_friends: collection.placement_friends ?? "both",
    pin_top: collection.pin_top ?? false,
    hub_anchor: collection.hub_anchor ?? {},
    poster: {
      // Legacy "generate" rows edit as "ai" (the renamed engine).
      mode:
        collection.poster?.mode === "generate"
          ? "ai"
          : (collection.poster?.mode ?? ""),
      title: collection.poster?.title ?? "",
      subtitle: collection.poster?.subtitle ?? "",
      style: collection.poster?.style ?? "",
    },
  };
}

/** One-line "who sees this row" summary for a row card. */
export function audienceSummary(collection: Collection, users: User[]): string {
  if (collection.audience === "everyone") return "Everyone";
  const names = collection.audience_user_ids
    .map((id) => {
      const user = users.find((u) => u.id === id);
      return user && (user.display_name || user.username);
    })
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

  // Badged BEFORE the watched cap, and instead of it: on a rewatch row the cap is plumbing (it only
  // stops the pool dropping finished titles), so "Watched: no filter" describes the mechanism while
  // "Rewatches first" describes the row. Showing both would read as two competing settings.
  if (collection.rewatch) {
    parts.push("Rewatches first");
  } else if (
    // null inherits the global recommendations.watched_pct, so there's nothing to badge. Unlike the
    // prompt, this override IS honoured on the default row, so it isn't gated on the slug.
    collection.watched_pct !== null &&
    collection.watched_pct !== undefined
  ) {
    parts.push(watchedBadgeLabel(collection.watched_pct));
  }

  if (collection.unstarted_only) {
    parts.push("Never started only");
  }

  // null inherits the global freshness, so only badge a per-row override.
  if (collection.freshness !== null && collection.freshness !== undefined) {
    parts.push(freshnessBadgeLabel(collection.freshness));
  }

  // null inherits the global recent_count (web-search recency), so only badge a per-row override.
  if (
    collection.recent_count !== null &&
    collection.recent_count !== undefined
  ) {
    parts.push(`Recent watches: ${collection.recent_count}`);
  }

  // null inherits the engine's seed budget, so only badge a per-row override.
  if (collection.max_seeds !== null && collection.max_seeds !== undefined) {
    parts.push(
      `Built from ${collection.max_seeds} ${collection.max_seeds === 1 ? "watch" : "watches"}`,
    );
  }

  // null inherits the global cold-start behaviour, so only badge a row that overrides it — and only
  // "skip" is worth a badge: it is the one that makes a row silently absent for someone.
  if (collection.cold_start === "skip") {
    parts.push("Skipped without watch history");
  }

  // 1 = always their most recent watch (the default), so only badge a row that actually cycles —
  // otherwise the setting is invisible on the rows page and nobody discovers they turned it on.
  if ((collection.seed_window ?? 1) > 1) {
    parts.push(`Cycles ${collection.seed_window} recent watches`);
  }

  // Only badge when placement differs from the "both" default.
  if (
    collection.placement !== "both" ||
    collection.placement_friends !== "both"
  ) {
    const owner = placementLabel(collection.placement);
    const friends = placementLabel(collection.placement_friends);
    if (collection.placement === collection.placement_friends) {
      if (collection.placement !== "both") parts.push(`Shows on: ${owner}`);
    } else {
      parts.push(`Owner: ${owner} · Friends: ${friends}`);
    }
  }
  // Legacy row-level pin, or a per-library "Top" set via the Position control.
  const anchors = Object.values(collection.hub_anchor ?? {});
  if (collection.pin_top || anchors.some((a) => a.top))
    parts.push("Pinned to top");
  else if (anchors.some((a) => a.anchor)) parts.push("Custom shelf position");

  return parts;
}
