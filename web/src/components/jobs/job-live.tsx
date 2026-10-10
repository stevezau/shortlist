import { CheckCircle2 } from "lucide-react";
import { Link } from "react-router";
import type { UseMutationResult } from "@tanstack/react-query";

import { MutationAlert } from "@/components/mutation-alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ProgressBar } from "@/components/ui/progress-bar";
import type { api } from "@/lib/api";
import { driftFindings } from "@/lib/job-drift";
import type { SyncFinishedEvent, SyncProgressEvent } from "@/lib/types";

// What is happening, or just happened, because you pressed a job's button. The bodies of the
// JobRow `live` slots on the Jobs tab; the row still decides WHEN to show one.

type Result<F extends (...args: never[]) => Promise<unknown>> = UseMutationResult<
  Awaited<ReturnType<F>>,
  Error,
  void
>;

function SyncBar({
  done,
  total,
  label,
  line,
}: {
  // SSE fields are optional AND nullable (Pydantic `int | None`, not just "absent"): normalise
  // before handing off to ProgressBar, which only knows "omit for indeterminate".
  done?: number | null;
  total?: number | null;
  label: string;
  line: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <ProgressBar
        done={done ?? undefined}
        total={total ?? undefined}
        label={label}
      />
      <p role="status" className="text-xs text-muted-foreground">
        {line}
      </p>
    </div>
  );
}

export function Succeeded({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-center gap-2 text-sm text-foreground">
      <CheckCircle2
        aria-hidden="true"
        className="size-4 shrink-0 text-success"
      />
      {children}
    </p>
  );
}

export function SyncUsersLive({
  syncUsers,
  usersProgress,
}: {
  syncUsers: Result<typeof api.syncUsers>;
  usersProgress: SyncProgressEvent | null;
}) {
  return (
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
            usersProgress?.phase === "save" &&
            usersProgress.total
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
          {syncUsers.data.added > 0 ||
          syncUsers.data.updated > 0
            ? `Synced ${syncUsers.data.total} ${syncUsers.data.total === 1 ? "user" : "users"} — ${syncUsers.data.added} added, ${syncUsers.data.updated} updated.`
            : `All ${syncUsers.data.total} ${syncUsers.data.total === 1 ? "user is" : "users are"} already up to date.`}
        </Succeeded>
      )}
    </div>
  );
}

export function SyncWatchedLive({
  syncWatched,
  watchedProgress,
  watchedResult,
}: {
  syncWatched: Result<typeof api.syncWatched>;
  watchedProgress: SyncProgressEvent | null;
  watchedResult: SyncFinishedEvent | null;
}) {
  const watchedRunning = watchedProgress !== null;
  return (
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
        <p
          role="alert"
          className="text-sm text-destructive-text"
        >
          The sync couldn&rsquo;t finish
          {watchedResult.error
            ? ` (${watchedResult.error})`
            : ""}
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
            Sync started — it runs in the background across
            every user. The effectiveness report updates on its
            own once it finishes.
          </Succeeded>
        )}
    </div>
  );
}

export function DriftLive({
  driftPreview,
  driftFix,
  onConfirmDelete,
}: {
  driftPreview: Result<typeof api.runJob>;
  driftFix: Result<typeof api.runJob>;
  /** Fix was pressed while it would delete something; the caller asks first. */
  onConfirmDelete: () => void;
}) {
  const { drifted, orphans } = driftFindings(driftPreview.data);
  return (
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
          &mdash; {orphans.join(", ")}. Shortlist no longer
          knows who they belong to, so hiding them would leave
          them in your Collections tab for ever. This cannot be
          undone.
        </p>
      )}
      {/* `status` matters: the queue skips a drain while a run is writing to Plex,
        which is exactly when someone presses this. Reporting "everything is in
        sync" for a check that never ran would be a lie. */}
      {driftPreview.data &&
        !driftPreview.data.error &&
        driftPreview.data.status !== "done" && (
          <p className="text-sm text-muted-foreground">
            Waiting for the current run to finish — the check
            will run straight after.
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
            variant="outline"
            loading={driftFix.isPending}
            // Confirm at the CLICK when this will delete, which every other
            // irreversible Plex write in the app already does (row delete, row
            // cleanup, disable-everyone, backup restore). The callout above already
            // names each collection, so the audit's "no confirm at all" was half
            // wrong — but one verb still fired reversible demotions and an
            // unrecoverable delete together, with nothing between the press and the
            // destruction. Only when something will actually be deleted: a confirm
            // on every fix teaches people to click through the one that matters.
            onClick={() =>
              orphans.length > 0
                ? onConfirmDelete()
                : driftFix.mutate()
            }
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
  );
}

export function PrivacySyncLive({ privacySync }: { privacySync: Result<typeof api.runJob> }) {
  return (
    <MutationAlert
      error={privacySync.error}
      fallback="Couldn't start that job. Try again."
      onRetry={() => privacySync.mutate()}
    />
  );
}

export function BackupLive({ backupNow }: { backupNow: Result<typeof api.createBackup> }) {
  return (
    <>
      {/* "Backup failed." was the whole message — no cause, no next step, on the
          one operation whose entire purpose is to be there when something else
          goes wrong. The server's own detail is terse too ("backup failed"), so
          the standing line under it carries what the owner can actually check. */}
      {backupNow.isError && (
        <div className="space-y-1">
          <MutationAlert
            error={backupNow.error}
            fallback="Couldn’t take a backup."
            onRetry={() => backupNow.mutate()}
          />
          <p className="text-xs text-muted-foreground">
            Backups are written next to your database, in{" "}
            <span className="font-mono">/config/backups</span>.
            Check the disk has room and that Shortlist can write
            there — the{" "}
            <Link
              to="/activity?tab=log"
              className="font-medium underline underline-offset-2"
            >
              Activity log
            </Link>{" "}
            has the reason it gave.
          </p>
        </div>
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
  );
}

export function PruneLive({ pruneNow }: { pruneNow: Result<typeof api.runJob> }) {
  return (
    <div className="flex flex-col gap-3">
      {pruneNow.isError && (
        <MutationAlert
          error={pruneNow.error}
          fallback="Couldn't clear out old records. Try again."
          onRetry={() => pruneNow.mutate()}
        />
      )}
      {/* `status` matters here for the same reason it does on the check above: the
          job can come back still queued, and reporting a tidy-up that never ran
          would be a lie. */}
      {pruneNow.data &&
        !pruneNow.data.error &&
        pruneNow.data.status !== "done" && (
          <p className="text-sm text-muted-foreground">
            Queued — it will run as soon as there's a free slot.
          </p>
        )}
      {pruneNow.data?.status === "done" && (
        <Succeeded>
          {pruneNow.data.detail ||
            "There was nothing old enough to clear out."}
        </Succeeded>
      )}
      {pruneNow.data?.error && (
        <p
          role="alert"
          className="text-sm text-destructive-text"
        >
          {pruneNow.data.error}
        </p>
      )}
    </div>
  );
}

/** The confirmation before a sync-check fix that would delete collections Shortlist can no longer attribute. */
export function DeleteOrphansDialog({
  open,
  onOpenChange,
  orphans,
  driftFix,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  orphans: string[];
  driftFix: Result<typeof api.runJob>;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Delete {orphans.length} collection
            {orphans.length === 1 ? "" : "s"}?
          </DialogTitle>
          <DialogDescription>
            {orphans.join(", ")} will be removed from Plex for good. Shortlist
            no longer knows who they belong to, so it cannot hide them
            instead. The titles themselves stay in your library. This can’t be
            undone.
          </DialogDescription>
        </DialogHeader>
        {/* Inside the dialog, not beside the button that opened it: a failure leaves this dialog
            open, and everything behind an open dialog is aria-hidden — an alert out there would be
            invisible to a screen reader and buried under the overlay for everyone else. */}
        {driftFix.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            Couldn’t fix those rows. Try again.
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            loading={driftFix.isPending}
            onClick={() =>
              driftFix.mutate(undefined, {
                onSuccess: () => onOpenChange(false),
              })
            }
          >
            Delete and fix
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
