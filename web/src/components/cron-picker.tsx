import { useState } from "react";

import { CronInput } from "@/components/cron-input";
import { Segmented } from "@/components/segmented";

/** The four presets every "how often" picker in Jobs offers — "" (Daily) is the built-in default,
 *  not an off state. */
const SYNC_PRESETS = [
  { value: "", label: "Daily" },
  { value: "17 */12 * * *", label: "12h" },
  { value: "17 */6 * * *", label: "6h" },
  { value: "17 */4 * * *", label: "4h" },
];

/** Daily/12h/6h/4h presets, or drop to `CronInput` for anything else. Shared by every job's
 *  frequency editor (sync, backup) so "how often" is one control, not one per job. */
export function CronPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (cron: string) => void;
}) {
  const matchesPreset = SYNC_PRESETS.some((p) => p.value === value);
  const [custom, setCustom] = useState(!matchesPreset && value !== "");

  return (
    <div className="flex flex-wrap items-start gap-2">
      <span className="pt-1.5 text-xs text-muted-foreground">Frequency:</span>
      <Segmented
        value={custom ? "__custom__" : value}
        onChange={(v) => {
          if (v === "__custom__") {
            setCustom(true);
          } else {
            setCustom(false);
            onChange(v);
          }
        }}
        options={[
          ...SYNC_PRESETS.map((p) => ({ value: p.value, label: p.label })),
          { value: "__custom__", label: "Custom" },
        ]}
      />
      {custom && <CronInput value={value} onChange={onChange} />}
    </div>
  );
}
