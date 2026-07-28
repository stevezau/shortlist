import { useState } from "react";

import { Input } from "@/components/ui/input";
import { describeCron, parseNaturalSchedule } from "@/lib/cron";

/**
 * The "Custom" schedule box, shared by every schedule picker in the app.
 *
 * One field takes either form: plain English ("every 4 hours", "mondays at 9pm") or a raw cron
 * expression. Whatever is typed, the line underneath says what it will actually do and what gets
 * saved, so a schedule is never committed on trust. Nothing is saved until the input parses — a
 * typo used to save fine and then be silently replaced by the scheduler's built-in default.
 */
export function CronInput({
  value,
  onChange,
  id,
}: {
  value: string;
  onChange: (cron: string) => void;
  id?: string;
}) {
  const [draft, setDraft] = useState(value);
  const trimmed = draft.trim();
  const cron = parseNaturalSchedule(draft);
  const description = cron ? describeCron(cron) : "";

  const commit = () => {
    if (!cron) return;
    setDraft(cron);
    onChange(cron);
  };

  return (
    <div className="space-y-1.5">
      <Input
        id={id}
        className="h-8 w-full max-w-sm font-mono text-xs"
        placeholder="every 4 hours — or 17 */4 * * *"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
      />
      {trimmed === "" ? (
        <p className="text-xs text-muted-foreground">
          Describe it in plain English — “every 30 minutes”, “every 6 hours”,
          “nightly at 3:30am”, “mondays at 9pm” — or type a cron expression if
          you already know one.
        </p>
      ) : cron ? (
        <p className="text-xs text-muted-foreground">
          {description ? `${description}. ` : ""}
          Saved as <span className="font-mono">{cron}</span>
          {cron !== trimmed ? " — press Enter to use it." : "."}
        </p>
      ) : (
        <p className="text-xs text-destructive">
          Not a schedule we recognise. Try “every 4 hours” or “nightly at 3am” —
          or a five-field cron expression like{" "}
          <span className="font-mono">0 */4 * * *</span>.
        </p>
      )}
    </div>
  );
}
