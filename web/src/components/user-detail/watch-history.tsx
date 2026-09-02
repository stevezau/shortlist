import { Ban, Search } from "lucide-react";
import { useState } from "react";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { WatchRating } from "@/components/user-detail/watch-rating";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { timeAgo } from "@/lib/format";
import { useBlockSeed, useUserWatched } from "@/lib/queries";
import { blockedSeeds } from "@/lib/types";
import type { User, WatchedPage, WatchedTitle } from "@/lib/types";
import { useDebouncedValue } from "@/lib/use-debounced-value";

const PAGE = 25;

/** How much of a show they've actually seen, or how often they've rewatched a film.
 *
 *  This is the line that answers "I've watched that — why was it recommended?". A part-watched show
 *  is CORRECTLY still eligible (the engine only drops one seen past `watched_show_pct`), and until
 *  the page said so the only way to find that out was to ask. Both counts are null for movies and
 *  for anything Plex reports no episode totals for — "0 of 0" would be a different, wrong claim.
 */
export function watchDepth(item: WatchedTitle): string | null {
  if (item.media_type === "show") {
    if (item.leaf_count == null || item.viewed_leaf_count == null) return null;
    if (item.viewed_leaf_count >= item.leaf_count) return "finished";
    return `${item.viewed_leaf_count} of ${item.leaf_count} episodes`;
  }
  return item.watch_count > 1 ? `watched ${item.watch_count}×` : null;
}

/** Why an active filter found nothing, in the words of the filters that are on.
 *
 *  The combination worth naming is a type and a library that can't both be true — a TV library holds
 *  no movies — which otherwise looks like a broken page rather than an impossible question.
 */
function emptyFilterHint(mediaType: string, library: string): string {
  const type = mediaType === "movie" ? "movies" : "shows";
  if (mediaType && library) return `No ${type} watched in ${library}.`;
  if (library) return `Nothing watched in ${library} yet.`;
  return "No watched title of that type yet.";
}

/** A user's watched set, searchable — read from Shortlist's cache rather than live from Plex.
 *
 *  That choice is the point: this is the SAME set every recommendation is filtered against, so the
 *  page can answer why something was picked. The cost is freshness, which the footer states rather
 *  than hides — a live read would be fresher but could only ever show the 25 most recent titles,
 *  making a search box that mostly finds nothing.
 *
 *  Each watch carries a Block control, because THIS is where you realise a watch shouldn't be shaping
 *  their picks — you're looking at the sport, or the thing they put on for a friend. Sending someone
 *  to a separate settings panel to retype a title that's already on screen is the kind of small
 *  friction that means the feature never gets used.
 */
export function WatchHistory({
  userId,
  user,
}: {
  userId: number;
  user?: User;
}) {
  const [search, setSearch] = useState("");
  const [mediaType, setMediaType] = useState<"" | "movie" | "show">("");
  const [library, setLibrary] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const debounced = useDebouncedValue(search, 250);
  const query = useUserWatched(userId, {
    q: debounced,
    mediaType,
    library,
    limit,
  });
  const block = useBlockSeed(userId);
  const alreadyBlocked = new Set(
    blockedSeeds(user?.prefs).map((seed) => seed.tmdb_id),
  );

  // A new filter starts a new list — carrying the old "Show 50 more" over would ask the server for
  // 200 rows of a search that has three.
  const reset = (next: () => void) => {
    setLimit(PAGE);
    next();
  };

  // Read off the last page rather than a second request: the response carries every library this
  // person has watched in, unnarrowed by the current filter. The selected one is unioned in so a
  // library renamed in Plex between requests can still be seen and cleared, rather than leaving a
  // blank control with a filter silently applied.
  const libraries = Array.from(
    new Set([...(query.data?.libraries ?? []), ...(library ? [library] : [])]),
  ).sort();

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="relative sm:max-w-xs sm:flex-1">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            type="search"
            className="pl-9"
            placeholder="Search watched titles…"
            aria-label="Search watched titles"
            value={search}
            onChange={(e) => reset(() => setSearch(e.target.value))}
          />
        </div>
        <div className="flex items-center gap-2">
          <Segmented<"" | "movie" | "show">
            value={mediaType}
            ariaLabel="Filter by type"
            options={[
              { value: "", label: "All" },
              { value: "movie", label: "Movies" },
              { value: "show", label: "Shows" },
            ]}
            onChange={(value) => reset(() => setMediaType(value))}
          />
          {/* A dropdown rather than more buttons: library counts vary from two to a dozen between
              servers, and a segmented row of a dozen wrecks the toolbar. Hidden entirely when there
              is only one — the control could then only ever say "All libraries". */}
          {libraries.length > 1 && (
            <select
              value={library}
              onChange={(e) => reset(() => setLibrary(e.target.value))}
              aria-label="Filter by library"
              className="h-9 rounded-md border bg-background px-2 text-sm"
            >
              <option value="">All libraries</option>
              {libraries.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      <QueryBoundary
        query={query}
        skeleton={<Skeleton className="h-40 w-full" />}
        isEmpty={(page) => page.items.length === 0}
        empty={
          debounced || mediaType || library ? (
            <EmptyState
              title="Nothing matches that"
              hint={
                debounced
                  ? `No watched title contains “${debounced}”. Their history may not have synced yet — check the count below the list.`
                  : emptyFilterHint(mediaType, library)
              }
            />
          ) : (
            <EmptyState
              title="No watch history"
              hint="Shortlist sees nothing this person has watched yet — recommendations start once they do."
            />
          )
        }
      >
        {(page) => (
          <>
            <ul className="divide-y">
              {page.items.map((item, i) => {
                const depth = watchDepth(item);
                return (
                  <li
                    key={i}
                    className="flex items-baseline justify-between gap-3 py-2"
                  >
                    <span className="min-w-0 text-sm">
                      <span className="font-medium">{item.title}</span>
                      {item.year ? (
                        <span className="text-muted-foreground">
                          {" "}
                          ({item.year})
                        </span>
                      ) : null}
                      {/* Which Plex libraries hold it. Two names is a title stored twice — this row
                          used to be two rows, each with its own Block button that did the same
                          thing. Empty for a watch cached before the name was recorded; the next sync
                          fills it in, and no line is better than a guessed one. */}
                      {item.libraries.length > 0 && (
                        <span className="block text-xs text-muted-foreground/80">
                          {item.libraries.join(" · ")}
                        </span>
                      )}
                    </span>
                    <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                      {item.media_type === "show" ? "Show" : "Movie"}
                      {depth ? ` · ${depth}` : ""} · {timeAgo(item.watched_at)}
                      <WatchRating rating={item.user_rating} page={page} />
                      {/* No tmdb:// GUID means nothing a block could key on, so no button rather than one
                          that fails. */}
                      {item.tmdb_id !== null &&
                        (alreadyBlocked.has(item.tmdb_id) ? (
                          <span
                            className="inline-flex items-center gap-1 text-muted-foreground/70"
                            title="Already blocked — it stays in their history but no longer shapes their picks"
                          >
                            <Ban className="h-3 w-3" aria-hidden />
                            blocked
                          </span>
                        ) : (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 px-1.5 text-xs text-muted-foreground hover:text-foreground"
                            disabled={block.isPending}
                            title={`Stop "${item.title}" shaping their picks`}
                            aria-label={`Block ${item.title} — stop it shaping their picks`}
                            onClick={() =>
                              block.mutate({
                                tmdbId: item.tmdb_id as number,
                                title: item.title,
                                mediaType: item.media_type,
                                year: item.year ?? undefined,
                              })
                            }
                          >
                            <Ban className="h-3 w-3" aria-hidden />
                            Block
                          </Button>
                        ))}
                    </span>
                  </li>
                );
              })}
            </ul>

            {page.total > page.items.length && (
              <div className="flex justify-center">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setLimit((n) => n + 50)}
                >
                  Show 50 more
                </Button>
              </div>
            )}

            <SyncFooter page={page} shown={page.items.length} />
          </>
        )}
      </QueryBoundary>
    </div>
  );
}

/** What this list is and how complete it is. Without it "I watched that and it still got picked"
 *  has no visible answer — the set may simply not have been read yet. */
function SyncFooter({ page, shown }: { page: WatchedPage; shown: number }) {
  return (
    <div className="space-y-1.5 border-t pt-3 text-xs text-muted-foreground">
      <p>
        Showing {shown} of {page.total} titles
        {/* "library copies", not "titles": this is a sum of per-library row counts, so a server
            holding anything twice makes it larger than the total above. Two numbers that look like
            the same number and disagree read as a bug. */}
        {page.synced_titles
          ? ` · ${page.synced_titles} library copies synced`
          : ""}{" "}
        ·{" "}
        {page.last_full_sync_at ? (
          <>last full sync {timeAgo(page.last_full_sync_at)}</>
        ) : (
          <>
            never fully synced &mdash; run{" "}
            <strong className="text-foreground">Sync watch history</strong> in
            Jobs
          </>
        )}
      </p>
      <RatingsNote page={page} />
    </div>
  );
}

/** Why the ratings in this list are, or aren't, changing anything.
 *
 *  The distrusted case is the one that has to be said out loud: an account whose ratings were
 *  written by another tool shows a full column of stars that affect nothing, and silence there reads
 *  as a broken feature rather than a deliberate refusal. Says nothing at all when nobody has rated
 *  anything, which is most people — an explanation of an absent feature is just noise.
 */
function RatingsNote({ page }: { page: WatchedPage }) {
  if (!page.rated_count) return null;
  if (!page.ratings_trusted) {
    return (
      <p role="status" className="text-warning">
        Another tool is writing Plex ratings on this account (they aren’t whole
        numbers, which is the one thing Plex’s own star control can’t do), so
        Shortlist ignores all {page.rated_count} of them.
      </p>
    );
  }
  if (page.dislike_threshold === null) {
    return (
      <p>
        They’ve rated {page.rated_count}{" "}
        {page.rated_count === 1 ? "title" : "titles"}. Turn on{" "}
        <strong className="text-foreground">Respect Plex ratings</strong> in
        Settings to stop the ones they disliked shaping their picks.
      </p>
    );
  }
  const stars = page.dislike_threshold / 2;
  return (
    <p>
      They’ve rated {page.rated_count}{" "}
      {page.rated_count === 1 ? "title" : "titles"} in Plex. Anything at or
      below {stars} {stars === 1 ? "star" : "stars"} stops being used to find
      similar titles for them.
    </p>
  );
}
