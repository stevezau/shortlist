import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Skeleton } from "@/components/ui/skeleton";
import { timeAgo } from "@/lib/format";
import { useUserHistory } from "@/lib/queries";

/** A user's recent watches, straight from Plex/Tautulli. All four states via QueryBoundary. */
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
          hint="Rowarr sees nothing this person has watched yet — recommendations start once they do."
        />
      }
    >
      {(items) => (
        <ul className="divide-y">
          {items.map((item, i) => (
            <li
              key={i}
              className="flex items-baseline justify-between gap-3 py-2"
            >
              <span className="text-sm">
                <span className="font-medium">{item.title}</span>
                {item.year ? (
                  <span className="text-muted-foreground"> ({item.year})</span>
                ) : null}
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {item.media_type === "show" ? "Show" : "Movie"} ·{" "}
                {timeAgo(item.watched_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </QueryBoundary>
  );
}
