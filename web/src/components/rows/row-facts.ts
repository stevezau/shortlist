import type { CollectionInput, PlexLibrary, User } from "@/lib/types";

type Audience = Pick<CollectionInput, "audience" | "audience_user_ids">;

/** The people a row reaches as the engine counts them: enabled, not paused, and in its audience. */
export function reachedUsers(row: Audience, users: User[]): User[] {
  return users.filter(
    (user) =>
      user.enabled && !user.prefs?.paused && (row.audience === "everyone" || row.audience_user_ids.includes(user.id)),
  );
}

/** How many people a row reaches; null while the roster is empty or loading, so nothing is claimed on a guess. */
export function rowReach(row: Audience, users: User[]): number | null {
  return users.length === 0 ? null : reachedUsers(row, users).length;
}

/** "1 person", "4 people". */
export function peopleCount(count: number): string {
  return count === 1 ? "1 person" : `${count} people`;
}

/** "Movies & TV Shows", the way the row's media reads in a sentence. */
export function mediaLabel(media: CollectionInput["media"]): string {
  return media === "movie" ? "Movies" : media === "show" ? "TV Shows" : "Movies & TV Shows";
}

/** The libraries a row lands in: the ones it names, or every library of its type when it names none. */
export function rowLibraries(
  row: Pick<CollectionInput, "library_keys" | "media">,
  libraries: PlexLibrary[],
): PlexLibrary[] {
  if (row.library_keys.length > 0) return libraries.filter((library) => row.library_keys.includes(library.key));
  return libraries.filter((library) => row.media === "both" || library.type === row.media);
}

/** When a row was last built: "02:30 today", "02:30 yesterday", then "28 Sep". Local time, like the rest of the app. */
export function builtAt(iso: string, now: Date = new Date()): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "at an unknown time";
  const time = when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (when.toDateString() === now.toDateString()) return `${time} today`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (when.toDateString() === yesterday.toDateString()) return `${time} yesterday`;
  return when.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
