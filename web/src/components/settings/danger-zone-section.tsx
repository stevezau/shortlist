import { Link } from "react-router";

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
        className="text-lg font-semibold text-destructive-text"
      >
        Danger zone
      </h2>
      {/* The read-only Plex audit used to sit here, above everything. It was the safest control on
          the page — it changes nothing — under the scariest heading, which reads as a warning it
          does not deserve. It lives under Advanced now. */}
      <Card className="border-destructive/40">
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="font-medium">
                {pausedAll ? "Everything is paused" : "Pause all users"}
              </p>
              {/* Precise: a run still STARTS (`enabled_profiles` returns [] while paused, and the
                engine still does its privacy sweep on an empty user list). What stops is any row
                being built or re-picked. */}
              <p className="text-sm text-muted-foreground">
                Nobody is processed on any run, scheduled or manual, until you
                resume &mdash; so no row is rebuilt and nobody&rsquo;s picks
                change.
                <br />
                Nobody is enabled or disabled, and the rows already on Plex stay
                where they are.
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
                Completely removes Shortlist from Plex:
                <br />
                deletes every Shortlist collection and label, puts everyone’s
                share settings back the way they were, and turns off every row
                so nothing rebuilds.
                <br />
                Opens its own page with a preview and a live log of each step.
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
