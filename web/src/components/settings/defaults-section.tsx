import { useId, useState } from "react";

import { RowSizeField } from "@/components/row-size-field";
import { SaveStatus } from "@/components/save-status";
import { useSaveBarReport } from "@/components/settings/save-bar-context";
import { SettingsPanel, SettingsSection } from "@/components/settings/section-layout";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAutosavedSettings } from "@/lib/autosave";
import { ROW_SIZE_DEFAULT } from "@/lib/constants";
import { renderRowName, settingNumber, settingString } from "@/lib/format";
import { unselectedClass } from "@/lib/selected";
import type { Settings } from "@/lib/types";
import { cn } from "@/lib/utils";

/** The default row name template and row size applied to the "Picked for You" row. */
export function DefaultsSection({ settings }: { settings: Settings }) {
  const [rowNameTpl, setRowNameTpl] = useState(
    settingString(
      settings,
      "row.name_template",
      "✨ {library_name} Picked for You",
    ),
  );
  const [rowSize, setRowSize] = useState(
    settingNumber(settings, "row.size", ROW_SIZE_DEFAULT),
  );
  const rowNameId = useId();

  const save = useAutosavedSettings({ rowNameTpl, rowSize }, () => ({
    "row.name_template": rowNameTpl,
    "row.size": rowSize,
  }));

  const inSaveBar = useSaveBarReport("row-defaults", save);

  return (
    <SettingsSection
      id="row-defaults"
      title="Row defaults"
      description="What a new row is called and how many titles it holds. Existing rows keep their own."
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
        <div className="space-y-3 px-4 py-4 sm:px-5">
          <Label htmlFor={rowNameId} className="text-[13px]">Row name template</Label>
          <Input
            id={rowNameId}
            value={rowNameTpl}
            onChange={(event) => setRowNameTpl(event.target.value)}
          />
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground" role="group" aria-label="Insert a name variable">
            <span className="mr-1">Insert</span>
            {["library_name", "user", "top_seed"].map((token) => (
              <button
                key={token}
                type="button"
                aria-label={`Insert ${token.replaceAll("_", " ")}`}
                onClick={() => setRowNameTpl((name) => `${name}${name.endsWith(" ") ? "" : " "}{${token}}`)}
                className={cn("rounded-full border px-2.5 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring", unselectedClass)}
              >
                + {token.replaceAll("_", " ")}
              </button>
            ))}
          </div>
          <details className="text-xs text-muted-foreground">
            <summary className="w-fit cursor-pointer text-accent-foreground underline-offset-2 hover:underline">How name variables work</summary>
            <p className="max-w-prose pt-2 leading-relaxed">Library name becomes Movies or TV Shows; user becomes the person’s name; top seed becomes a title they recently watched. Row names do not change your Plex sharing settings.</p>
          </details>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-md border bg-elevated px-3 py-2.5">
            <span className="text-xs text-muted-foreground">On Plex this looks like</span>
            <span className="font-medium text-foreground [overflow-wrap:anywhere]">
              {renderRowName(rowNameTpl) || "✨ Picked for You"}
            </span>
          </div>
        </div>
        <div className="px-4 py-4 sm:px-5">
          <RowSizeField value={rowSize} onChange={setRowSize} />
        </div>
      </SettingsPanel>
    </SettingsSection>
  );
}
