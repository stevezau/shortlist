import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { CronPicker } from "@/components/cron-picker";
import { MutationAlert } from "@/components/mutation-alert";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { describeCron } from "@/lib/cron";
import { formatSize, timeAgo } from "@/lib/format";
import { queryKeys, useSaveSettings, useSettings } from "@/lib/queries";

const RETENTION_OPTIONS = ["5", "10", "20", "30"];

/** Mirrors the scheduler's own default (`DEFAULT_CRONS["backup.cron"]`, scheduler.py) — a blank
 *  `backup.cron` setting falls back to this, so the empty-state copy below has to describe THIS
 *  schedule rather than a hardcoded one that drifts from it. */
const DEFAULT_BACKUP_CRON = "0 3 * * *";

/** "every day at 3:00 AM" / "every 12 hours, at 17 minutes past" — whatever `backupCron` actually
 *  is, not a hardcoded "tonight at 3 AM" that used to say the default even after it was changed via
 *  the picker above. */
function describeBackupSchedule(cron: string): string {
  const description = describeCron(cron || DEFAULT_BACKUP_CRON);
  const first = description.charAt(0);
  if (!first) return "on its schedule";
  return first.toLowerCase() + description.slice(1);
}

/** Backups: what's in one, where it lives, how often, how many to keep, and the restore list. */
export function BackupPanel() {
  const queryClient = useQueryClient();
  const settings = useSettings();
  const saveSettings = useSaveSettings();
  const backups = useQuery({
    queryKey: queryKeys.backups,
    queryFn: api.getBackups,
  });
  const restore = useMutation({ mutationFn: api.restoreBackup });
  const [confirmRestore, setConfirmRestore] = useState<string | null>(null);

  const backupCron = ((settings.data ?? {})["backup.cron"] as string) ?? "";
  const backupMaxKeep =
    ((settings.data ?? {})["backup.max_keep"] as number) ?? 10;

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
                  queryClient.invalidateQueries({ queryKey: queryKeys.syncs }),
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
                    queryClient.invalidateQueries({
                      queryKey: queryKeys.syncs,
                    });
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
          No backups yet. One will be created automatically{" "}
          {describeBackupSchedule(backupCron)}.
        </p>
      )}
    </div>
  );
}
