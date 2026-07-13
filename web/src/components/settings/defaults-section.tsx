import { useId, useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { ROW_SIZES } from "@/lib/constants";
import { renderRowName, settingNumber, settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** The default row name template and row size applied to the "Picked for You" row. */
export function DefaultsSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const [rowNameTpl, setRowNameTpl] = useState(
    settingString(settings, "row.name_template", "✨ Picked for You"),
  );
  const [rowSize, setRowSize] = useState(
    settingNumber(settings, "row.size", 15),
  );
  const [justSaved, setJustSaved] = useState(false);
  const rowNameId = useId();

  const save = () => {
    setJustSaved(false);
    saveSettings.mutate(
      { "row.name_template": rowNameTpl, "row.size": rowSize },
      { onSuccess: () => setJustSaved(true) },
    );
  };

  return (
    <section aria-labelledby="defaults-heading" className="space-y-3">
      <h2 id="defaults-heading" className="text-lg font-semibold">
        Defaults
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="space-y-2">
            <Label htmlFor={rowNameId}>Row name template</Label>
            <Input
              id={rowNameId}
              value={rowNameTpl}
              onChange={(event) => setRowNameTpl(event.target.value)}
            />
            <p className="text-sm text-muted-foreground">
              Use <span className="font-mono">{"{top_seed}"}</span> for each
              user's top watched title.
            </p>
            <div className="rounded-md border bg-card p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">
                On Plex this looks like
              </p>
              <p className="font-medium text-primary">
                {renderRowName(rowNameTpl) || "✨ Picked for You"}
              </p>
            </div>
          </div>
          <Segmented
            legend="Row size"
            value={String(rowSize)}
            options={ROW_SIZES.map((size) => ({
              value: String(size),
              label: String(size),
            }))}
            onChange={(size) => setRowSize(Number(size))}
          />
          <div className="flex items-center gap-3">
            <Button onClick={save} loading={saveSettings.isPending}>
              Save defaults
            </Button>
            <SavedIndicator show={justSaved && !saveSettings.isPending} />
          </div>
          {saveSettings.isError && (
            <p role="alert" className="text-sm text-destructive">
              {apiErrorMessage(
                saveSettings.error,
                "Saving failed. Check the server log and try again.",
              )}
            </p>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
