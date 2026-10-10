import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  Cog,
  Database,
  Eraser,
  Lock,
  RefreshCw,
  ShieldCheck,
  Users as UsersIcon,
} from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router";

import { CronPicker } from "@/components/cron-picker";
import { ActivityFeed } from "@/components/jobs/activity-feed";
import { BackupPanel } from "@/components/jobs/backup-panel";
import {
  BackupLive,
  DeleteOrphansDialog,
  DriftLive,
  PrivacySyncLive,
  PruneLive,
  SyncUsersLive,
  SyncWatchedLive,
} from "@/components/jobs/job-live";
import { JobRow } from "@/components/jobs/job-row";
import { MutationAlert } from "@/components/mutation-alert";
import { NightlyRunCard } from "@/components/jobs/nightly-run-card";
import { RowSchedules } from "@/components/jobs/row-schedules";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { driftFindings } from "@/lib/job-drift";
import { queuedReason, useRunActive, useWritesPlex } from "@/lib/job-activity";
import {
  queryKeys,
  useBuiltInScheduleLabel,
  useSchedule,
  useSettings,
  useSaveSettings,
} from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type {
  JobCatalogEntry,
  SyncFinishedEvent,
  SyncProgressEvent,
} from "@/lib/types";

/** Names for rows that render before the catalogue arrives, so a row is never blank-titled. The
 *  server's catalogue is authoritative and replaces these the moment it lands. */
const PENDING_LABELS: Record<string, string> = {
  "sync.history": "Sync watch history",
  "sync.users": "Sync people from Plex",
  "sync.check": "Check and fix rows on Plex",
  "privacy.sync": "Privacy sync",
  "backup.take": "Back up the database",
  "maintenance.prune": "Clear out old records",
  "themes.rotate": "Pick new row themes",
};

/**
 * What each job you can press CHANGES, on the line rather than a paragraph deep.
 *
 * "Run now" mixes jobs with wildly different consequences: one only reads, two only touch
 * Shortlist's own database, and one can delete a collection off your Plex server for good. They all
 * looked identical until you expanded them. A job with no tag here changes nothing outside
 * Shortlist's own records — which is a claim each of those three descriptions makes too.
 */
const EFFECT_TAGS: Record<
  string,
  { text: string; title: string; note?: string }
> = {
  "sync.users": {
    text: "Changes Plex",
    title:
      "Adds and removes people, and takes the rows of anyone who has lost access off your Plex server.",
  },
  "sync.check": {
    text: "Can delete",
    title:
      "Writes corrections to Plex, and after you have read the preview and pressed Fix it can delete a collection for good. Nothing is deleted before you press Fix.",
    // The reassurance was in the `title` above and nowhere else, so the only visible thing on the
    // row was a red "Can delete" — hover-only on a desktop, unreachable on a phone, next to a
    // button people then did not dare press. The scary half must never outlive the calming half.
    note: "Nothing is deleted until you read the preview and press Fix.",
  },
  // Plain text, not a warning: it only ever removes Shortlist's own old records, never anything on Plex.
  "maintenance.prune": {
    text: "Deletes data",
    title: "Removes run history and change-log entries older than the retention limits in Settings.",
  },
  "privacy.sync": {
    text: "Changes Plex",
    title:
      "Rewrites every account's share filter. It only ever hides things, so it can only make your server more private.",
  },
};

function pendingEntry(kind: string): JobCatalogEntry {
  return {
    kind,
    label: PENDING_LABELS[kind] ?? kind,
    description: "",
    manual: true,
    schedule_optional: false,
    schedule_setting: "",
    trigger: "",
    scheduled: false,
    next_run: null,
    last: null,
    total: 0,
    queued: 0,
    running: 0,
    failed: 0,
  };
}

function GroupHeading({
  title,
  hint,
  note,
}: {
  title: string;
  hint?: string;
  /** A second line, for something too important to be a trailing aside on the heading. */
  note?: string;
}) {
  return (
    <div className="space-y-1 px-1">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h2 className="text-sm font-semibold text-foreground">
          {title}
        </h2>
        {hint && (
          <span className="text-sm text-muted-foreground">{hint}</span>
        )}
      </div>
      {note && <p className="text-sm text-muted-foreground">{note}</p>}
    </div>
  );
}

// --- panels: settings and reference, revealed when a row is expanded -----------------------------

/**
 * The frequency editor, for any job that owns a cron.
 *
 * Generic on purpose: anything with a `schedule_setting` gets this, so no job can show when it
 * next runs without letting you change it.
 */
function SchedulePanel({ entry }: { entry: JobCatalogEntry }) {
  const queryClient = useQueryClient();
  const settings = useSettings();
  const schedule = useSchedule();
  const saveSettings = useSaveSettings();
  // Blank means the built-in default below, not off, so the blank preset itself says what it runs at.
  const blankLabel = useBuiltInScheduleLabel(entry.kind);
  if (!entry.schedule_setting) return null;

  // `null` is not "blank": it deletes the stored cron, which is the only way to say "use the
  // built-in default" for a schedule where a stored blank means OFF (server: scheduler._OFF_ABLE).
  const save = (cron: string | null) =>
    saveSettings.mutate(
      { [entry.schedule_setting]: cron },
      {
        onSuccess: () => {
          // Both, and both matter: the panel reads the EFFECTIVE cron from /api/schedule, and the
          // row's next-run comes from the catalogue. Without these a schedule you just changed goes
          // on showing the old one until the catalogue's idle poll comes round.
          queryClient.invalidateQueries({ queryKey: queryKeys.schedule });
          queryClient.invalidateQueries({ queryKey: queryKeys.jobsCatalog });
        },
      },
    );

  // A schedule that can be switched OFF has to be edited against the cron it ACTUALLY runs on, not
  // the stored setting: for those, a stored blank means off while an absent row means "the built-in
  // default", and `GET /api/settings` folds the default in, so the two are the same "" there. Reading
  // that made the off switch appear only once some other frequency had been saved — on a default
  // install there was no off control at all, for the one job the code goes out of its way to let you
  // switch off (scheduler._register_sync_check).
  if (entry.schedule_optional) {
    // A failed fetch is not a slow one. Treating them alike left a permanent skeleton where the off
    // switch belongs — the only control on this page that could get stuck with no way back.
    if (schedule.isError) {
      return (
        <p role="alert" className="text-sm text-destructive-text">
          Couldn&rsquo;t load this schedule, so it can&rsquo;t be changed here
          right now.{" "}
          <button
            type="button"
            onClick={() => schedule.refetch()}
            className="underline underline-offset-4"
          >
            Try again
          </button>
        </p>
      );
    }
    if (!schedule.data) return <Skeleton className="h-8 w-72" />;
    const job = schedule.data.jobs.find((j) => j.kind === entry.kind);
    return (
      <div className="space-y-2">
        <CronPicker
          value={job?.cron ?? ""}
          onChange={save}
          blankLabel="Off"
          // The way back from Off. The cron comes from the server so the SPA never holds a second
          // copy of it, and picking it saves `null` rather than a cron, so the setting goes back to
          // inheriting the built-in time instead of pinning today's value of it.
          defaultCron={job?.default_cron ?? ""}
          onRestoreDefault={() => save(null)}
        />
        <p className="text-xs text-muted-foreground">
          Off means it never runs on its own &mdash; the button on this row
          still works whenever you press it.
        </p>
      </div>
    );
  }

  const stored =
    ((settings.data ?? {})[entry.schedule_setting] as string | undefined) ?? "";

  return <CronPicker value={stored} onChange={save} blankLabel={blankLabel} />;
}

/**
 * The job catalogue: every kind, its health, and its next run.
 *
 * Fast while something is in flight, SLOW when idle — never `false`. Stopping altogether meant a job
 * queued anywhere else (the scheduler firing, another tab, a row edit) never showed up here: the page
 * that exists to show background work was the one place that didn't know it had started, while the
 * header's activity icon — which always polls — did.
 */
function useJobCatalog() {
  return useQuery({
    queryKey: queryKeys.jobsCatalog,
    queryFn: api.getJobCatalog,
    refetchInterval: (query) =>
      (query.state.data ?? []).some((e) => e.running + e.queued > 0)
        ? 3_000
        : 15_000,
  });
}

/**
 * The Job history tab of the Activity page: every job run across every kind, newest first.
 *
 * It was the "Activity" half of a "Jobs | Activity" switch inside the Jobs tab — a second row of tabs
 * under the page's own. Its own tab answers the same question ("what has my server been doing?") with
 * one level of navigation, and the Jobs tab's "N failed" badge still lands here filtered to failures
 * (`?tab=history&filter=failed`).
 */
export function JobHistoryPanel() {
  const [searchParams] = useSearchParams();
  const catalog = useJobCatalog();
  return (
    <div className="space-y-3">
      {catalog.isError && (
        <MutationAlert
          error={catalog.error}
          fallback="Couldn't load the job names, so jobs show by their internal kind."
          onRetry={() => catalog.refetch()}
        />
      )}
      <ActivityFeed
        catalog={catalog.data ?? []}
        initialFilter={searchParams.get("filter") === "failed" ? "failed" : "all"}
      />
    </div>
  );
}

/**
 * The Jobs tab of the Activity page (it was the Jobs page until the two merged): every piece of
 * background maintenance Shortlist does, as a compact list — name, health, next run, and the button.
 * A job is a LINE, and its description, settings and history open on demand.
 *
 * The previous design gave each job a full card with its paragraph and its controls permanently on
 * screen: nine of those was ~1800px of scrolling, four of them jobs you can never start, and no
 * cross-job feed at all. That feed is the Job history tab now.
 */
export function JobsPanel() {
  const queryClient = useQueryClient();
  const [, setSearchParams] = useSearchParams();
  // `?tab=` belongs to the Activity page this panel sits in; the badges below move it to the history.
  const showHistory = (filter?: "failed") =>
    setSearchParams(filter ? { tab: "history", filter } : { tab: "history" }, { replace: true });

  const catalog = useJobCatalog();

  // One EventSource for the whole page (rules/frontend.md); the two sync jobs read the slice of
  // `sync.*` events carrying their own `kind`. `null` = idle, so no bar shows until a run starts.
  const [watchedProgress, setWatchedProgress] =
    useState<SyncProgressEvent | null>(null);
  const [usersProgress, setUsersProgress] = useState<SyncProgressEvent | null>(
    null,
  );
  // The watched sync's POST returns the moment it's queued (202 "started"), so its OUTCOME only
  // arrives on the bus. The users sync's POST awaits the whole thing, so its mutation result is
  // authoritative — the bus just drives its live bar.
  const [watchedResult, setWatchedResult] = useState<SyncFinishedEvent | null>(
    null,
  );
  // Gate on the one irreversible half of "Fix N rows" — see the button's own comment.
  const [confirmDelete, setConfirmDelete] = useState(false);

  useSSE({
    onSyncProgress: (event) => {
      if (event.kind === "watched") {
        setWatchedProgress(event);
        setWatchedResult(null); // a fresh run supersedes the last result line
      } else if (event.kind === "users") {
        setUsersProgress(event);
      }
    },
    onSyncFinished: (event) => {
      if (event.kind === "watched") {
        setWatchedProgress(null);
        setWatchedResult(event);
        // The watched sync refreshes each user's picks-watched — repaint the users list once done.
        queryClient.invalidateQueries({ queryKey: queryKeys.users });
        queryClient.invalidateQueries({ queryKey: queryKeys.jobs });
      } else if (event.kind === "users") {
        setUsersProgress(null);
      }
      // `credited` is ignored on purpose: it is the live credit pass, not a sync, and this page's
      // success line says "watch history is up to date", which that pass never did.
    },
  });

  const settings = useSettings();
  const saveSettings = useSaveSettings();
  const watchCron = ((settings.data ?? {})["sync.watch_cron"] as string) ?? "";
  const usersCron = ((settings.data ?? {})["sync.users_cron"] as string) ?? "";
  const watchBlankLabel = useBuiltInScheduleLabel("sync.history");
  const usersBlankLabel = useBuiltInScheduleLabel("sync.users");

  // Every job's action lives here rather than inside its panel: the button is on the ROW, which
  // stays visible when the panel is closed.
  const invalidateJobs = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.jobs });
  const syncWatched = useMutation({
    mutationFn: api.syncWatched,
    onSettled: invalidateJobs,
  });
  const syncUsers = useMutation({
    mutationFn: api.syncUsers,
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.users });
      invalidateJobs();
    },
  });
  // Preview first, then act. Converge only ever REMOVES visibility so a live pass is never unsafe,
  // but "press a button, we silently rewrite every library" is the wrong default.
  const driftPreview = useMutation({
    mutationFn: () => api.runJob("sync.check", { dry_run: true }),
    onSettled: invalidateJobs,
  });
  const driftFix = useMutation({
    // `confirmed` is what authorises the DELETE half. The scheduled pass and the preview both omit
    // it, so an unattended run demotes and reports what it would remove — only this button, pressed
    // after reading that, can destroy a collection.
    mutationFn: () => api.runJob("sync.check", { confirmed: true }),
    onSuccess: () => driftPreview.reset(),
    onSettled: invalidateJobs,
  });
  const privacySync = useMutation({
    // background: returns as soon as the job is queued and the row polls for the outcome, so a slow
    // job can't end in a proxy timeout that reads as a failure.
    mutationFn: () => api.runJob("privacy.sync", {}, true),
    onSettled: invalidateJobs,
  });
  const backupNow = useMutation({
    mutationFn: api.createBackup,
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.backups });
      invalidateJobs();
    },
  });
  // Foreground, unlike the privacy pass: this one deletes rows from Shortlist's own SQLite and comes
  // back in well under a second, so waiting for the real "pruned N runs" line beats a toast that
  // only says it started. It takes no arguments — the retention limits come from settings.
  const pruneNow = useMutation({
    mutationFn: () => api.runJob("maintenance.prune", {}),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      invalidateJobs();
    },
  });

  const entries = catalog.data ?? [];
  const byKind = Object.fromEntries(entries.map((e) => [e.kind, e]));
  const entryFor = (kind: string) => byKind[kind] ?? pendingEntry(kind);
  // Automatic jobs are queued by the mutation that knows their target (disabling someone, renaming
  // a row). No button may start them — but a cleanup that ran out of retries must still be visible.
  const automatic = entries.filter((e) => !e.manual);
  // Split by whether the job has ever HAPPENED. On a fresh install none of them has, so this
  // section was nine rows of internal machinery ("Credit a finished playback", "Undo a watch-history
  // copy") each stamped "never run" — the job registry rendered as a to-do list the owner cannot
  // act on, and "never run" fifteen times down one page. The ones that have run, or are running,
  // are the ones worth a row; the rest go behind a disclosure so they are still findable when
  // something does go wrong with one.
  const hasHappened = (entry: JobCatalogEntry) =>
    entry.last !== null || entry.running + entry.queued + entry.failed > 0;
  const automaticSeen = automatic.filter(hasHappened);
  const automaticIdle = automatic.filter((entry) => !hasHappened(entry));
  const totals = entries.reduce(
    (acc, e) => ({
      running: acc.running + e.running,
      queued: acc.queued + e.queued,
      failed: acc.failed + e.failed,
    }),
    { running: 0, queued: 0, failed: 0 },
  );
  const active = totals.running + totals.queued;
  // "2 running · 1 queued" rather than "3 in flight": queued and running are different situations —
  // one is working, the other is waiting on the writer lock or a busy queue — and lumping them hides
  // which. Clicking goes to the Job history tab, where you can see WHICH jobs they are.
  const activeLabel = [
    totals.running > 0 ? `${totals.running} running` : null,
    totals.queued > 0 ? `${totals.queued} queued` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  // The same "why is this queued" explanation the activity popover gives, on the row's status
  // chip — the maintainer looks at both, and "Queued" alone explains nothing.
  const writesPlexFor = useWritesPlex();
  const runActive = useRunActive(totals.queued > 0);
  const queuedTitleFor = (kind: string): string | undefined =>
    entryFor(kind).queued > 0
      ? queuedReason(writesPlexFor(kind), runActive).title
      : undefined;

  const watchedRunning = watchedProgress !== null;
  const { orphans } = driftFindings(driftPreview.data);

  return (
    <div className="space-y-5">
      <DeleteOrphansDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        orphans={orphans}
        driftFix={driftFix}
      />
      <NightlyRunCard />

      {/* Health at a glance, and only when there is something to say — a permanent "0 failed"
          teaches you to stop reading it. */}
      <div className="flex flex-wrap items-center justify-end gap-2 empty:hidden">
          {active > 0 && (
            <button
              type="button"
              onClick={() => showHistory()}
              className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              title="See which jobs are running"
            >
              <span className="size-1.5 animate-pulse rounded-full bg-primary" />
              {activeLabel}
            </button>
          )}
          {/* A BUTTON, like its "N running" sibling above. It was a bare span: the badge that
              reports the healthy state was clickable and the one reporting the problem was a dead
              end. Worse, nothing else on this tab pointed at the failures either — a job's status
              chip shows only its LAST attempt, so eight failed `privacy.sync` runs sat behind a
              green tick and the badge was the sole evidence they existed. */}
          {totals.failed > 0 && (
            <button
              type="button"
              onClick={() => showHistory("failed")}
              className="inline-flex items-center gap-1.5 rounded-full bg-destructive/10 px-2.5 py-1 text-xs font-medium text-destructive-text hover:bg-destructive/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              title="See what failed, and why"
            >
              {/* The counts are every failure the history still holds, not a recent window: on a
                  server whose last failure was weeks ago a bare "12 failed" read as an emergency. */}
              {totals.failed} failed in job history
            </button>
          )}
          {active === 0 && totals.failed === 0 && entries.length > 0 && (
            <span className="text-xs text-muted-foreground">
              All jobs healthy
            </span>
          )}
        </div>

      {catalog.isError && (
        <MutationAlert
          error={catalog.error}
          fallback="Couldn't load the job list."
          onRetry={() => catalog.refetch()}
        />
      )}

      {catalog.isPending ? (
        <Skeleton className="h-72 w-full" />
      ) : (
        <div className="space-y-5">
          <section className="space-y-2">
            {/* The page subtitle already says "each on its own timer", and the tag note is a
                legend for something that appears three rows down — three explainers stacked
                between the page title and the first job. One is enough here; the tags carry
                their own meaning (a job with no tag changes nothing on Plex). */}
            <GroupHeading
              title="Upkeep"
              hint="each on its own timer — open one to change when it runs"
            />
            <div className="overflow-hidden rounded-md border">
              <JobRow
                first
                entry={entryFor("sync.users")}
                queuedTitle={queuedTitleFor("sync.users")}
                tag={EFFECT_TAGS["sync.users"]}
                icon={UsersIcon}
                action={{
                  label: "Run",
                  run: () => syncUsers.mutate(),
                  pending: syncUsers.isPending,
                }}
                live={
                  syncUsers.isPending || syncUsers.isError || syncUsers.data ? (
                    <SyncUsersLive syncUsers={syncUsers} usersProgress={usersProgress} />
                  ) : null
                }
                panel={
                  <CronPicker
                    value={usersCron}
                    blankLabel={usersBlankLabel}
                    onChange={(cron) =>
                      saveSettings.mutate({ "sync.users_cron": cron })
                    }
                  />
                }
              />

              <JobRow
                entry={entryFor("sync.history")}
                queuedTitle={queuedTitleFor("sync.history")}
                icon={RefreshCw}
                action={{
                  label: "Run",
                  run: () => syncWatched.mutate(),
                  pending: syncWatched.isPending || watchedRunning,
                }}
                live={
                  watchedRunning ||
                  syncWatched.isError ||
                  watchedResult ||
                  syncWatched.isSuccess ? (
                    <SyncWatchedLive
                      syncWatched={syncWatched}
                      watchedProgress={watchedProgress}
                      watchedResult={watchedResult}
                    />
                  ) : null
                }
                panel={
                  <CronPicker
                    value={watchCron}
                    blankLabel={watchBlankLabel}
                    onChange={(cron) =>
                      saveSettings.mutate({ "sync.watch_cron": cron })
                    }
                  />
                }
              />

              <JobRow
                entry={entryFor("sync.check")}
                queuedTitle={queuedTitleFor("sync.check")}
                tag={EFFECT_TAGS["sync.check"]}
                icon={ShieldCheck}
                panel={<SchedulePanel entry={entryFor("sync.check")} />}
                action={{
                  // Not "Check for drift": "drift" is our word for it, not anyone else's, and the
                  // button has to read as the safe half of a two-step — this one only looks.
                  label: "Run",
                  run: () => driftPreview.mutate(),
                  pending: driftPreview.isPending,
                }}
                // The preview's verdict is live, NOT panel: a result you have to expand a row to
                // read is a result you won't read, and the Fix button hangs off it.
                live={
                  driftPreview.isError ||
                  driftFix.isError ||
                  driftPreview.data ||
                  driftFix.data ? (
                    <DriftLive
                      driftPreview={driftPreview}
                      driftFix={driftFix}
                      onConfirmDelete={() => setConfirmDelete(true)}
                    />
                  ) : null
                }
              />

              <JobRow
                entry={entryFor("privacy.sync")}
                queuedTitle={queuedTitleFor("privacy.sync")}
                tag={EFFECT_TAGS["privacy.sync"]}
                icon={Lock}
                panel={<SchedulePanel entry={entryFor("privacy.sync")} />}
                action={{
                  label: "Run",
                  run: () => privacySync.mutate(),
                  pending: privacySync.isPending,
                }}
                live={
                  privacySync.isError ? (
                    <PrivacySyncLive privacySync={privacySync} />
                  ) : null
                }
              />

              <JobRow
                entry={entryFor("backup.take")}
                queuedTitle={queuedTitleFor("backup.take")}
                icon={Database}
                action={{
                  label: "Run",
                  run: () => backupNow.mutate(),
                  pending: backupNow.isPending,
                }}
                live={
                  backupNow.isError || backupNow.isSuccess ? (
                    <BackupLive backupNow={backupNow} />
                  ) : null
                }
                panel={<BackupPanel />}
              />

              {/* The retention pass. It was `manual: true` — so the "Automatic" group filtered it
                  out — and it was not one of the hardcoded rows here either, which left it in the
                  page totals with no row anywhere. A prune that failed showed "1 failed" in the
                  header and there was nothing to click, nothing to read, and no way to retry. */}
              <JobRow
                entry={entryFor("maintenance.prune")}
                queuedTitle={queuedTitleFor("maintenance.prune")}
                tag={EFFECT_TAGS["maintenance.prune"]}
                icon={Eraser}
                action={{
                  label: "Run",
                  run: () => pruneNow.mutate(),
                  pending: pruneNow.isPending,
                }}
                live={
                  pruneNow.isError || pruneNow.data ? (
                    <PruneLive pruneNow={pruneNow} />
                  ) : null
                }
                panel={<SchedulePanel entry={entryFor("maintenance.prune")} />}
              />
            </div>
          </section>

          {/* Row schedules were the ONLY thing the separate Timeline tab showed that this list didn't
              — every job already carries its own next-run. Listing them here makes Jobs the single
              answer to "what runs on a timer", instead of two pages that each held half of it. */}
          <RowSchedules />

          {automatic.length > 0 && (
            <section className="space-y-2">
              <GroupHeading
                title="Automatic"
                hint="queued for you when something changes"
                note={
                  automaticSeen.length === 0
                    ? "Nothing has needed one of these yet. They queue themselves when you change something — there is nothing to do here."
                    : undefined
                }
              />
              {automaticSeen.length > 0 && (
                <div className="overflow-hidden rounded-md border">
                  {automaticSeen.map((entry, index) => (
                    <JobRow
                      key={entry.kind}
                      first={index === 0}
                      entry={entry}
                      queuedTitle={queuedTitleFor(entry.kind)}
                      icon={Cog}
                    />
                  ))}
                </div>
              )}
              {/* Behind a disclosure, not deleted: when one of these DOES misbehave, its
                  description and history are the only explanation of what it was for. Shut by
                  default because a list of jobs that have never happened is not news. */}
              {automaticIdle.length > 0 && (
                <details className="group rounded-md border">
                  <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground [&::-webkit-details-marker]:hidden">
                    <ChevronRight
                      className="size-3.5 shrink-0 transition-transform group-open:rotate-90"
                      aria-hidden="true"
                    />
                    {automaticIdle.length === 1
                      ? "1 job that hasn’t needed to run"
                      : `${automaticIdle.length} jobs that haven’t needed to run`}
                  </summary>
                  <div className="border-t">
                    {automaticIdle.map((entry, index) => (
                      <JobRow
                        key={entry.kind}
                        first={index === 0}
                        entry={entry}
                        queuedTitle={queuedTitleFor(entry.kind)}
                        icon={Cog}
                      />
                    ))}
                  </div>
                </details>
              )}
            </section>
          )}
        </div>
      )}
    </div>
  );
}
