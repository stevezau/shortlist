import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { UninstallDialog } from "@/components/uninstall-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, apiErrorMessage } from "@/lib/api";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** Pause every user at once, and the full uninstall (with a dry-run preview) behind a dialog. */
export function DangerZoneSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const [uninstallOpen, setUninstallOpen] = useState(false);
  const pausedAll = settings["paused_all"] === true;

  const uninstall = useMutation({ mutationFn: () => api.uninstall(false) });
  const uninstallPreview = useMutation({
    mutationFn: () => api.uninstall(true),
  });

  return (
    <section aria-labelledby="danger-heading" className="space-y-3">
      <h2
        id="danger-heading"
        className="text-lg font-semibold text-destructive"
      >
        Danger zone
      </h2>
      <Card className="border-destructive/40">
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="font-medium">
                {pausedAll ? "Everything is paused" : "Pause all users"}
              </p>
              <p className="text-sm text-muted-foreground">
                Stops scheduled and manual runs alike — no user stays enabled or
                gets disabled, and rows stay on Plex untouched until you resume.
              </p>
            </div>
            <Button
              variant="outline"
              onClick={() => saveSettings.mutate({ paused_all: !pausedAll })}
              loading={saveSettings.isPending}
            >
              {pausedAll ? "Resume all" : "Pause all"}
            </Button>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
            <div>
              <p className="font-medium">Full uninstall</p>
              <p className="text-sm text-muted-foreground">
                Removes every Rowarr collection and label, and restores all
                share filters from the original snapshots. Preview the exact
                changes before committing.
              </p>
            </div>
            <Button
              variant="destructive"
              onClick={() => setUninstallOpen(true)}
            >
              Uninstall Rowarr…
            </Button>
          </div>
          {uninstall.isSuccess && (
            <p role="status" className="text-sm text-success">
              {uninstall.data.message ||
                "Uninstall complete. Your server is as Rowarr found it."}
            </p>
          )}
          {uninstall.isError && (
            <p role="alert" className="text-sm text-destructive">
              {apiErrorMessage(
                uninstall.error,
                "Uninstall failed. Nothing was left half-done — see the server log, then try again.",
              )}
            </p>
          )}
        </CardContent>
      </Card>

      <UninstallDialog
        open={uninstallOpen}
        onOpenChange={(open) => {
          setUninstallOpen(open);
          if (!open) uninstallPreview.reset();
        }}
        pending={uninstall.isPending}
        onConfirm={() =>
          uninstall.mutate(undefined, {
            onSuccess: () => setUninstallOpen(false),
          })
        }
        onPreview={() => uninstallPreview.mutate()}
        previewPending={uninstallPreview.isPending}
        preview={uninstallPreview.data ?? null}
      />
    </section>
  );
}
