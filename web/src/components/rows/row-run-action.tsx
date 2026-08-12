import { Play } from "lucide-react";
import { useNavigate } from "react-router";

import { MutationAlert } from "@/components/mutation-alert";
import { Button } from "@/components/ui/button";
import { useStartRun } from "@/lib/queries";
import type { Collection } from "@/lib/types";

/**
 * Rebuild ONE row, now — the same scoped run the Runs page's "Run selected rows…" dialog starts,
 * offered where the row is.
 *
 * `collection_ids` has always been part of `POST /api/runs`; the only way to reach it was a dialog
 * on another page, where you re-picked the row you were already looking at.
 *
 * It lands you on the run. A row build takes minutes and its one useful next screen is the live
 * run — staying put would leave a button that visibly did nothing while the work happened
 * elsewhere.
 *
 * Scoping is safe by construction: the run narrows only the DELIVERY loop, while the leak-safe
 * privacy pass still covers every account (`run_service.start_run`), so rows this run doesn't
 * build stay hidden from everyone they aren't for.
 */
export function RowRunAction({
  collection,
  variant = "ghost",
  size = "sm",
}: {
  collection: Collection;
  variant?: "ghost" | "outline";
  size?: "sm" | "default";
}) {
  const navigate = useNavigate();
  const startRun = useStartRun();

  return (
    <>
      <Button
        variant={variant}
        size={size}
        className={variant === "ghost" ? "text-muted-foreground" : undefined}
        loading={startRun.isPending}
        // A disabled row is skipped by the run and then REMOVED from Plex, so "Run" on one would
        // either look broken or do the opposite of what the word promises. Say which it is.
        disabled={!collection.enabled}
        title={
          collection.enabled
            ? "Rebuild just this row now, for everyone who gets it"
            : "This row is off — turn it on first, or a run only takes it off Plex"
        }
        aria-label={`Run ${collection.name} now`}
        onClick={() =>
          startRun.mutate(
            { collection_ids: [collection.id] },
            {
              onSuccess: (created) => navigate(`/runs/${created.run_id}`),
            },
          )
        }
      >
        {!startRun.isPending && <Play aria-hidden="true" />}
        {/* "Run now", not "Run". It sits directly beside "Runs" — the row's history — and one
            letter is not enough to tell a Plex write apart from a link to a list, especially when
            the misclick that costs something is the shorter word. Matches the Jobs page's "Run now"
            group and the Runs page's "Run all rows now". */}
        Run now
      </Button>
      {/* `w-full` so it takes its own line: both callers lay their controls out with `flex-wrap`,
          and an alert sharing a line with the buttons squeezes them off the row on a phone. */}
      {startRun.isError && (
        <MutationAlert
          className="w-full"
          error={startRun.error}
          fallback="Couldn’t start a run for this row. Check the server log and try again."
        />
      )}
    </>
  );
}
