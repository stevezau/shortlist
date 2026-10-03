import { ArrowUpDown } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { SaveStatus } from "@/components/save-status";
import { useSaveBarReport } from "@/components/settings/save-bar-context";
import { SettingRow, SettingsPanel, SettingsSection } from "@/components/settings/section-layout";
import { Button } from "@/components/ui/button";
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

  const inSaveBar = useSaveBarReport("placement", save);

  return (
    <SettingsSection
      id="placement"
      title="Row placement"
      description="Where Shortlist’s rows sit on each library’s Recommended shelf."
    >
      {!inSaveBar && (
        <SaveStatus
          isPending={save.isPending}
          isError={save.isError}
          error={save.error}
          saved={save.saved}
          onRetry={save.retry}
        />
      )}
      <SettingsPanel>
        <SettingRow
          title="Let Shortlist order the Recommended shelf"
          control={
            <Switch
              checked={manageOrder}
              onCheckedChange={setManageOrder}
              aria-label="Let Shortlist order the Recommended shelf"
            />
          }
        >
          {/* What the switch costs, on its own line: it is the one Shortlist control that touches
              rows it did not make (plex-safety rule 4's shelf-position exception). */}
          <p className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <ArrowUpDown className="h-3.5 w-3.5 text-accent-foreground" aria-hidden="true" />
            Moves other tools’ rows on the shelf.
          </p>
          <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
            Their order relative to each other is kept; only their position
            shifts so Shortlist’s rows can sit among them. Turn it{" "}
            <strong className="text-foreground">off</strong> if another tool
            (Kometa, Agregarr) orders that shelf, and Shortlist will leave it
            alone. Rows are still built, delivered and kept private either way.
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
          <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
            Running one of those? An out-of-date{" "}
            <strong className="text-foreground">Agregarr</strong> can put
            other people’s rows on <em>your</em> Home between runs.{" "}
            <a
              href={DOCS_SHELF_CONTENTION_URL}
              target="_blank"
              rel="noreferrer"
              className="font-medium text-accent-foreground underline underline-offset-2 hover:text-foreground"
            >
              How to run one alongside Shortlist
            </a>
            .
          </p>
          {!manageOrder && (
            <p className="rounded-md border border-dashed bg-muted/30 p-3 text-xs text-muted-foreground">
              Shelf ordering is off — Shortlist won’t touch the Recommended
              shelf order. Your rows are still built, delivered and kept
              private; Plex drops new ones at the end of the shelf and they
              stay there.
            </p>
          )}
        </SettingRow>
        {/* Only while ordering is on: with it off, Shortlist places nothing, so a row's own spot
            is not applied either. */}
        {manageOrder && <SettingRow
          title="Where each row goes"
          description={
            <>
              Set on the row itself: top of the shelf, just after or before a
              collection you pick, or not positioned at all. Look for{" "}
              <strong className="text-foreground">Plex placement</strong> in
              the row editor.
            </>
          }
          control={
            <Button asChild variant="outline" size="sm">
              <Link to="/rows">Open Rows</Link>
            </Button>
          }
        />}
      </SettingsPanel>
    </SettingsSection>
  );
}
