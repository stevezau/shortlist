import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Clock,
  Database,
  Download,
  RefreshCw,
  ShieldCheck,
  Users as UsersIcon,
  Wrench,
} from "lucide-react";
import { useState } from "react";

import { CronInput } from "@/components/cron-input";
import { JobsTable } from "@/components/jobs-table";
import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ProgressBar } from "@/components/ui/progress-bar";
import { api } from "@/lib/api";
import { timeAgo, timeUntil } from "@/lib/format";
import {
  queryKeys,
  useSettings,
  useSaveSettings,
  useSyncs,
} from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { SyncFinishedEvent, SyncProgressEvent } from "@/lib/types";

const SYNC_PRESETS = [
  { value: "", label: "Daily" },
  { value: "17 */12 * * *", label: "12h" },
  { value: "17 */6 * * *", label: "6h" },
  { value: "17 */4 * * *", label: "4h" },
];

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

/**
 * Tools — on-demand maintenance the owner runs by hand, distinct from the nightly schedule. Each
 * action here is a deliberate "reconcile now" for when something has drifted; none of them writes
 * to Plex. Every card handles its own pending / error / success states inline.
 */
export function ToolsPage() {
  const queryClient = useQueryClient();
  // One EventSource for the whole page (rules/frontend.md); the two sync cards read the slice of
  // `sync.*` events that carries their own `kind`. `null` = idle, so no bar shows until a run starts.
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
      // Clear the bar once the sync ends; the card's own success/error line takes over from here.
      if (event.kind === "watched") {
        setWatchedProgress(null);
        setWatchedResult(event);
        // The watched sync refreshes each user's picks-watched — repaint the users list once done.
        queryClient.invalidateQueries({ queryKey: queryKeys.users });
      } else {
        setUsersProgress(null);
      }
    },
  });

  const syncs = useSyncs();
  const settings = useSettings();
  const saveSettings = useSaveSettings();
  const watchCron = ((settings.data ?? {})["sync.watch_cron"] as string) ?? "";

  return (
    <div>
      <PageHeader
        icon={Wrench}
        title="Jobs"
        subtitle="Maintenance jobs you can run now, and what they did. Use these when something has drifted — a new user, or watched state that's out of sync — rather than waiting for the nightly run."
      />
      <div className="grid gap-4">
        <SyncHistoryCard
          progress={watchedProgress}
          result={watchedResult}
          lastSynced={syncs.data?.watched.last ?? null}
          nextRun={syncs.data?.watched.next ?? null}
          watchCron={watchCron}
          onCronChange={(cron) =>
            saveSettings.mutate(
              { "sync.watch_cron": cron },
              {
                onSuccess: () =>
                  queryClient.invalidateQueries({ queryKey: ["syncs"] }),
              },
            )
          }
        />
        <SyncUsersCard
          progress={usersProgress}
          lastSynced={syncs.data?.users.last ?? null}
          nextRun={syncs.data?.users.next ?? null}
          usersCron={((settings.data ?? {})["sync.users_cron"] as string) ?? ""}
          onCronChange={(cron) =>
            saveSettings.mutate(
              { "sync.users_cron": cron },
              {
                onSuccess: () =>
                  queryClient.invalidateQueries({ queryKey: ["syncs"] }),
              },
            )
          }
        />
        <SyncCheckCard />
        <BackupsCard />
      </div>

      {/* What running these actually did. On the same page as the buttons on purpose: "I pressed it,
          did it work?" is one question, and jobs retry themselves — so a failure that resolved on
          the second attempt has no other place to be seen. */}
      <JobsTable />
    </div>
  );
}

/** Re-read every user's complete watched set now, rather than waiting for the nightly sync. */
function SyncHistoryCard({
  progress,
  result,
  lastSynced,
  nextRun,
  watchCron,
  onCronChange,
}: {
  progress: SyncProgressEvent | null;
  result: SyncFinishedEvent | null;
  lastSynced: string | null;
  nextRun: string | null;
  watchCron: string;
  onCronChange: (cron: string) => void;
}) {
  const sync = useMutation({ mutationFn: api.syncWatched });
  // This POST returns 202 the moment the sync is QUEUED — the real outcome arrives on the bus as
  // `result`. So the bar is live while events flow, then the bus result (not the POST) is the truth.
  const running = progress !== null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <RefreshCw
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          Sync watch history now
        </CardTitle>
        <CardDescription>
          Re-read every user's complete watched set from Plex right now —
          including anything they've marked as watched. Use it when you want the
          effectiveness report refreshed straight away.
        </CardDescription>
        {(lastSynced || nextRun) && (
          <p className="flex items-center gap-3 pt-1 text-xs text-muted-foreground">
            <Clock className="size-3.5 shrink-0" aria-hidden="true" />
            {lastSynced && <span>Last synced {timeAgo(lastSynced)}</span>}
            {lastSynced && nextRun && <span aria-hidden="true">·</span>}
            {nextRun && <span>Next: {timeUntil(nextRun)}</span>}
          </p>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-start gap-3">
          <Button
            variant="outline"
            onClick={() => sync.mutate()}
            loading={sync.isPending || running}
          >
            <RefreshCw aria-hidden="true" />
            Sync history
          </Button>
          <CronPicker value={watchCron} onChange={onCronChange} />
        </div>
        {running && (
          <div className="flex flex-col gap-1.5">
            <ProgressBar
              done={progress.done}
              total={progress.total}
              label="Syncing watch history"
            />
            <p role="status" className="text-xs text-muted-foreground">
              {progress.total
                ? `Syncing ${progress.done ?? 0} of ${progress.total} ${progress.total === 1 ? "user" : "users"}…`
                : "Syncing…"}
            </p>
          </div>
        )}
        {sync.isError && (
          <MutationAlert
            error={sync.error}
            fallback="Couldn't start the sync. Check the Plex connection and try again."
            onRetry={() => sync.mutate()}
          />
        )}
        {!running && result?.ok === false && (
          <p role="alert" className="text-sm text-destructive">
            The sync couldn't finish
            {result.error ? ` (${result.error})` : ""}. Check the Plex
            connection and try again.
          </p>
        )}
        {!running && result?.ok && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            Synced {result.count ?? 0} {result.count === 1 ? "user" : "users"} —
            watch history is up to date and the effectiveness report reflects it
            now.
          </p>
        )}
        {/* No bus result yet (SSE not connected) but the POST was accepted — say it's running. */}
        {!running && !result && sync.isSuccess && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            Sync started — it runs in the background across every user. The
            effectiveness report updates on its own once it finishes.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** Re-pull the shared + Home users (and the owner) from plex.tv into the users table. */
function SyncUsersCard({
  progress,
  lastSynced,
  nextRun,
  usersCron,
  onCronChange,
}: {
  progress: SyncProgressEvent | null;
  lastSynced: string | null;
  nextRun: string | null;
  usersCron: string;
  onCronChange: (cron: string) => void;
}) {
  const queryClient = useQueryClient();
  const sync = useMutation({
    mutationFn: api.syncUsers,
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
  const result = sync.data;
  // This POST awaits the whole sync, so `sync.data` is the authoritative result. The bus events just
  // drive the live bar while it's in flight: an indeterminate "fetch" phase, then a "save" count.
  const running = sync.isPending;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UsersIcon
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          Sync users
        </CardTitle>
        <CardDescription>
          Re-pull everyone you share with — and yourself — from plex.tv and
          Tautulli (if connected). Refreshes usernames, display names/friendly
          names, and share status. Use it after inviting someone new so they
          show up in the user list without waiting for the next run.
        </CardDescription>
        {(lastSynced || nextRun) && (
          <p className="flex items-center gap-3 pt-1 text-xs text-muted-foreground">
            <Clock className="size-3.5 shrink-0" aria-hidden="true" />
            {lastSynced && <span>Last synced {timeAgo(lastSynced)}</span>}
            {lastSynced && nextRun && <span aria-hidden="true">·</span>}
            {nextRun && <span>Next: {timeUntil(nextRun)}</span>}
          </p>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-start gap-3">
          <Button
            variant="outline"
            onClick={() => sync.mutate()}
            loading={running}
          >
            <UsersIcon aria-hidden="true" />
            Sync users
          </Button>
          <CronPicker value={usersCron} onChange={onCronChange} />
        </div>
        {running && (
          <div className="flex flex-col gap-1.5">
            <ProgressBar
              done={progress?.phase === "save" ? progress.done : undefined}
              total={progress?.phase === "save" ? progress.total : undefined}
              label="Syncing users"
            />
            <p role="status" className="text-xs text-muted-foreground">
              {progress?.phase === "save" && progress.total
                ? `Saving ${progress.done ?? 0} of ${progress.total} ${progress.total === 1 ? "user" : "users"}…`
                : "Contacting plex.tv…"}
            </p>
          </div>
        )}
        {sync.isError && (
          <MutationAlert
            error={sync.error}
            fallback="Couldn't reach plex.tv to refresh the user list. Try again."
            onRetry={() => sync.mutate()}
          />
        )}
        {result && !running && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            {result.added > 0 || result.updated > 0
              ? `Synced ${result.total} ${result.total === 1 ? "user" : "users"} — ${result.added} added, ${result.updated} updated.`
              : `All ${result.total} ${result.total === 1 ? "user is" : "users are"} already up to date.`}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

const RETENTION_OPTIONS = [
  { value: 5, label: "5" },
  { value: 10, label: "10" },
  { value: 20, label: "20" },
  { value: 30, label: "30" },
];

function BackupsCard() {
  const queryClient = useQueryClient();
  const syncs = useSyncs();
  const settings = useSettings();
  const saveSettings = useSaveSettings();
  const backups = useQuery({
    queryKey: ["backups"],
    queryFn: api.getBackups,
  });
  const create = useMutation({
    mutationFn: api.createBackup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
  });
  const restore = useMutation({
    mutationFn: api.restoreBackup,
  });
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
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Database className="h-4 w-4" aria-hidden="true" />
          Backups
        </CardTitle>
        <CardDescription>
          A copy of Shortlist’s whole database, taken on the schedule below and
          again before every upgrade.
          {syncs.data?.backup?.next && (
            <span className="ml-1">
              Next: {timeUntil(syncs.data.backup.next)}
            </span>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5 rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
          <p>
            <span className="font-medium text-foreground">What:</span> settings,
            rows, people, run history — and each user’s original Plex share
            filters.
          </p>
          <p>
            <span className="font-medium text-foreground">Why:</span> those
            share filters are the only record of how sharing looked before
            Shortlist. Uninstall restores from them.
          </p>
          <p>
            Saved to <span className="font-mono text-xs">/config/backups</span>.{" "}
            <span className="font-mono text-xs">secret.key</span> isn’t included
            — keep a copy, or a restored backup can’t read your saved keys.
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
              options={RETENTION_OPTIONS.map((o) => ({
                value: String(o.value),
                label: o.label,
              }))}
            />
          </div>
        </div>

        <Button
          size="sm"
          variant="outline"
          loading={create.isPending}
          onClick={() => create.mutate()}
        >
          <Download className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
          Back up now
        </Button>

        {create.isError && (
          <MutationAlert error={create.error} fallback="Backup failed." />
        )}
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

        {/* Shown BEFORE the confirm, not after it. A restore is not a neutral rollback: the database
            is what decides who may see which rows, so restoring a copy from before a shared row's
            audience was narrowed puts the wider audience back — and the un-hiding happens on the next
            run, long after this screen is closed. */}
        {confirmRestore && (
          <p
            role="alert"
            className="rounded-md border border-warning/40 bg-warning/5 p-2 text-sm text-warning-foreground"
          >
            Restoring also puts back who could see which rows at the time of the backup. If you have
            narrowed a shared row&rsquo;s audience since then, those people will be able to see it
            again after the next run.
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
                            onClick={() => {
                              restore.mutate(b.name, {
                                onSuccess: () => setConfirmRestore(null),
                              });
                            }}
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
      </CardContent>
    </Card>
  );
}

/**
 * Sync check — the convergence pass, on demand.
 *
 * Things drift out of sync because a run only ever writes promotion flags for the people IN that
 * run: anyone paused, disabled, or caught by a run that died keeps whatever flags they last got.
 * The nightly run fixes it, but that can be a day away.
 */
function SyncCheckCard() {
  const queryClient = useQueryClient();
  // Preview first, then act. Converge only ever REMOVES visibility so a live pass is never unsafe,
  // but "press a button, we silently rewrite every library" is the wrong default — the operator
  // should see what would change before authorising it.
  const preview = useMutation({
    mutationFn: () => api.runJob("sync.check", { dry_run: true }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const fix = useMutation({
    mutationFn: () => api.runJob("sync.check"),
    onSuccess: () => preview.reset(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const drifted =
    preview.data?.status === "done" ? (preview.data.fixed ?? []) : [];
  const orphans =
    preview.data?.status === "done" ? (preview.data.orphans ?? []) : [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          Sync check
        </CardTitle>
        <CardDescription>
          Checks every row on Plex against what Shortlist intends, and fixes
          anything that drifted. Rows can fall out of step when a run
          doesn&rsquo;t finish, when the container restarts mid-write, or when
          someone was paused or disabled while their row was already live
          &mdash; a run only updates the people in that run, so everyone else
          keeps whatever they last had. This runs the same check without waiting
          for tonight.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap gap-3">
          <Button
            variant="outline"
            onClick={() => preview.mutate()}
            loading={preview.isPending}
          >
            <ShieldCheck aria-hidden="true" />
            Check for drift
          </Button>
          {drifted.length + orphans.length > 0 && (
            <Button onClick={() => fix.mutate()} loading={fix.isPending}>
              Fix {drifted.length + orphans.length} row
              {drifted.length + orphans.length === 1 ? "" : "s"}
            </Button>
          )}
        </div>
        {preview.isError && (
          <MutationAlert
            error={preview.error}
            fallback="Couldn’t run the sync check. Try again."
          />
        )}
        {fix.isError && (
          <MutationAlert
            error={fix.error}
            fallback="Couldn’t fix those rows. Try again."
          />
        )}
        {/* Deletions get their own callout, above the summary line. Folding them into the "N rows"
            count would hide the one irreversible action behind a number, in the very preview an
            operator reads to decide whether to run for real. */}
        {orphans.length > 0 && (
          <p className="rounded-md border border-dashed border-destructive/50 bg-destructive/5 p-3 text-sm text-muted-foreground">
            <strong className="text-foreground">
              This will delete {orphans.length} collection
              {orphans.length === 1 ? "" : "s"}
            </strong>{" "}
            &mdash; {orphans.join(", ")}. Shortlist no longer knows who they
            belong to, so hiding them would leave them in your Collections tab
            for ever. This cannot be undone.
          </p>
        )}
        {/* `status` matters: the queue skips a drain while a run is writing to Plex, which is
            exactly when someone presses this. That leaves the job `queued` with no result and no
            error — reporting "everything is in sync" for a check that never ran would be a lie. */}
        {preview.data &&
          !preview.data.error &&
          preview.data.status !== "done" && (
            <p className="text-sm text-muted-foreground">
              Waiting for the current run to finish — the check will run
              straight after, and appear below.
            </p>
          )}
        {preview.data &&
          !preview.data.error &&
          preview.data.status === "done" && (
            <p className="text-sm text-muted-foreground">
              {drifted.length === 0 && orphans.length === 0
                ? "Everything is in sync — nothing to fix."
                : `${drifted.length} row${drifted.length === 1 ? "" : "s"} drifted onto your Home screen: ${drifted.join(", ")}`}
            </p>
          )}
        {fix.data && !fix.data.error && (
          <p className="text-sm text-muted-foreground">{fix.data.detail}</p>
        )}
      </CardContent>
    </Card>
  );
}
