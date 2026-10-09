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
import { useNavigate } from "react-router";

import { ErrorState, QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { TitlePoster } from "@/components/title-poster";
import { runOutcome } from "@/lib/run-outcome";
import type { RunFinishedEvent } from "@/lib/types";
import { api, apiErrorMessage } from "@/lib/api";
import { useRun, useUsers } from "@/lib/queries";
import { describeCounts, RUN_STAGES, STAGE_LABELS } from "@/lib/run-stages";
import { useSSE } from "@/lib/sse";
import type { Pick, RunUserStageEvent, User } from "@/lib/types";
import { cn } from "@/lib/utils";

import type { StepProps } from "./step-props";

/** What each stage's counts mean, phrased for humans. The queue position is left out. */
function countsLine(counts: Record<string, number | string>): string {
  const { position: _position, ...rest } = counts;
  return describeCounts(rest);
}

interface UserProgress {
  stage: string;
  counts: Record<string, number | string>;
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
  picks,
}: {
  user: User;
  progress: UserProgress | undefined;
  runFinished: boolean;
  picks: Pick[];
}) {
  const stage = progress?.stage;
  const terminal = stage === "done" || stage === "cold_start" || stage === "error" || stage === "skipped";
  const active = !!progress && stage !== "queued" && !terminal && !runFinished;

  let detail: string;
  if (!progress || stage === "queued") {
    const position = progress?.counts.position;
    detail = runFinished
      ? "not recorded for this person — check the run details"
      : `queued${position ? ` — #${position} in line` : ""} · rows build one user at a time`;
  } else if (stage === "done" || stage === "cold_start") {
    const picks = progress.counts.picks ?? 0;
    const seconds = progress.counts.seconds;
    detail = `${stage === "cold_start" ? `popular-title picks — ${picks} found` : `row built — ${picks} picks`}${seconds ? ` in ${seconds}s` : ""}`;
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
      <CardContent className="space-y-3 p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <p className="font-medium">{user.display_name || user.username}</p>
            {active && <StageTrail stage={stage ?? ""} />}
          </div>
          <p
            title={detail}
            className={cn(
              "text-sm break-words",
              stage === "error" ? "text-destructive-text" : "text-muted-foreground",
            )}
          >
            {detail}
          </p>
        </div>
        {(stage === "done" || stage === "cold_start") && (
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
            className="h-4 w-4 shrink-0 text-destructive-text"
            aria-hidden="true"
          />
        )}
        {active && (
          <Loader2
            className="h-4 w-4 shrink-0 animate-spin text-primary"
            aria-hidden="true"
          />
        )}
      </div>
      {/* The owner never otherwise sees a pick during setup: a strip of what each person got is the
          proof the run did something. Posters come from the owner's own Plex via the pick proxy. */}
      {runFinished && picks.length > 0 && (
        <div className="grid grid-cols-6 gap-2" data-testid={`picks-${user.slug}`}>
          {picks.slice(0, 6).map((pick) => (
            <TitlePoster
              key={`${pick.rank}-${pick.rating_key}`}
              ratingKey={pick.rating_key}
              className="h-auto w-full sm:h-auto sm:w-full aspect-[2/3]"
            />
          ))}
        </div>
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
export function StepFirstRun({ data, update, complete }: StepProps) {
  const navigate = useNavigate();
  const usersQuery = useUsers();
  const willGetRow = (usersQuery.data ?? []).filter((user) => user.enabled);
  const [progress, setProgress] = useState<Record<string, UserProgress>>({});
  const [eventStatus, setFinishedStatus] = useState<
    RunFinishedEvent["status"] | null
  >(null);
  const [eventError, setFinishedError] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: () => api.startRun({}),
    onMutate: () => {
      setProgress({});
      setFinishedStatus(null);
      setFinishedError(null);
    },
    onSuccess: (result) => update({ first_run_id: result.run_id }),
  });
  const runId = run.data?.run_id ?? data.first_run_id;
  const savedRun = useRun(runId ?? 0, runId !== undefined);
  const storedStatus = savedRun.data?.status;
  const finishedStatus = eventStatus ?? (storedStatus === "ok" || storedStatus === "error" || storedStatus === "aborted" ? storedStatus : null);
  const finishedError = eventError ?? savedRun.data?.error;
  const recordedProgress: Record<string, UserProgress> = {};
  const recordedPicks: Record<string, Pick[]> = {};
  for (const person of savedRun.data?.users ?? []) {
    recordedPicks[person.slug] = person.picks;
    recordedProgress[person.slug] = {
      stage: person.status === "ok" ? "done" : person.status,
      counts: { picks: person.picks.length, ...(person.duration_ms ? { seconds: Math.round(person.duration_ms / 1000) } : {}) },
      reason: person.reason,
    };
  }
  // Stored terminal results are authoritative after a reconnect; live stages fill the in-flight gaps.
  const userProgress = (user: User) => {
    const recorded = recordedProgress[user.slug];
    return recorded && ["done", "cold_start", "error", "skipped"].includes(recorded.stage)
      ? recorded : progress[user.slug] ?? progress[user.username] ?? recorded;
  };

  useSSE({
    onRunUserStage: (event: RunUserStageEvent) => {
      if (event.run_id !== runId) return;
      setProgress((current) => ({ ...current, [event.user]: { stage: event.stage, counts: event.counts ?? {}, reason: event.reason ?? null } }));
    },
    onRunFinished: (event) => {
      if (event.run_id !== runId) return;
      setFinishedStatus(event.status);
      setFinishedError(event.error ?? null);
      void savedRun.refetch();
    },
  });

  const started = runId !== undefined;
  const finished = finishedStatus !== null;
  const outcome = finishedStatus === null ? null : runOutcome(finishedStatus);
  const failed = outcome === "failed";
  const stopped = outcome === "stopped";
  // Candidate picks and a completed stage do not prove delivery: a row can lack a usable name.
  // Use the recorded Plex diff, including retained titles, and stay neutral until it is available.
  const hasBuiltRows = [...(savedRun.data?.users ?? []), ...(savedRun.data?.shared_rows ?? [])].some(
    (result) => (result.diff?.added?.length ?? 0) + (result.diff?.kept?.length ?? 0) > 0,
  );

  return (
    <div className="space-y-6">
      {started && <p className="text-sm text-muted-foreground">Run #{runId} · progress is saved, so you can return to this step.</p>}
      {started && savedRun.isError && <ErrorState error={savedRun.error} onRetry={() => void savedRun.refetch()} />}
      {!started && (
        <div className="space-y-6">
          <p className="text-sm text-muted-foreground">
            This looks at what everyone has watched, finds titles they should
            enjoy, and adds a row to each person&rsquo;s Plex. You can watch it
            happen as rows are built. Shortlist applies sharing rules before promoting
            rows; the owner and parental-profile limitations still apply.
          </p>
          {/* No time estimate: nothing recorded before a first run says how long this server takes,
              and a number guessed from the people count would be invented. */}
          <Card>
            <CardContent className="space-y-2 p-4">
              <p className="text-sm font-medium">
                {usersQuery.data === undefined
                  ? "Checking who gets a row…"
                  : willGetRow.length === 0
                    ? "No one is switched on yet"
                    : `${willGetRow.length} ${willGetRow.length === 1 ? "person gets" : "people get"} a row`}
              </p>
              {willGetRow.length > 0 && (
                <p className="text-sm text-muted-foreground">
                  {willGetRow.map((user) => user.display_name || user.username).join(", ")}
                </p>
              )}
              {usersQuery.data !== undefined && willGetRow.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  Go back to step 5 and switch someone on, or skip this and add people later.
                </p>
              )}
            </CardContent>
          </Card>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
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
            <button
              type="button"
              className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline disabled:opacity-50"
              onClick={() => void complete()}
              disabled={run.isPending}
            >
              Skip for now — I&rsquo;ll run it later
            </button>
          </div>
          {run.isError && (
            <p role="alert" className="text-sm text-destructive-text">
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
                const p = userProgress(user);
                return (
                  p &&
                  (p.stage === "done" ||
                    p.stage === "cold_start" ||
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
                    progress={userProgress(user)}
                    runFinished={finished}
                    picks={recordedPicks[user.slug] ?? []}
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
                    All users processed — finishing up: hiding each person&rsquo;s
                    row from everyone else, then putting them on the Home
                    screen.
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
        <div role="status" className="space-y-4">
          <p
            className={
              failed
                ? "inline-flex items-center gap-2 text-lg font-semibold text-destructive-text"
                : stopped
                  ? "inline-flex items-center gap-2 text-lg font-semibold text-warning"
                  : "inline-flex items-center gap-2 text-lg font-semibold text-success"
            }
          >
            {failed || stopped ? (
              <TriangleAlert className="h-5 w-5" aria-hidden="true" />
            ) : (
              <PartyPopper className="h-5 w-5" aria-hidden="true" />
            )}
            {failed
              ? "The run needs attention"
              : stopped
                ? "Stopped — the rows built before you stopped it are live"
                : hasBuiltRows ? "Your rows are on Plex" : "First run complete"}
          </p>
          {/* "warning", not "destructive", for a stop: the owner did it on purpose. */}
          <Badge
            variant={
              finishedStatus === "ok"
                ? "success"
                : stopped
                  ? "warning"
                  : "destructive"
            }
          >
            run {finishedStatus}
          </Badge>
          <p className="text-sm text-muted-foreground">
            {failed
              ? "Check the per-person results before trying again. The Runs page keeps the full result and error details."
              : stopped
                ? "Everyone the run reached kept their row, and their privacy filters were applied. Run it again whenever you like — it picks up from where things are."
                : hasBuiltRows ? "Review each person’s result above. They will see their row on Home next time they open Plex. Skipped accounts may need a different setup before they can receive a row." : "No built rows were recorded for the people in this run. Review their results and check the full run details after finishing setup."}
          </p>
          {failed && finishedError && (
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                Something went wrong on this run — copy the details below when
                reporting it:
              </p>
              <p className="rounded-md bg-destructive/10 px-3 py-2 font-mono text-xs text-destructive-text">
                {finishedError}
              </p>
            </div>
          )}
          {!failed && hasBuiltRows && (
            <p className="text-sm text-muted-foreground">
              You will see everyone&rsquo;s rows in each library&rsquo;s Collections tab: Plex never
              filters the server owner.
            </p>
          )}
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3 border-t pt-6">
            <Button onClick={() => void complete()}>
              {failed ? "Finish setup anyway" : "Go to dashboard"}
            </Button>
            {!failed && (
              <>
                <button
                  type="button"
                  className="text-sm text-muted-foreground hover:text-foreground"
                  onClick={() => void complete().then(() => navigate("/rows/new"))}
                >
                  Add more rows
                </button>
                <button
                  type="button"
                  className="text-sm text-muted-foreground hover:text-foreground"
                  onClick={() => void complete().then(() => navigate("/settings#requests"))}
                >
                  Set up requests
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
