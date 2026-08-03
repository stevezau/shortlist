import { useState } from "react";

import { CronInput } from "@/components/cron-input";
import { Segmented } from "@/components/segmented";
import { dailyCronTime } from "@/lib/cron";

/** The three interval presets every "how often" picker in Jobs offers. The blank preset is added
 *  separately, because what a blank cron MEANS differs by job — see {@link CronPicker}. */
const INTERVAL_PRESETS = [
  { value: "17 */12 * * *", label: "12h" },
  { value: "17 */6 * * *", label: "6h" },
  { value: "17 */4 * * *", label: "4h" },
];

/** Segmented value for "put this back on its built-in schedule". Not a cron: restoring the default
 *  is a different write from saving one (`null`, not a string), so it needs its own token. */
const RESTORE_DEFAULT = "__default__";

/**
 * Daily/12h/6h/4h presets, or drop to `CronInput` for anything else. Shared by every job's
 * frequency editor (sync, backup) so "how often" is one control, not one per job.
 *
 * `blankLabel` exists because a blank cron does not mean the same thing for every job. For most it
 * means "use the built-in default", which is daily — hence "Daily". For the drift check it is the
 * OFF switch (`scheduler._OFF_ABLE`), and a chip labelled "Daily" that switches a Plex-writing job
 * off is the worst kind of lie a control can tell.
 *
 * `defaultCron` + `onRestoreDefault` add the way BACK from that off switch. Where blank means off,
 * no chip means "the built-in schedule", so returning to it meant typing the cron into Custom — and
 * the SPA cannot offer that cron from its own knowledge, because a second copy of the server's
 * `DEFAULT_CRONS` is exactly how the drift check came to be documented as off-by-default while it
 * ran nightly. So the cron comes from `GET /api/schedule`, and picking the chip calls
 * `onRestoreDefault`, which saves `null` — the value that means "inherit", where "" means off.
 */
export function CronPicker({
  value,
  onChange,
  blankLabel = "Daily",
  defaultCron = "",
  onRestoreDefault,
}: {
  value: string;
  onChange: (cron: string) => void;
  blankLabel?: string;
  /** The job's built-in cron, straight from the server. Omit for a job with no restore chip. */
  defaultCron?: string;
  /** Saves "use the built-in default". Required with `defaultCron` for the chip to appear. */
  onRestoreDefault?: () => void;
}) {
  const presets = [{ value: "", label: blankLabel }, ...INTERVAL_PRESETS];
  const restorable = Boolean(defaultCron) && onRestoreDefault !== undefined;
  // Selected by the cron it RUNS on, which is all this component is given. A cron typed into Custom
  // that happens to equal the built-in one therefore also lights this chip up — the two schedules
  // are the same times, and pressing it only changes which of them the setting means.
  const onDefault = restorable && value === defaultCron;
  const matchesPreset = onDefault || presets.some((p) => p.value === value);
  // Derived, not initial-only state: `value` can arrive AFTER first render (it comes from a query),
  // and a `useState` initialiser never re-runs — so a cron that lands late left every chip
  // unselected and the custom box hidden, with no way to see what the job actually runs on.
  const [customOpened, setCustom] = useState(false);
  const custom = customOpened || (!matchesPreset && value !== "");
  const time = restorable ? dailyCronTime(defaultCron) : null;

  return (
    <div className="flex flex-wrap items-start gap-2">
      <span className="pt-1.5 text-xs text-muted-foreground">Frequency:</span>
      <Segmented
        value={custom ? "__custom__" : onDefault ? RESTORE_DEFAULT : value}
        onChange={(v) => {
          if (v === "__custom__") {
            setCustom(true);
          } else if (v === RESTORE_DEFAULT) {
            setCustom(false);
            onRestoreDefault?.();
          } else {
            setCustom(false);
            onChange(v);
          }
        }}
        options={[
          ...presets.map((p) => ({ value: p.value, label: p.label })),
          ...(restorable
            ? [
                {
                  value: RESTORE_DEFAULT,
                  label: time ? `Built-in (${time})` : "Built-in",
                },
              ]
            : []),
          { value: "__custom__", label: "Custom" },
        ]}
      />
      {custom && <CronInput value={value} onChange={onChange} />}
    </div>
  );
}
