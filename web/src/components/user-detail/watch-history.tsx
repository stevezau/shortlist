import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Skeleton } from "@/components/ui/skeleton";
import { timeAgo } from "@/lib/format";
import { useUserHistory } from "@/lib/queries";
import type { WatchItem } from "@/lib/types";

/** "S2 · E5 · Episode title" for a show watch — as much as the source reported, or null if none. */
function episodeLabel(item: WatchItem): string | null {
  if (item.media_type !== "show") return null;
  const parts: string[] = [];
  if (item.season != null) parts.push(`S${item.season}`);
  if (item.episode != null) parts.push(`E${item.episode}`);
  if (item.episode_title) parts.push(item.episode_title);
  return parts.length ? parts.join(" · ") : null;
}

/** A user's recent watches, read from Plex per user. All four states via QueryBoundary. */
export function WatchHistory({ userId }: { userId: number }) {
  const query = useUserHistory(userId);
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
                <span className="shrink-0 text-xs text-muted-foreground">
                  {item.media_type === "show" ? "Show" : "Movie"} ·{" "}
                  {timeAgo(item.watched_at)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </QueryBoundary>
  );
}
