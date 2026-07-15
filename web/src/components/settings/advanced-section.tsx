import { Segmented } from "@/components/segmented";
import { Card, CardContent } from "@/components/ui/card";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

const LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] as const;
type Level = (typeof LEVELS)[number];

const CONCURRENCY = [1, 2, 4, 8] as const;

/** Power-user knobs: log verbosity + run concurrency. Both auto-save and apply live — no restart. */
export function AdvancedSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const level = (
    LEVELS.includes(settings["log.level"] as Level)
      ? settings["log.level"]
      : "DEBUG"
  ) as Level;
  const concurrency = String(
    (settings["run.concurrency"] as number | undefined) ?? 4,
  );

  return (
    <section aria-labelledby="advanced-heading" className="space-y-3">
      <h2 id="advanced-heading" className="text-lg font-semibold">
        Advanced
      </h2>
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div>
            <p className="font-medium">Log level</p>
            <p className="text-sm text-muted-foreground">
              How much detail the container writes to its console and rotating
              log file. <strong>DEBUG</strong> shows per-source candidate
              counts, AI calls with timing and token usage, cache hits and
              rate-limit waits; <strong>TRACE</strong> adds the full AI prompts.
              Takes effect immediately — no restart needed.
            </p>
          </div>
          <Segmented<Level>
            value={level}
            ariaLabel="Log level"
            options={LEVELS.map((l) => ({ value: l, label: l }))}
            onChange={(value) => saveSettings.mutate({ "log.level": value })}
          />
          <div className="border-t pt-4">
            <p className="font-medium">Run concurrency</p>
            <p className="text-sm text-muted-foreground">
              How many people a run works on at once. Only their history,
              candidate lookups and AI curation overlap — every change to Plex
              is still made one at a time, in order. Higher is faster for big
              servers; <strong>1</strong> runs everyone sequentially.
            </p>
          </div>
          <Segmented<string>
            value={concurrency}
            ariaLabel="Run concurrency"
            options={CONCURRENCY.map((n) => ({
              value: String(n),
              label: String(n),
            }))}
            onChange={(value) =>
              saveSettings.mutate({ "run.concurrency": Number(value) })
            }
          />
        </CardContent>
      </Card>
    </section>
  );
}
