import { useId, useState } from "react";

import { ErrorState } from "@/components/query-boundary";
import { PickList } from "@/components/pick-list";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { SELECT_CLASS } from "@/components/rows/seasons/select-class";
import { useRun, useStartRun } from "@/lib/queries";
import type { Collection, RunDetail, User } from "@/lib/types";

function personName(user: User): string {
  return user.display_name || user.username;
}

/**
 * Try an AI row for one person (#138): a scoped dry run of just this row, shown as the picks and the
 * reasons a real run would write. It reads the SAVED row and list, and writes nothing to Plex.
 */
export function AiTryIt({
  collection,
  users,
  unsaved,
}: {
  /** The saved row; null while a new row is being added. */
  collection: Collection | null;
  users: User[];
  /** The editor has changes that aren't saved, which a dry run can't see: it runs the saved row. */
  unsaved: boolean;
}) {
  const selectId = useId();
  const [personId, setPersonId] = useState<number | null>(null);
  const [runId, setRunId] = useState<number | null>(null);
  const start = useStartRun();
  const run = useRun(runId ?? 0, runId !== null);

  if (collection === null) {
    return (
      <p className="text-sm text-muted-foreground">
        Add the row first. Then you can run it for one person here, to see what it would pick and why, without
        writing anything to Plex.
      </p>
    );
  }
  if (users.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No one to try it for yet. People appear here once Shortlist has read them from Plex.
      </p>
    );
  }

  const person = users.find((user) => user.id === personId) ?? users[0]!;
  const go = () => {
    setRunId(null);
    start.mutate(
      { dry_run: true, user_ids: [person.id], collection_ids: [collection.id] },
      { onSuccess: (created) => setRunId(created.run_id) },
    );
  };
  const waiting = start.isPending || (runId !== null && (run.isPending || !run.data?.finished_at));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-2">
          <Label htmlFor={selectId}>Try it for</Label>
          <select
            id={selectId}
            className={SELECT_CLASS}
            value={person.id}
            onChange={(event) => setPersonId(Number(event.target.value))}
          >
            {users.map((user) => (
              <option key={user.id} value={user.id}>
                {personName(user)}
              </option>
            ))}
          </select>
        </div>
        <Button type="button" variant="outline" onClick={go} loading={waiting} disabled={unsaved}>
          Try it
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        A practice run for one person. Nothing is written to Plex.
      </p>
      {unsaved && (
        <p className="text-sm text-warning">
          Try it runs the row as it was last saved, and you have changes that aren’t saved. Save the row first to try
          them.
        </p>
      )}

      {start.isError && (
        <ErrorState error={start.error} onRetry={go} />
      )}
      {runId !== null && run.isError && <ErrorState error={run.error} onRetry={() => void run.refetch()} />}
      {waiting && !start.isError && (
        <div role="status" aria-label={`Running it for ${personName(person)}`} className="space-y-2">
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}
      {run.data?.finished_at && !waiting && <Result run={run.data} person={person} />}
    </div>
  );
}

function Result({ run, person }: { run: RunDetail; person: User }) {
  const result = run.users.find((user) => user.slug === person.slug);
  const name = personName(person);
  if (!result) {
    return (
      <p className="text-sm text-muted-foreground">
        The run finished without a result for {name}. Check that they are switched on and in this row’s audience.
      </p>
    );
  }
  if (result.error) {
    return (
      <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">
        <p>
          It couldn’t be tried for {name}. Nothing was written to Plex. Try again in a moment; if it keeps failing,
          the Runs page shows what happened.
        </p>
      </div>
    );
  }
  if (result.picks.length === 0) {
    return (
      <div className="space-y-1 rounded-lg border border-dashed p-4 text-sm">
        <p className="font-medium">It picked nothing for {name}.</p>
        <p className="text-muted-foreground">
          {result.reason ?? "Nothing in the list is on your server and unwatched by them."} Widen the list, or ask
          the AI to change it.
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">What {name} would get</p>
      <PickList picks={result.picks} collapseAfter={10} />
    </div>
  );
}
