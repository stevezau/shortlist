import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

/** The full uninstall: the one irreversible control in Settings, linking to its own page (with a
 *  live log). Pause all users is reversible, so it sits under System → Run speed. */
export function DangerZoneSection() {
  return (
    <section id="danger" aria-labelledby="danger-heading" className="scroll-mt-32 space-y-3 md:scroll-mt-8">
      <h2 id="danger-heading" className="text-lg font-semibold tracking-tight text-destructive-text">
        Danger zone
      </h2>
      <Card className="border-destructive/40">
        <CardContent className="pt-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
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
