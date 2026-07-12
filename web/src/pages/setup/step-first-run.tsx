import { useMutation } from "@tanstack/react-query";
import { Loader2, PartyPopper, Play } from "lucide-react";
import { useState } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useUsers } from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { RunUserStageEvent, User } from "@/lib/types";

import type { StepProps } from "./step-props";

interface UserProgress {
  stage: string;
  counts: Record<string, number>;
}

function countsLine(counts: Record<string, number>): string {
  const entries = Object.entries(counts);
  if (entries.length === 0) return "";
  return entries.map(([name, value]) => `${value} ${name}`).join(" · ");
}

function ProgressCard({
  user,
  progress,
  finished,
}: {
  user: User;
  progress: UserProgress | undefined;
  finished: boolean;
}) {
  return (
    <Card>
      <CardContent className="flex items-center justify-between gap-3 p-4">
        <div>
          <p className="font-medium">{user.username}</p>
          <p className="text-sm text-muted-foreground">
            {progress
              ? `${progress.stage}${countsLine(progress.counts) ? ` — ${countsLine(progress.counts)}` : ""}`
              : finished
                ? "done"
                : "waiting…"}
          </p>
        </div>
        {progress && !finished && (
          <Loader2
            className="h-4 w-4 shrink-0 animate-spin text-primary"
            aria-hidden="true"
          />
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Step 7 — fire the first real run and stream per-user progress via SSE
 * (design doc §3 step 7). On finish: success panel + complete the wizard.
 */
export function StepFirstRun({ data, complete }: StepProps) {
  const usersQuery = useUsers();
  const [progress, setProgress] = useState<Record<string, UserProgress>>({});
  const [finishedStatus, setFinishedStatus] = useState<string | null>(null);

  useSSE({
    onRunUserStage: (event: RunUserStageEvent) =>
      setProgress((current) => ({
        ...current,
        [event.user]: { stage: event.stage, counts: event.counts },
      })),
    onRunFinished: (event) => setFinishedStatus(event.status),
  });

  // Without a passing Privacy Check the server refuses real writes (fail-closed), so a
  // skipper's "first run" can only ever be a dry run. Say so, and do that instead.
  const dryRunOnly = Boolean(data.privacy_skipped) && !data.privacy_passed;

  const run = useMutation({
    mutationFn: () => api.startRun({ dry_run: dryRunOnly }),
    onMutate: () => {
      setProgress({});
      setFinishedStatus(null);
    },
  });

  const started = run.isSuccess;
  const finished = finishedStatus !== null;

  return (
    <div className="space-y-6">
      {!started && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {dryRunOnly
              ? "You skipped the Privacy Check, so Rowarr will not write to Plex — it never does until a check passes. This is a dry run: you'll see exactly what it would build for each user. Run the Privacy Check from Settings whenever you're ready, then build the rows for real."
              : "This builds a real row for every enabled user — history → candidates → curating → collection → privacy sync — and you get to watch every stage live."}
          </p>
          <Button
            size="lg"
            onClick={() => run.mutate()}
            disabled={run.isPending}
          >
            {run.isPending ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : (
              <Play aria-hidden="true" />
            )}
            {dryRunOnly ? "Preview my rows (dry run)" : "Build my rows"}
          </Button>
          {run.isError && (
            <p role="alert" className="text-sm text-destructive">
              {run.error instanceof ApiError
                ? run.error.message
                : "The run could not start. Check the server log and try again."}
            </p>
          )}
        </div>
      )}

      {started && (
        <QueryBoundary
          query={usersQuery}
          skeleton={<Skeleton className="h-48 w-full" />}
        >
          {(users) => {
            const enabled = users.filter((user) => user.enabled);
            return (
              <div className="space-y-3">
                {enabled.map((user) => (
                  <ProgressCard
                    key={user.id}
                    user={user}
                    progress={progress[user.slug] ?? progress[user.username]}
                    finished={finished}
                  />
                ))}
                {enabled.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    No users are enabled — go back to step 5 and switch someone
                    on.
                  </p>
                )}
              </div>
            );
          }}
        </QueryBoundary>
      )}

      {finished && (
        <div
          role="status"
          className="space-y-3 rounded-lg border border-success/50 bg-success/10 p-5"
        >
          <p className="inline-flex items-center gap-2 text-lg font-semibold text-success">
            <PartyPopper className="h-5 w-5" aria-hidden="true" />
            Rows are live on Plex
          </p>
          <Badge variant={finishedStatus === "ok" ? "success" : "secondary"}>
            run {finishedStatus}
          </Badge>
          <p className="text-sm text-muted-foreground">
            Tell your users to look for their new row tonight — something like:
            "Your Plex now has a private Picked-for-You row, built from what you
            actually watch. Enjoy."
          </p>
          <Button onClick={() => void complete()}>Finish setup</Button>
        </div>
      )}
    </div>
  );
}
