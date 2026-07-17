import { Link } from "react-router-dom";

import { CleanupAuditCard } from "@/components/settings/cleanup-audit-card";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** Pause every user at once, and a link to the full uninstall (its own page, with a live log). */
export function DangerZoneSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const pausedAll = settings["paused_all"] === true;

  return (
    <section aria-labelledby="danger-heading" className="space-y-3">
      <h2
        id="danger-heading"
        className="text-lg font-semibold text-destructive"
      >
        Danger zone
      </h2>
      <CleanupAuditCard />
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
                Removes every Shortlist collection and label, restores all share
                filters from the original snapshots, and switches off every row
                so nothing rebuilds. Opens a dedicated page with a preview and a
                live log of every step.
              </p>
            </div>
            <Button asChild variant="destructive">
              <Link to="/settings/uninstall">Uninstall Shortlist…</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
