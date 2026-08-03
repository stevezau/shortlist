import { Ban } from "lucide-react";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { timeAgo } from "@/lib/format";
import { useBlockSeed, useUserHistory } from "@/lib/queries";
import { blockedSeeds } from "@/lib/types";
import type { User, WatchItem } from "@/lib/types";

/** "S2 · E5 · Episode title" for a show watch — as much as the source reported, or null if none. */
function episodeLabel(item: WatchItem): string | null {
  if (item.media_type !== "show") return null;
  const parts: string[] = [];
  if (item.season != null) parts.push(`S${item.season}`);
  if (item.episode != null) parts.push(`E${item.episode}`);
  if (item.episode_title) parts.push(item.episode_title);
  return parts.length ? parts.join(" · ") : null;
}

/** A user's recent watches, read from Plex per user. All four states via QueryBoundary.
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
  const query = useUserHistory(userId);
  const block = useBlockSeed(userId);
  const alreadyBlocked = new Set(
    blockedSeeds(user?.prefs).map((seed) => seed.tmdb_id),
  );
  return (
    <QueryBoundary
      query={query}
      skeleton={<Skeleton className="h-40 w-full" />}
      isEmpty={(items) => items.length === 0}
      empty={
        <EmptyState
          title="No watch history"
          hint="Shortlist sees nothing this person has watched yet — recommendations start once they do."
        />
      }
    >
      {(items) => (
        <ul className="divide-y">
          {items.map((item, i) => {
            const episode = episodeLabel(item);
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
                  {episode ? (
                    <span className="block text-xs text-muted-foreground">
                      {episode}
                    </span>
                  ) : null}
                </span>
                <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                  {item.media_type === "show" ? "Show" : "Movie"} ·{" "}
                  {timeAgo(item.watched_at)}
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
      )}
    </QueryBoundary>
  );
}
