import { useId, useState } from "react";

import { RowSizeField } from "@/components/row-size-field";
import { SaveStatus } from "@/components/save-status";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAutosavedSettings } from "@/lib/autosave";
import { ROW_SIZE_DEFAULT } from "@/lib/constants";
import { renderRowName, settingNumber, settingString } from "@/lib/format";
import type { Settings } from "@/lib/types";

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

  return (
    <section aria-labelledby="defaults-heading" className="space-y-3">
      <h2 id="defaults-heading" className="text-lg font-semibold">
        Row defaults
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
            <div className="flex flex-wrap items-center gap-2" aria-label="Insert a name variable">
              {["library_name", "user", "top_seed"].map((token) => <Button key={token} type="button" size="sm" variant="outline" onClick={() => setRowNameTpl((name) => `${name}${name.endsWith(" ") ? "" : " "}{${token}}`)}>{token.replaceAll("_", " ")}</Button>)}
            </div>
            <details className="text-sm text-muted-foreground"><summary className="cursor-pointer">How name variables work</summary><p className="pt-2">Library name becomes Movies or TV Shows; user becomes the person’s name; top seed becomes a title they recently watched. Row names do not change your Plex sharing settings.</p></details>
            <div className="rounded-md border bg-card p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">
                On Plex this looks like
              </p>
              <p className="font-medium text-primary">
                {renderRowName(rowNameTpl) || "✨ Picked for You"}
              </p>
            </div>
          </div>
          <RowSizeField value={rowSize} onChange={setRowSize} />
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
