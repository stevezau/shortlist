import { useMutation } from "@tanstack/react-query";
import {
  ArrowRight,
  Check,
  CircleSlash,
  Loader2,
  PartyPopper,
  Play,
  TriangleAlert,
} from "lucide-react";
import { useState } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api";
import { useUsers } from "@/lib/queries";
import { RUN_STAGES, STAGE_LABELS } from "@/lib/run-stages";
import { useSSE } from "@/lib/sse";
import type { RunUserStageEvent, User } from "@/lib/types";
import { cn } from "@/lib/utils";

import type { StepProps } from "./step-props";

/** What each stage's counts mean, phrased for humans ("113 history · 40 seeds"). */
function countsLine(counts: Record<string, number>): string {
  const entries = Object.entries(counts).filter(
    ([name]) => name !== "position",
  );
  if (entries.length === 0) return "";
  return entries.map(([name, value]) => `${value} ${name}`).join(" · ");
}

interface UserProgress {
  stage: string;
  counts: Record<string, number>;
  reason?: string | null;
}

function StageTrail({ stage }: { stage: string }) {
  const activeIndex = RUN_STAGES.indexOf(stage as (typeof RUN_STAGES)[number]);
  const done = stage === "done";
  return (
    <div className="flex items-center gap-1" aria-hidden="true">
      {RUN_STAGES.map((name, i) => (
        <span
          key={name}
          title={STAGE_LABELS[name]}
          className={cn(
            "h-1.5 w-6 rounded-full transition-colors",
            done || i < activeIndex
              ? "bg-success"
              : i === activeIndex
                ? "animate-pulse bg-primary"
                : "bg-muted",
          )}
        />
      ))}
    </div>
  );
}

function ProgressCard({
  user,
  progress,
  runFinished,
}: {
  user: User;
  progress: UserProgress | undefined;
  runFinished: boolean;
}) {
  const stage = progress?.stage;
  const terminal = stage === "done" || stage === "error" || stage === "skipped";
  const active = !!progress && stage !== "queued" && !terminal && !runFinished;

  let detail: string;
  if (!progress || stage === "queued") {
    const position = progress?.counts.position;
    detail = runFinished
      ? "done"
      : `queued${position ? ` — #${position} in line` : ""} · rows build one user at a time`;
  } else if (stage === "done") {
    const picks = progress.counts.picks ?? 0;
    const seconds = progress.counts.seconds;
    detail = `row built — ${picks} picks${seconds ? ` in ${seconds}s` : ""}`;
  } else if (stage === "skipped") {
    // The engine says WHY (no per-person row enabled, not in an audience, muted…). The old copy
    // hardcoded one of those reasons and stated it as fact for all of them (issue #3).
    detail = progress?.reason
      ? `skipped — ${progress.reason.charAt(0).toLowerCase()}${progress.reason.slice(1)}`
      : "skipped — no row was due for them in this run";
  } else if (stage === "error") {
    detail =
      "failed — the rest of the run continues; detail is on the Runs page";
  } else {
    const line = countsLine(progress.counts);
    detail = `${STAGE_LABELS[stage ?? ""] ?? stage}${line ? ` — ${line}` : ""}`;
  }

  return (
    <Card>
      <CardContent className="flex items-center justify-between gap-3 p-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <p className="font-medium">{user.username}</p>
            {active && <StageTrail stage={stage ?? ""} />}
          </div>
          <p
            // The line is truncated to keep the card one row tall, so the full text — a skip reason
            // is a whole sentence — stays reachable on hover.
            title={detail}
            className={cn(
              "truncate text-sm",
              stage === "error" ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {detail}
          </p>
        </div>
        {stage === "done" && (
          <Check className="h-4 w-4 shrink-0 text-success" aria-hidden="true" />
        )}
        {stage === "skipped" && (
          <CircleSlash
            className="h-4 w-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
        )}
        {stage === "error" && (
          <TriangleAlert
            className="h-4 w-4 shrink-0 text-destructive"
            aria-hidden="true"
          />
        )}
        {active && (
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
 * (design doc §3 step 7). Every user card walks the pipeline stages live
 * (queued → history → candidates → curating → delivering → done), and the
 * owner can leave at any point — the run keeps going server-side.
 */
export function StepFirstRun({ complete }: StepProps) {
  const usersQuery = useUsers();
  const [progress, setProgress] = useState<Record<string, UserProgress>>({});
  const [finishedStatus, setFinishedStatus] = useState<string | null>(null);
  const [finishedError, setFinishedError] = useState<string | null>(null);

  useSSE({
    onRunUserStage: (event: RunUserStageEvent) =>
      setProgress((current) => ({
        ...current,
        [event.user]: {
          stage: event.stage,
          counts: event.counts,
          reason: event.reason ?? null,
        },
      })),
    onRunFinished: (event) => {
      setFinishedStatus(event.status);
      setFinishedError(event.error ?? null);
    },
  });

  const run = useMutation({
    mutationFn: () => api.startRun({}),
    onMutate: () => {
      setProgress({});
      setFinishedStatus(null);
      setFinishedError(null);
    },
  });

  const started = run.isSuccess;
  const finished = finishedStatus !== null;
  const failed = finished && finishedStatus !== "ok";

  return (
    <div className="space-y-6">
      {!started && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            This builds a real row for every enabled user — history → candidates
            → curating → collection → privacy sync — and you get to watch every
            stage live. Each row is delivered hidden and only shown to the
            person it&rsquo;s for.
          </p>
          <div className="flex flex-wrap items-center gap-3">
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
              Build my rows
            </Button>
            {/* Finishing without a run is fine — nothing needs the first run to have happened.
                The nightly schedule builds rows anyway, and "Build my rows" waits on the Runs page. */}
            <Button
              variant="ghost"
              onClick={() => void complete()}
              disabled={run.isPending}
            >
              Skip for now — I&rsquo;ll run it later
            </Button>
          </div>
          {run.isError && (
            <p role="alert" className="text-sm text-destructive">
              {apiErrorMessage(
                run.error,
                "The run could not start. Check the server log and try again.",
              )}
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
            const allUsersDone =
              enabled.length > 0 &&
              enabled.every((user) => {
                const p = progress[user.slug] ?? progress[user.username];
                return (
                  p &&
                  (p.stage === "done" ||
                    p.stage === "error" ||
                    p.stage === "skipped")
                );
              });
            return (
              <div className="space-y-3">
                {enabled.map((user) => (
                  <ProgressCard
                    key={user.id}
                    user={user}
                    progress={progress[user.slug] ?? progress[user.username]}
                    runFinished={finished}
                  />
                ))}
                {enabled.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    No users are enabled — go back to step 5 and switch someone
                    on.
                  </p>
                )}
                {!finished && allUsersDone && (
                  <p
                    className="inline-flex items-center gap-2 text-sm text-muted-foreground"
                    role="status"
                  >
                    <Loader2
                      className="h-3.5 w-3.5 animate-spin"
                      aria-hidden="true"
                    />
                    All rows built — finishing up: merging privacy filters
                    across every account, then promoting the rows onto Home.
                  </p>
                )}
                {!finished && (
                  <div className="flex items-center gap-3 pt-2">
                    {/* Nothing after this step needs the run to have finished — it keeps
                        going server-side, and the Runs page streams the same progress. */}
                    <Button variant="ghost" onClick={() => void complete()}>
                      <ArrowRight aria-hidden="true" />
                      Continue setup — keep building in the background
                    </Button>
                    <span className="text-xs text-muted-foreground">
                      the run keeps going; follow it on the Runs page
                    </span>
                  </div>
                )}
              </div>
            );
          }}
        </QueryBoundary>
      )}

      {finished && (
        <div
          role="status"
          className={
            failed
              ? "space-y-3 rounded-lg border border-destructive/50 bg-destructive/10 p-5"
              : "space-y-3 rounded-lg border border-success/50 bg-success/10 p-5"
          }
        >
          <p
            className={
              failed
                ? "inline-flex items-center gap-2 text-lg font-semibold text-destructive"
                : "inline-flex items-center gap-2 text-lg font-semibold text-success"
            }
          >
            {failed ? (
              <TriangleAlert className="h-5 w-5" aria-hidden="true" />
            ) : (
              <PartyPopper className="h-5 w-5" aria-hidden="true" />
            )}
            {failed
              ? "The run failed — no rows were built"
              : "Rows are live on Plex"}
          </p>
          <Badge variant={finishedStatus === "ok" ? "success" : "destructive"}>
            run {finishedStatus}
          </Badge>
          <p className="text-sm text-muted-foreground">
            {failed
              ? "Nothing was half-applied — fix the cause and run it again. Full per-user detail is on the Runs page."
              : 'Tell your users to look for their new row tonight — something like: "Your Plex now has a private Picked-for-You row, built from what you actually watch. Enjoy."'}
          </p>
          {failed && finishedError && (
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                Something went wrong on this run — copy the details below when
                reporting it:
              </p>
              <p className="rounded-md bg-destructive/10 px-3 py-2 font-mono text-xs text-destructive">
                {finishedError}
              </p>
            </div>
          )}
          {!failed && (
            <p className="text-sm text-muted-foreground">
              Want more? In Settings you can add extra recommendation sources
              (Trakt, AI web search), auto-request missing titles via
              Sonarr/Radarr, and add more rows.
            </p>
          )}
          <Button onClick={() => void complete()}>
            {failed ? "Finish setup anyway" : "Finish setup"}
          </Button>
        </div>
      )}
    </div>
  );
}
