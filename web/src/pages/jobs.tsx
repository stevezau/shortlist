import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Cog,
  Database,
  Lock,
  RefreshCw,
  ShieldCheck,
  Users as UsersIcon,
  Wrench,
} from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router";

import { CronInput } from "@/components/cron-input";
import { ActivityFeed } from "@/components/jobs/activity-feed";
import { JobRow } from "@/components/jobs/job-row";
import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { RowSchedules } from "@/components/jobs/row-schedules";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { ProgressBar } from "@/components/ui/progress-bar";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { queryKeys, useSettings, useSaveSettings } from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type {
  JobCatalogEntry,
  SyncFinishedEvent,
  SyncProgressEvent,
} from "@/lib/types";

const SYNC_PRESETS = [
  { value: "", label: "Daily" },
  { value: "17 */12 * * *", label: "12h" },
  { value: "17 */6 * * *", label: "6h" },
  { value: "17 */4 * * *", label: "4h" },
];

const RETENTION_OPTIONS = ["5", "10", "20", "30"];

function CronPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (cron: string) => void;
}) {
  const matchesPreset = SYNC_PRESETS.some((p) => p.value === value);
  const [custom, setCustom] = useState(!matchesPreset && value !== "");

  return (
    <div className="flex flex-wrap items-start gap-2">
      <span className="pt-1.5 text-xs text-muted-foreground">Frequency:</span>
      <Segmented
        value={custom ? "__custom__" : value}
        onChange={(v) => {
          if (v === "__custom__") {
            setCustom(true);
          } else {
            setCustom(false);
            onChange(v);
          }
        }}
        options={[
          ...SYNC_PRESETS.map((p) => ({ value: p.value, label: p.label })),
          { value: "__custom__", label: "Custom" },
        ]}
      />
      {custom && <CronInput value={value} onChange={onChange} />}
    </div>
  );
}

/** Names for rows that render before the catalogue arrives, so a row is never blank-titled. The
 *  server's catalogue is authoritative and replaces these the moment it lands. */
const PENDING_LABELS: Record<string, string> = {
  "sync.history": "Sync watch history",
  "sync.users": "Sync people from Plex",
  "sync.check": "Sync check",
  "privacy.sync": "Privacy sync",
  "backup.take": "Back up the database",
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

function GroupHeading({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 px-1">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h2>
      {hint && <span className="text-xs text-muted-foreground">· {hint}</span>}
    </div>
  );
}

// --- panels: settings and reference, revealed when a row is expanded -----------------------------

function SyncCronPanel({
  value,
  onChange,
}: {
  value: string;
  onChange: (cron: string) => void;
}) {
  return <CronPicker value={value} onChange={onChange} />;
}

/**
 * The frequency editor, for any job that owns a cron.
 *
 * Generic on purpose. Panels used to be wired job by job, so the two that nobody wired — privacy sync
 * and the drift check — had no way to set a schedule at all: you could see when they would next run
 * and not change it. Anything with a `schedule_setting` now gets this.
 */
function SchedulePanel({ entry }: { entry: JobCatalogEntry }) {
  const settings = useSettings();
  const saveSettings = useSaveSettings();
  if (!entry.schedule_setting) return null;

  const stored =
    ((settings.data ?? {})[entry.schedule_setting] as string | undefined) ?? "";

  return (
    <div className="space-y-2">
      <CronPicker
        value={stored}
        onChange={(cron) =>
          saveSettings.mutate({ [entry.schedule_setting]: cron })
        }
      />
      {/* Only offered where blank genuinely means OFF. Everywhere else a blank cron falls back to the
          built-in default, so a "turn off" button there would be a lie. */}
      {entry.schedule_optional && stored !== "" && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs text-muted-foreground"
          disabled={saveSettings.isPending}
          onClick={() =>
            saveSettings.mutate({ [entry.schedule_setting]: "" })
          }
        >
          Turn this schedule off
        </Button>
      )}
    </div>
  );
}

/** Backups: what's in one, where it lives, how often, how many to keep, and the restore list. */
function BackupPanel() {
  const queryClient = useQueryClient();
  const settings = useSettings();
  const saveSettings = useSaveSettings();
  const backups = useQuery({ queryKey: ["backups"], queryFn: api.getBackups });
  const restore = useMutation({ mutationFn: api.restoreBackup });
  const [confirmRestore, setConfirmRestore] = useState<string | null>(null);

  const backupCron = ((settings.data ?? {})["backup.cron"] as string) ?? "";
  const backupMaxKeep =
    ((settings.data ?? {})["backup.max_keep"] as number) ?? 10;

  function formatSize(bytes: number) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5 rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
        <p>
          <span className="font-medium text-foreground">What:</span> settings,
          rows, people, run history — and each user&rsquo;s original Plex share
          filters.
        </p>
        <p>
          <span className="font-medium text-foreground">Why:</span> those share
          filters are the only record of how sharing looked before Shortlist.
          Uninstall restores from them.
        </p>
        <p>
          Saved to <span className="font-mono text-xs">/config/backups</span>.{" "}
          <span className="font-mono text-xs">secret.key</span> isn&rsquo;t
          included — keep a copy, or a restored backup can&rsquo;t read your
          saved keys.
        </p>
      </div>

      <div className="flex flex-wrap items-start gap-4">
        <CronPicker
          value={backupCron}
          onChange={(cron) =>
            saveSettings.mutate(
              { "backup.cron": cron },
              {
                onSuccess: () =>
                  queryClient.invalidateQueries({ queryKey: ["syncs"] }),
              },
            )
          }
        />
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Keep:</span>
          <Segmented
            value={String(backupMaxKeep)}
            onChange={(v) =>
              saveSettings.mutate(
                { "backup.max_keep": Number(v) },
                {
                  onSuccess: () => {
                    queryClient.invalidateQueries({ queryKey: ["syncs"] });
                    queryClient.invalidateQueries({
                      queryKey: queryKeys.settings,
                    });
                  },
                },
              )
            }
            options={RETENTION_OPTIONS.map((v) => ({ value: v, label: v }))}
          />
        </div>
      </div>

      {restore.isSuccess && (
        <div className="space-y-1.5">
          <p className="text-sm text-success">{restore.data.message}</p>
          {/* A restore is not a neutral rollback: the database decides who may see which rows, so
              restoring one from before an audience was narrowed puts the wider audience back. */}
          {restore.data.privacy_note && (
            <p
              role="alert"
              className="rounded-md border border-warning/40 bg-warning/5 p-2 text-sm text-warning-foreground"
            >
              {restore.data.privacy_note}
            </p>
          )}
        </div>
      )}
      {restore.isError && (
        <MutationAlert error={restore.error} fallback="Restore failed." />
      )}

      {/* Shown BEFORE the confirm, not after it — the un-hiding happens on the next run, long after
          this screen is closed. */}
      {confirmRestore && (
        <p
          role="alert"
          className="rounded-md border border-warning/40 bg-warning/5 p-2 text-sm text-warning-foreground"
        >
          Restoring also puts back who could see which rows at the time of the
          backup. If you have narrowed a shared row&rsquo;s audience since then,
          those people will be able to see it again after the next run.
        </p>
      )}

      {backups.data && backups.data.length > 0 && (
        <div className="max-h-48 overflow-y-auto rounded border">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-muted/80 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-1.5">Backup</th>
                <th className="px-3 py-1.5">Size</th>
                <th className="px-3 py-1.5">When</th>
                <th className="px-3 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {backups.data.map((b) => (
                <tr key={b.name} className="border-t">
                  <td className="px-3 py-1.5 font-mono text-xs">
                    {b.name.replace("shortlist_", "").replace(".db", "")}
                  </td>
                  <td className="px-3 py-1.5">{formatSize(b.size_bytes)}</td>
                  <td className="px-3 py-1.5">{timeAgo(b.created_at)}</td>
                  <td className="px-3 py-1.5 text-right">
                    {confirmRestore === b.name ? (
                      <span className="flex items-center justify-end gap-1">
                        <Button
                          size="sm"
                          variant="destructive"
                          className="h-6 px-2 text-xs"
                          loading={restore.isPending}
                          onClick={() =>
                            restore.mutate(b.name, {
                              onSuccess: () => setConfirmRestore(null),
                            })
                          }
                        >
                          Confirm
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-6 px-2 text-xs"
                          onClick={() => setConfirmRestore(null)}
                        >
                          Cancel
                        </Button>
                      </span>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 px-2 text-xs"
                        onClick={() => setConfirmRestore(b.name)}
                      >
                        Restore
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {backups.data && backups.data.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No backups yet. One will be created automatically tonight at 3 AM.
        </p>
      )}
    </div>
  );
}

// --- live slots: what is happening, or just happened, because you pressed the button -------------

function SyncBar({
  done,
  total,
  label,
  line,
}: {
  done?: number;
  total?: number;
  label: string;
  line: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <ProgressBar done={done} total={total} label={label} />
      <p role="status" className="text-xs text-muted-foreground">
        {line}
      </p>
    </div>
  );
}

function Succeeded({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-center gap-2 text-sm text-foreground">
      <CheckCircle2
        aria-hidden="true"
        className="size-4 shrink-0 text-emerald-600 dark:text-emerald-500"
      />
      {children}
    </p>
  );
}

/**
 * Jobs — every piece of background maintenance Shortlist does.
 *
 * Two areas, because they answer different questions. **Jobs** is a compact list: name, health,
 * next run, and the button — a job is a LINE, and its description, settings and history open on
 * demand. **Activity** is every run across every kind, newest first.
 *
 * The previous design gave each job a full card with its paragraph and its controls permanently on
 * screen: nine of those was ~1800px of scrolling, four of them jobs you can never start, and no
 * cross-job feed at all.
 */
type JobsView = "jobs" | "activity";

export function JobsPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab");
  // `timeline` still resolves — the tab is gone, but /schedule redirects to it and links exist.
  const view: JobsView = tab === "activity" ? "activity" : "jobs";

  const catalog = useQuery({
    queryKey: ["jobs", "catalog"],
    queryFn: api.getJobCatalog,
    // Fast while something is in flight, SLOW when idle — never `false`. Stopping altogether meant a
    // job queued anywhere else (the scheduler firing, another tab, a row edit) never showed up here:
    // the page that exists to show background work was the one place that didn't know it had started,
    // while the header's activity icon — which always polls — did.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((e) => e.running + e.queued > 0)
        ? 3_000
        : 15_000,
  });

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

  useSSE({
    onSyncProgress: (event) => {
      if (event.kind === "watched") {
        setWatchedProgress(event);
        setWatchedResult(null); // a fresh run supersedes the last result line
      } else {
        setUsersProgress(event);
      }
    },
    onSyncFinished: (event) => {
      if (event.kind === "watched") {
        setWatchedProgress(null);
        setWatchedResult(event);
        // The watched sync refreshes each user's picks-watched — repaint the users list once done.
        queryClient.invalidateQueries({ queryKey: queryKeys.users });
        queryClient.invalidateQueries({ queryKey: ["jobs"] });
      } else {
        setUsersProgress(null);
      }
    },
  });

  const settings = useSettings();
  const saveSettings = useSaveSettings();
  const watchCron = ((settings.data ?? {})["sync.watch_cron"] as string) ?? "";
  const usersCron = ((settings.data ?? {})["sync.users_cron"] as string) ?? "";

  // Every job's action lives here rather than inside its panel: the button is on the ROW, which
  // stays visible when the panel is closed.
  const invalidateJobs = () =>
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
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
      queryClient.invalidateQueries({ queryKey: ["backups"] });
      invalidateJobs();
    },
  });

  const entries = catalog.data ?? [];
  const byKind = Object.fromEntries(entries.map((e) => [e.kind, e]));
  const entryFor = (kind: string) => byKind[kind] ?? pendingEntry(kind);
  // Automatic jobs are queued by the mutation that knows their target (disabling someone, renaming
  // a row). No button may start them — but a cleanup that ran out of retries must still be visible.
  const automatic = entries.filter((e) => !e.manual);
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
  // which. Clicking goes to Activity, where you can see WHICH jobs they are.
  const activeLabel = [
    totals.running > 0 ? `${totals.running} running` : null,
    totals.queued > 0 ? `${totals.queued} queued` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const watchedRunning = watchedProgress !== null;
  const drifted =
    driftPreview.data?.status === "done" ? (driftPreview.data.fixed ?? []) : [];
  const orphans =
    driftPreview.data?.status === "done"
      ? (driftPreview.data.orphans ?? [])
      : [];

  return (
    <div className="space-y-5">
      <PageHeader
        icon={Wrench}
        title="Jobs"
        subtitle="Background maintenance Shortlist does for you. Run any of it now when something has drifted, rather than waiting for the nightly run."
      />

      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* Three views of one subject: what background work exists, when it runs, and what it did.
            "Timeline" was its own top-level Schedule page, which listed every job a second time and
            left each view unable to answer a whole question on its own. */}
        <Segmented<JobsView>
          ariaLabel="Jobs view"
          value={view}
          onChange={(next) =>
            setSearchParams(next === "jobs" ? {} : { tab: next }, {
              replace: true,
            })
          }
          options={[
            { value: "jobs", label: "Jobs" },
            { value: "activity", label: "Activity" },
          ]}
        />
        {/* Health at a glance, and only when there is something to say — a permanent "0 failed"
            teaches you to stop reading it. */}
        <div className="flex flex-wrap items-center gap-2">
          {active > 0 && (
            <button
              type="button"
              onClick={() => setSearchParams({ tab: "activity" }, { replace: true })}
              className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              title="See which jobs are running"
            >
              <span className="size-1.5 animate-pulse rounded-full bg-primary" />
              {activeLabel}
            </button>
          )}
          {totals.failed > 0 && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-destructive/10 px-2.5 py-1 text-xs font-medium text-destructive">
              {totals.failed} failed
            </span>
          )}
          {active === 0 && totals.failed === 0 && entries.length > 0 && (
            <span className="text-xs text-muted-foreground">
              All jobs healthy
            </span>
          )}
        </div>
      </div>

      {catalog.isError && (
        <MutationAlert
          error={catalog.error}
          fallback="Couldn't load the job list."
          onRetry={() => catalog.refetch()}
        />
      )}

      {view === "activity" ? (
        <ActivityFeed catalog={entries} />
      ) : catalog.isPending ? (
        <Skeleton className="h-72 w-full" />
      ) : (
        <div className="space-y-5">
          <section className="space-y-2">
            <GroupHeading title="Run now" hint="you start these" />
            <div className="overflow-hidden rounded-md border">
              <JobRow
                first
                entry={entryFor("sync.users")}
                icon={UsersIcon}
                action={{
                  label: "Run",
                  run: () => syncUsers.mutate(),
                  pending: syncUsers.isPending,
                }}
                live={
                  syncUsers.isPending || syncUsers.isError || syncUsers.data ? (
                  <div className="flex flex-col gap-3">
                    {syncUsers.isPending && (
                      <SyncBar
                        label="Syncing users"
                        done={
                          usersProgress?.phase === "save"
                            ? usersProgress.done
                            : undefined
                        }
                        total={
                          usersProgress?.phase === "save"
                            ? usersProgress.total
                            : undefined
                        }
                        line={
                          usersProgress?.phase === "save" && usersProgress.total
                            ? `Saving ${usersProgress.done ?? 0} of ${usersProgress.total} ${usersProgress.total === 1 ? "user" : "users"}…`
                            : "Contacting plex.tv…"
                        }
                      />
                    )}
                    {syncUsers.isError && (
                      <MutationAlert
                        error={syncUsers.error}
                        fallback="Couldn't reach plex.tv to refresh the user list. Try again."
                        onRetry={() => syncUsers.mutate()}
                      />
                    )}
                    {syncUsers.data && !syncUsers.isPending && (
                      <Succeeded>
                        {syncUsers.data.added > 0 || syncUsers.data.updated > 0
                          ? `Synced ${syncUsers.data.total} ${syncUsers.data.total === 1 ? "user" : "users"} — ${syncUsers.data.added} added, ${syncUsers.data.updated} updated.`
                          : `All ${syncUsers.data.total} ${syncUsers.data.total === 1 ? "user is" : "users are"} already up to date.`}
                      </Succeeded>
                    )}
                  </div>
                  ) : null
                }
                panel={
                  <SyncCronPanel
                    value={usersCron}
                    onChange={(cron) =>
                      saveSettings.mutate(
                        { "sync.users_cron": cron },
                        {
                          onSuccess: () =>
                            queryClient.invalidateQueries({
                              queryKey: ["syncs"],
                            }),
                        },
                      )
                    }
                  />
                }
              />

              <JobRow
                entry={entryFor("sync.history")}
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
                  <div className="flex flex-col gap-3">
                    {watchedRunning && (
                      <SyncBar
                        label="Syncing watch history"
                        done={watchedProgress.done}
                        total={watchedProgress.total}
                        line={
                          watchedProgress.total
                            ? `Syncing ${watchedProgress.done ?? 0} of ${watchedProgress.total} ${watchedProgress.total === 1 ? "user" : "users"}…`
                            : "Syncing…"
                        }
                      />
                    )}
                    {syncWatched.isError && (
                      <MutationAlert
                        error={syncWatched.error}
                        fallback="Couldn't start the sync. Check the Plex connection and try again."
                        onRetry={() => syncWatched.mutate()}
                      />
                    )}
                    {!watchedRunning && watchedResult?.ok === false && (
                      <p role="alert" className="text-sm text-destructive">
                        The sync couldn&rsquo;t finish
                        {watchedResult.error ? ` (${watchedResult.error})` : ""}
                        . Check the Plex connection and try again.
                      </p>
                    )}
                    {!watchedRunning && watchedResult?.ok && (
                      <Succeeded>
                        Synced {watchedResult.count ?? 0}{" "}
                        {watchedResult.count === 1 ? "user" : "users"} — watch
                        history is up to date and the effectiveness report
                        reflects it now.
                      </Succeeded>
                    )}
                    {/* No bus result yet (SSE not connected) but the POST was accepted. */}
                    {!watchedRunning &&
                      !watchedResult &&
                      syncWatched.isSuccess && (
                        <Succeeded>
                          Sync started — it runs in the background across every
                          user. The effectiveness report updates on its own once
                          it finishes.
                        </Succeeded>
                      )}
                  </div>
                  ) : null
                }
                panel={
                  <SyncCronPanel
                    value={watchCron}
                    onChange={(cron) =>
                      saveSettings.mutate(
                        { "sync.watch_cron": cron },
                        {
                          onSuccess: () =>
                            queryClient.invalidateQueries({
                              queryKey: ["syncs"],
                            }),
                        },
                      )
                    }
                  />
                }
              />

              <JobRow
                entry={entryFor("sync.check")}
                icon={ShieldCheck}
                panel={<SchedulePanel entry={entryFor("sync.check")} />}
                action={{
                  label: "Check for drift",
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
                  <div className="flex flex-col gap-3">
                    {driftPreview.isError && (
                      <MutationAlert
                        error={driftPreview.error}
                        fallback="Couldn't run the sync check. Try again."
                      />
                    )}
                    {driftFix.isError && (
                      <MutationAlert
                        error={driftFix.error}
                        fallback="Couldn't fix those rows. Try again."
                      />
                    )}
                    {/* Deletions get their own callout above the summary. Folding them into the "N
                        rows" count would hide the one irreversible action behind a number. */}
                    {orphans.length > 0 && (
                      <p className="rounded-md border border-dashed border-destructive/50 bg-destructive/5 p-3 text-sm text-muted-foreground">
                        <strong className="text-foreground">
                          This will delete {orphans.length} collection
                          {orphans.length === 1 ? "" : "s"}
                        </strong>{" "}
                        &mdash; {orphans.join(", ")}. Shortlist no longer knows
                        who they belong to, so hiding them would leave them in
                        your Collections tab for ever. This cannot be undone.
                      </p>
                    )}
                    {/* `status` matters: the queue skips a drain while a run is writing to Plex,
                        which is exactly when someone presses this. Reporting "everything is in
                        sync" for a check that never ran would be a lie. */}
                    {driftPreview.data &&
                      !driftPreview.data.error &&
                      driftPreview.data.status !== "done" && (
                        <p className="text-sm text-muted-foreground">
                          Waiting for the current run to finish — the check will
                          run straight after.
                        </p>
                      )}
                    {driftPreview.data &&
                      !driftPreview.data.error &&
                      driftPreview.data.status === "done" && (
                        <p className="text-sm text-muted-foreground">
                          {drifted.length === 0 && orphans.length === 0
                            ? "Everything is in sync — nothing to fix."
                            : `${drifted.length} row${drifted.length === 1 ? "" : "s"} drifted onto your Home screen: ${drifted.join(", ")}`}
                        </p>
                      )}
                    {drifted.length + orphans.length > 0 && (
                      <div>
                        <Button
                          size="sm"
                          loading={driftFix.isPending}
                          onClick={() => driftFix.mutate()}
                        >
                          Fix {drifted.length + orphans.length} row
                          {drifted.length + orphans.length === 1 ? "" : "s"}
                        </Button>
                      </div>
                    )}
                    {driftFix.data && !driftFix.data.error && (
                      <p className="text-sm text-muted-foreground">
                        {driftFix.data.detail}
                      </p>
                    )}
                  </div>
                  ) : null
                }
              />

              <JobRow
                entry={entryFor("privacy.sync")}
                icon={Lock}
                panel={<SchedulePanel entry={entryFor("privacy.sync")} />}
                action={{
                  label: "Run",
                  run: () => privacySync.mutate(),
                  pending: privacySync.isPending,
                }}
                live={
                  privacySync.isError ? (
                    <MutationAlert
                      error={privacySync.error}
                      fallback="Couldn't start that job. Try again."
                      onRetry={() => privacySync.mutate()}
                    />
                  ) : null
                }
              />

              <JobRow
                entry={entryFor("backup.take")}
                icon={Database}
                action={{
                  label: "Back up now",
                  run: () => backupNow.mutate(),
                  pending: backupNow.isPending,
                }}
                live={
                  backupNow.isError || backupNow.isSuccess ? (
                  <>
                    {backupNow.isError && (
                      <MutationAlert
                        error={backupNow.error}
                        fallback="Backup failed."
                      />
                    )}
                    {backupNow.isSuccess && !backupNow.isPending && (
                      <Succeeded>
                        Backed up as{" "}
                        <span className="font-mono text-xs">
                          {backupNow.data.name}
                        </span>
                        .
                      </Succeeded>
                    )}
                  </>
                  ) : null
                }
                panel={<BackupPanel />}
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
              />
              <div className="overflow-hidden rounded-md border">
                {automatic.map((entry, index) => (
                  <JobRow
                    key={entry.kind}
                    first={index === 0}
                    entry={entry}
                    icon={Cog}
                  />
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
