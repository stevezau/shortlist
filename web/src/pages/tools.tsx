import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Cog,
  Database,
  Download,
  Lock,
  Play,
  RefreshCw,
  ShieldCheck,
  Users as UsersIcon,
  Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

import { CronInput } from "@/components/cron-input";
import { JobCard } from "@/components/jobs/job-card";
import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { ProgressBar } from "@/components/ui/progress-bar";
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

/** Names for the cards that render before the catalogue arrives, so a card is never blank-titled.
 *  The server's catalogue is authoritative and replaces these as soon as it lands. */
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

/** A stat in the strip above the job list. */
function Tally({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "destructive" | "primary";
}) {
  return (
    <div className="rounded-lg border px-3 py-2">
      <p
        className={`text-xl font-semibold tabular-nums ${
          tone === "destructive"
            ? "text-destructive"
            : tone === "primary"
              ? "text-primary"
              : ""
        }`}
      >
        {value}
      </p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}

/**
 * Jobs — every piece of background maintenance Shortlist does, one card each.
 *
 * Organised BY JOB rather than by chronology. The page used to be four hand-written cards with
 * "run now" buttons, followed by one flat table of the last 25 job rows across every kind mixed
 * together — so "did the thing I just pressed work?" and "has the roster sync been failing?" both
 * meant scanning that table for the right rows. Now each job owns its status, its schedule, its
 * controls and its own history, and the four with bespoke controls pass them into the same card.
 */
export function ToolsPage() {
  const queryClient = useQueryClient();
  const catalog = useQuery({
    queryKey: ["jobs", "catalog"],
    queryFn: api.getJobCatalog,
    // Poll only while something is in flight, then stop — a job started here finishes without a
    // reload, but an idle page isn't hitting the API forever.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((e) => e.running + e.queued > 0)
        ? 3_000
        : false,
  });
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

  const settings = useSettings();
  const saveSettings = useSaveSettings();
  const watchCron = ((settings.data ?? {})["sync.watch_cron"] as string) ?? "";

  const entries = catalog.data ?? [];
  const byKind = Object.fromEntries(entries.map((e) => [e.kind, e]));
  // Automatic jobs are queued by a mutation that knows its target (disabling someone, renaming a
  // row). They get no button, but they very much need a status and a history — a cleanup that
  // exhausted its retries is invisible otherwise.
  const automatic = entries.filter((e) => !e.manual);
  const totals = entries.reduce(
    (acc, e) => ({
      total: acc.total + e.total,
      active: acc.active + e.running + e.queued,
      failed: acc.failed + e.failed,
    }),
    { total: 0, active: 0, failed: 0 },
  );

  /**
   * Renders a job's bespoke controls inside its catalogue card.
   *
   * ALWAYS a JobCard, even before the catalogue lands — falling back to the bare controls would
   * change the element type at this position the moment the request settled, and React unmounts on
   * a type change. That threw away whatever the controls were holding: a sync you had just started,
   * a drift preview you were reading. The catalogue also refetches while a job is in flight, so it
   * was not only a first-paint problem.
   */
  const shell = (kind: string, icon: LucideIcon, controls: React.ReactNode) => (
    <JobCard
      entry={byKind[kind] ?? pendingEntry(kind)}
      icon={icon}
      statusUnknown={!byKind[kind]}
    >
      {controls}
    </JobCard>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        icon={Wrench}
        title="Jobs"
        subtitle="Every piece of background maintenance Shortlist does — what it is, when it next runs, how it went last time, and its full history. Run any of them now when something has drifted rather than waiting for the nightly run."
      />

      <div className="flex flex-wrap gap-3">
        <Tally label="jobs run" value={totals.total} />
        <Tally
          label="in flight"
          value={totals.active}
          tone={totals.active > 0 ? "primary" : undefined}
        />
        <Tally
          label="failed"
          value={totals.failed}
          tone={totals.failed > 0 ? "destructive" : undefined}
        />
      </div>

      {catalog.isError && (
        <MutationAlert
          error={catalog.error}
          fallback="Couldn't load the job list."
          onRetry={() => catalog.refetch()}
        />
      )}

      <div className="grid gap-4">
        {shell(
          "sync.history",
          RefreshCw,
          <SyncHistoryControls
            progress={watchedProgress}
            result={watchedResult}
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
          />,
        )}
        {shell(
          "sync.users",
          UsersIcon,
          <SyncUsersControls
            progress={usersProgress}
            usersCron={
              ((settings.data ?? {})["sync.users_cron"] as string) ?? ""
            }
            onCronChange={(cron) =>
              saveSettings.mutate(
                { "sync.users_cron": cron },
                {
                  onSuccess: () =>
                    queryClient.invalidateQueries({ queryKey: ["syncs"] }),
                },
              )
            }
          />,
        )}
        {shell("sync.check", ShieldCheck, <SyncCheckControls />)}
        {shell(
          "privacy.sync",
          Lock,
          <RunJobButton kind="privacy.sync" label="Sync privacy now" />,
        )}
        {shell("backup.take", Database, <BackupControls />)}
      </div>

      {automatic.length > 0 && (
        <section aria-labelledby="automatic-heading" className="space-y-3">
          <div>
            <h2 id="automatic-heading" className="text-lg font-semibold">
              Automatic
            </h2>
            <p className="text-sm text-muted-foreground">
              Queued for you when something changes — you never start these
              yourself. They&rsquo;re here because one that runs out of retries
              has nowhere else to be seen.
            </p>
          </div>
          <div className="grid gap-4">
            {automatic.map((entry) => (
              <JobCard key={entry.kind} entry={entry} icon={Cog} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

/** "Run now" for a job with no bespoke controls of its own. */
function RunJobButton({ kind, label }: { kind: string; label: string }) {
  const queryClient = useQueryClient();
  const run = useMutation({
    // background: the request returns as soon as the job is queued and the card polls for the
    // outcome, so a slow job can't end in a proxy timeout that reads as a failure.
    mutationFn: () => api.runJob(kind, {}, true),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  return (
    <div className="space-y-3">
      <Button
        variant="outline"
        loading={run.isPending}
        onClick={() => run.mutate()}
      >
        <Play aria-hidden="true" />
        {label}
      </Button>
      {run.isError && (
        <MutationAlert
          error={run.error}
          fallback="Couldn't start that job. Try again."
          onRetry={() => run.mutate()}
        />
      )}
    </div>
  );
}

/** Re-read every user's complete watched set now, rather than waiting for the nightly sync. */
function SyncHistoryControls({
  progress,
  result,
  watchCron,
  onCronChange,
}: {
  progress: SyncProgressEvent | null;
  result: SyncFinishedEvent | null;
  watchCron: string;
  onCronChange: (cron: string) => void;
}) {
  const sync = useMutation({ mutationFn: api.syncWatched });
  // This POST returns 202 the moment the sync is QUEUED — the real outcome arrives on the bus as
  // `result`. So the bar is live while events flow, then the bus result (not the POST) is the truth.
  const running = progress !== null;

  return (
    <>
      <div className="flex flex-col gap-3">
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
      </div>
    </>
  );
}

/** Re-pull the shared + Home users (and the owner) from plex.tv into the users table. */
function SyncUsersControls({
  progress,
  usersCron,
  onCronChange,
}: {
  progress: SyncProgressEvent | null;
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
    <>
      <div className="flex flex-col gap-3">
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
      </div>
    </>
  );
}

const RETENTION_OPTIONS = [
  { value: 5, label: "5" },
  { value: 10, label: "10" },
  { value: 20, label: "20" },
  { value: 30, label: "30" },
];

function BackupControls() {
  const queryClient = useQueryClient();
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
    <>
      <div className="space-y-4">
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
            Restoring also puts back who could see which rows at the time of the
            backup. If you have narrowed a shared row&rsquo;s audience since
            then, those people will be able to see it again after the next run.
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
      </div>
    </>
  );
}

/**
 * Sync check — the convergence pass, on demand.
 *
 * Things drift out of sync because a run only ever writes promotion flags for the people IN that
 * run: anyone paused, disabled, or caught by a run that died keeps whatever flags they last got.
 * The nightly run fixes it, but that can be a day away.
 */
function SyncCheckControls() {
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
    <>
      <div className="flex flex-col gap-3">
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
      </div>
    </>
  );
}
