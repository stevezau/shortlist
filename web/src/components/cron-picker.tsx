import { useState } from "react";

import { CronInput } from "@/components/cron-input";
import { Segmented } from "@/components/segmented";

/** The three interval presets every "how often" picker in Jobs offers. The blank preset is added
 *  separately, because what a blank cron MEANS differs by job — see {@link CronPicker}. */
const INTERVAL_PRESETS = [
  { value: "17 */12 * * *", label: "12h" },
  { value: "17 */6 * * *", label: "6h" },
  { value: "17 */4 * * *", label: "4h" },
];

/**
 * Daily/12h/6h/4h presets, or drop to `CronInput` for anything else. Shared by every job's
 * frequency editor (sync, backup) so "how often" is one control, not one per job.
 *
 * `blankLabel` exists because a blank cron does not mean the same thing for every job. For most it
 * means "use the built-in default", which is daily — hence "Daily". For the drift check it is the
 * OFF switch (`scheduler._OFF_ABLE`), and a chip labelled "Daily" that switches a Plex-writing job
 * off is the worst kind of lie a control can tell.
 */
export function CronPicker({
  value,
  onChange,
  blankLabel = "Daily",
}: {
  value: string;
  onChange: (cron: string) => void;
  blankLabel?: string;
}) {
  const presets = [{ value: "", label: blankLabel }, ...INTERVAL_PRESETS];
  const matchesPreset = presets.some((p) => p.value === value);
  // Derived, not initial-only state: `value` can arrive AFTER first render (it comes from a query),
  // and a `useState` initialiser never re-runs — so a cron that lands late left every chip
  // unselected and the custom box hidden, with no way to see what the job actually runs on.
  const [customOpened, setCustom] = useState(false);
  const custom = customOpened || (!matchesPreset && value !== "");

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
          ...presets.map((p) => ({ value: p.value, label: p.label })),
          { value: "__custom__", label: "Custom" },
        ]}
      />
      {custom && <CronInput value={value} onChange={onChange} />}
    </div>
  );
}
