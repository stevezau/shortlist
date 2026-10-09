import { useId, type ReactNode } from "react";
import { Link } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";

/**
 * One inheritable setting's line: its name, what it is set to, whether this row overrides it, and
 * the switch that decides.
 *
 * The global's ACTUAL value is spelled out while the row follows it. Without the value, a row that
 * inherits tells you only that it inherits — you would have to leave the page, find the setting, and
 * come back to learn what you agreed to. The link is for changing it, not for finding out what it is.
 */
export function GlobalDefaultToggle({
  heading,
  label = "Use the server default",
  ariaLabel,
  inheriting,
  globalValue,
  settingsHash,
  onChange,
  disabledReason = null,
}: {
  /** The setting's name, which leads the line. */
  heading: ReactNode;
  /** What the switch says it does. */
  label?: string;
  ariaLabel: string;
  inheriting: boolean;
  /** The resolved global, already formatted for reading ("0% — never re-suggest"). Null while
   *  settings are still loading, which renders the toggle with no claim about the value. */
  globalValue: string | null;
  /** Anchor on the settings page, e.g. "recommendations". */
  settingsHash: string;
  onChange: (inheriting: boolean) => void;
  /** Why the toggle can't be used on this row right now; null when it can. */
  disabledReason?: string | null;
}) {
  const reasonId = useId();
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 text-sm font-medium sm:w-52">{heading}</div>
        {inheriting && globalValue !== null && (
          <span className="min-w-0 text-sm">
            <strong className="font-normal">{globalValue}</strong>
          </span>
        )}
        <Badge variant={inheriting ? "outline" : "default"} className="font-normal text-muted-foreground">
          {inheriting ? "server default" : "overridden here"}
        </Badge>
        <span className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
          {inheriting && (
            <Link
              to={`/settings#${settingsHash}`}
              className="underline underline-offset-2 hover:text-foreground"
            >
              Change the default
            </Link>
          )}
          <span aria-hidden="true">{label}</span>
          <Switch
            checked={inheriting}
            onCheckedChange={onChange}
            aria-label={ariaLabel}
            disabled={disabledReason !== null}
            aria-describedby={disabledReason !== null ? reasonId : undefined}
          />
        </span>
      </div>
      {disabledReason !== null && (
        <p id={reasonId} className="text-xs text-muted-foreground">
          {disabledReason}
        </p>
      )}
    </div>
  );
}
