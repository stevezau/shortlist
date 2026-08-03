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
    // Full width so that inside a wrapping row (the Tools frequency pickers) this drops onto its own
    // line rather than being squeezed beside the presets, where the hint would wrap to four lines.
    <div className="w-full space-y-1">
      <Input
        id={id}
        className="h-8 w-52 font-mono text-xs"
        placeholder="every 4 hours"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
      />
      {trimmed !== "" &&
        (cron ? (
          <p className="text-xs text-muted-foreground">
            {description || `Runs on ${cron}`}
            {cron !== trimmed && (
              <>
                {" "}
                — saved as <span className="font-mono">{cron}</span>
              </>
            )}
          </p>
        ) : (
          <p className="text-xs text-destructive-text">
            Not a schedule we recognise — this won’t be saved.
          </p>
        ))}
      {/* Always visible, never state-dependent: the box normally opens with a cron already in it,
          so a hint that only showed when empty was a hint nobody ever saw. */}
      <p className="text-xs text-muted-foreground">
        Type plain English (“every 30 minutes”, “mondays at 9pm”) or a cron
        expression.
      </p>
    </div>
  );
}
