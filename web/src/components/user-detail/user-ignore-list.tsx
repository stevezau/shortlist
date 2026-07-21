import { Ban, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { useBlocked, useSetBlocked } from "@/lib/queries";
import type { BlockedTitle, User } from "@/lib/types";

/**
 * Per-person ignore list (issue #5).
 *
 * Two switches per title, because they answer different complaints: "stop suggesting this" and
 * "stop letting this inspire suggestions". Watching one Western — or a football match, or something
 * watched out of curiosity — shouldn't turn a whole row into more of the same.
 */
export function UserIgnoreList({ user }: { user: User }) {
  const blocked = useBlocked(user.id);
  const setBlocked = useSetBlocked(user.id);

  const unblock = (row: BlockedTitle) =>
    setBlocked.mutate({
      tmdb_id: row.tmdb_id,
      media_type: row.media_type,
      block_pick: false,
      block_seed: false,
    });

  const toggle = (row: BlockedTitle, field: "block_pick" | "block_seed") =>
    setBlocked.mutate({
      tmdb_id: row.tmdb_id,
      media_type: row.media_type,
      title: row.title,
      block_pick: field === "block_pick" ? !row.block_pick : row.block_pick,
      block_seed: field === "block_seed" ? !row.block_seed : row.block_seed,
    });

  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <p className="text-sm text-muted-foreground">
          Titles {user.display_name ?? user.username} never wants involved. Use
          the ignore buttons on any of their picks above to add one — this is
          where you undo it. Changes apply on their next run.
        </p>

        {blocked.isLoading && <Skeleton className="h-16 w-full" />}

        {blocked.isError && (
          <p role="alert" className="text-sm text-destructive">
            {apiErrorMessage(
              blocked.error,
              "Couldn’t load the ignore list. Try again.",
            )}
          </p>
        )}

        {blocked.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            Nothing ignored yet. Use <strong>Ignore</strong> on any pick to stop
            it being suggested, or to stop the title that inspired it shaping
            their recommendations.
          </p>
        )}

        {blocked.data && blocked.data.length > 0 && (
          <ul className="divide-y rounded-lg border">
            {blocked.data.map((row) => (
              <li
                key={row.id}
                className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm"
              >
                <Ban
                  className="h-4 w-4 shrink-0 text-muted-foreground"
                  aria-hidden="true"
                />
                <span className="min-w-0 flex-1 truncate font-medium">
                  {row.title || `TMDB ${row.tmdb_id}`}
                </span>
                <button
                  type="button"
                  onClick={() => toggle(row, "block_pick")}
                  title="Whether this title may be recommended to them"
                >
                  <Badge variant={row.block_pick ? "secondary" : "outline"}>
                    {row.block_pick ? "Never suggest" : "May be suggested"}
                  </Badge>
                </button>
                <button
                  type="button"
                  onClick={() => toggle(row, "block_seed")}
                  title="Whether this title may inspire recommendations for them"
                >
                  <Badge variant={row.block_seed ? "secondary" : "outline"}>
                    {row.block_seed ? "Never inspire" : "May inspire"}
                  </Badge>
                </button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => unblock(row)}
                  aria-label={`Stop ignoring ${row.title || row.tmdb_id}`}
                >
                  <X aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ul>
        )}

        {setBlocked.isError && (
          <p role="alert" className="text-sm text-destructive">
            {apiErrorMessage(
              setBlocked.error,
              "Couldn’t save that change. Try again.",
            )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
