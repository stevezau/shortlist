import { useState, type ReactNode } from "react";

import { MutationAlert } from "@/components/mutation-alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { useBlockSeed, useHistoryMix, useUnblockSeed } from "@/lib/queries";
import { blockedSeeds } from "@/lib/types";
import type { HistoryMixItem, User } from "@/lib/types";

const RECENT_SHOWN = 5;

function itemLabel(item: HistoryMixItem): string {
  return item.year ? `${item.title} (${item.year})` : item.title;
}

/** One title in a favourites or older list, with the same block/unblock the watch history uses. */
function MixItem({
  item,
  blocked,
  busy,
  onToggle,
}: {
  item: HistoryMixItem;
  blocked: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <li className="flex items-center justify-between gap-3 py-1 text-sm">
      <span className={blocked ? "min-w-0 text-muted-foreground line-through" : "min-w-0"}>
        {itemLabel(item)}
      </span>
      {item.tmdb_id !== null && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="shrink-0"
          disabled={busy}
          aria-label={`${blocked ? "Undo" : "Don’t use"} ${item.title}`}
          onClick={onToggle}
        >
          {blocked ? "Undo" : "Don’t use"}
        </Button>
      )}
    </li>
  );
}

function MixGroup({
  heading,
  empty,
  children,
}: {
  heading: string;
  empty: boolean;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1">
      <h4 className="text-sm font-medium">{heading}</h4>
      {empty ? <p className="text-sm text-muted-foreground">Nothing yet.</p> : children}
    </div>
  );
}

/**
 * This week's mix for one row: the recent, favourite and older titles the AI web search draws on.
 * Fetched only once shown (`enabled`), because it is a live read of the person's Plex history.
 */
export function HistoryMixView({
  user,
  collectionId,
}: {
  user: User;
  collectionId: number;
}) {
  const query = useHistoryMix(user.id, collectionId, true);
  const block = useBlockSeed(user.id);
  const unblock = useUnblockSeed(user.id);
  const [blockedIds, setBlockedIds] = useState<Set<number> | null>(null);
  const [showAllRecent, setShowAllRecent] = useState(false);

  if (query.isPending) return <Skeleton className="h-24 w-full" />;
  if (query.isError) {
    return (
      <MutationAlert
        error={query.error}
        fallback="Couldn’t read this week’s mix. Check that Plex is reachable and try again."
        onRetry={() => void query.refetch()}
      />
    );
  }

  const mix = query.data;
  const blocked =
    blockedIds ?? new Set(blockedSeeds(user.prefs).map((seed) => seed.tmdb_id));
  const busy = block.isPending || unblock.isPending;
  const remember = (seeds: { tmdb_id: number }[]) =>
    setBlockedIds(new Set(seeds.map((seed) => seed.tmdb_id)));

  const toggle = (item: HistoryMixItem) => {
    if (item.tmdb_id === null) return;
    if (blocked.has(item.tmdb_id)) {
      unblock.mutate(item.tmdb_id, { onSuccess: (r) => remember(r.blocked_seeds) });
    } else {
      block.mutate(
        { tmdbId: item.tmdb_id, title: item.title, mediaType: item.media_type, year: item.year ?? undefined },
        { onSuccess: (r) => remember(r.blocked_seeds) },
      );
    }
  };

  const recent = showAllRecent ? mix.recent : mix.recent.slice(0, RECENT_SHOWN);
  const hidden = mix.recent.length - recent.length;
  const failed = block.isError ? block : unblock.isError ? unblock : null;

  const list = (items: HistoryMixItem[]) => (
    <ul className="divide-y">
      {items.map((item) => (
        <MixItem
          key={`${item.media_type}-${item.tmdb_id ?? item.title}`}
          item={item}
          blocked={item.tmdb_id !== null && blocked.has(item.tmdb_id)}
          busy={busy}
          onToggle={() => toggle(item)}
        />
      ))}
    </ul>
  );

  return (
    <div className="space-y-3 rounded-md border bg-raised/40 p-3">
      <MixGroup heading="Recent" empty={mix.recent.length === 0}>
        <ul className="text-sm">
          {recent.map((item) => (
            <li key={`${item.media_type}-${item.tmdb_id ?? item.title}`} className="py-0.5">
              {itemLabel(item)}
            </li>
          ))}
        </ul>
        {hidden > 0 && (
          <Button type="button" variant="ghost" size="sm" className="-ml-3" onClick={() => setShowAllRecent(true)}>
            +{hidden} more
          </Button>
        )}
      </MixGroup>
      {mix.favourite_count > 0 && (
        <MixGroup heading="Favourites" empty={mix.favourites.length === 0}>
          {list(mix.favourites)}
        </MixGroup>
      )}
      {mix.older_count > 0 && (
        <MixGroup heading="Older" empty={mix.older.length === 0}>
          {list(mix.older)}
        </MixGroup>
      )}
      {failed && (
        <p role="alert" className="text-sm text-destructive-text">
          {apiErrorMessage(failed.error, "Couldn’t change that. Try again.")}
        </p>
      )}
    </div>
  );
}
