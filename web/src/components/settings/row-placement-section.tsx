import { useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingBool } from "@/lib/format";
import { DOCS_SHELF_CONTENTION_URL } from "@/lib/support";
import type { Settings } from "@/lib/types";

/** The one switch for shelf ordering. WHERE each row goes is chosen on the row itself — this used to
 *  also hold a per-library default, which was a second source of truth for the same decision and
 *  disagreed with the engine about what its own "Wherever Plex puts them" option meant. */
export function RowPlacementSection({ settings }: { settings: Settings }) {
  const [manageOrder, setManageOrder] = useState<boolean>(() =>
    settingBool(settings, "rows.manage_shelf_order", true),
  );

  // `rows.hub_anchor` is no longer written from here. It was a SECOND source of truth for the same
  // decision, and it contradicted itself: "Wherever Plex puts them" wrote no entry, and with no
  // library configured the engine read that as "top of the shelf", while the moment one library WAS
  // configured every other one silently became "leave alone". Placement now lives on the row.
  const save = useAutosavedSettings({ manageOrder }, () => ({
    "rows.manage_shelf_order": manageOrder,
  }));

  return (
    <section aria-labelledby="placement-heading" className="space-y-3">
      <h2 id="placement-heading" className="text-lg font-semibold">
        Row placement
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <p className="font-medium">
                Let Shortlist order the Recommended shelf
              </p>
              <p className="text-sm text-muted-foreground">
                Turn it <strong className="text-foreground">off</strong> if
                another tool (Kometa, Agregarr) orders that shelf, and Shortlist
                will leave it alone. Rows are still built, delivered and kept
                private either way.
              </p>
              {/* One clause and a link, not the 458-character version this used to print
                  unconditionally — a fork recommendation, a GitHub URL and a Docker image name, on
                  a settings screen, for a tool most owners do not run.

                  The WARNING survives the trim, because it is the half that matters and it is not
                  in the guides: an unmaintained Agregarr re-promotes collections with Plex's
                  defaults, which puts other people's rows on the owner's own Home. Hedged, as it
                  always was — Shortlist clears that every run, so it is a gap between runs.

                  This line stays on screen at all rather than being left to the "something is
                  reordering your shelf" notification, because that one only fires while this
                  switch is ON — and turning it off is the fix it recommends. */}
              <p className="text-sm text-muted-foreground">
                Running one of those? An out-of-date{" "}
                <strong className="text-foreground">Agregarr</strong> can put
                other people’s rows on <em>your</em> Home between runs.{" "}
                <a
                  href={DOCS_SHELF_CONTENTION_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium underline underline-offset-2 hover:text-foreground"
                >
                  How to run one alongside Shortlist
                </a>
                .
              </p>
            </div>
            <Switch
              checked={manageOrder}
              onCheckedChange={setManageOrder}
              aria-label="Let Shortlist order the Recommended shelf"
            />
          </div>

          {!manageOrder ? (
            <p className="rounded-md border border-dashed bg-muted/30 p-3 text-sm text-muted-foreground">
              Shelf ordering is off — Shortlist won’t touch the Recommended
              shelf order. Your rows are still built, delivered and kept
              private; Plex drops new ones at the end of the shelf and they
              stay there.
            </p>
          ) : (
            <p className="border-t pt-4 text-sm text-muted-foreground">
              Each row chooses its own spot, on the row itself — top of the
              shelf, just after or before a collection you pick, or not
              positioned at all. Open a row and look for{" "}
              <strong className="text-foreground">Where it sits</strong>.
            </p>
          )}

          <SaveStatus
            isPending={save.isPending}
            isError={save.isError}
            error={save.error}
            saved={save.saved}
            onRetry={save.retry}
          />
        </CardContent>
      </Card>
    </section>
  );
}
