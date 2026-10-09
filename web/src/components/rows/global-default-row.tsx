import { useId, type ReactNode } from "react";
import { Link } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * One inheritable setting's line: its name, what it is set to, whether this row overrides it, and
 * the button that switches between the two.
 *
 * The global's ACTUAL value is spelled out while the row follows it. Without the value, a row that
 * inherits tells you only that it inherits — you would have to leave the page, find the setting, and
 * come back to learn what you agreed to. The link is for changing it, not for finding out what it is.
 */
export function GlobalDefaultToggle({
  heading,
  name,
  inheriting,
  globalValue,
  settingsHash,
  onChange,
  disabledReason = null,
}: {
  /** The setting's name, which leads the line. */
  heading: ReactNode;
  /** The same name as plain text, for the button's accessible name. */
  name: string;
  inheriting: boolean;
  /** The resolved global, already formatted for reading ("0% — never re-suggest"). Null while
   *  settings are still loading, which renders the line with no claim about the value. */
  globalValue: string | null;
  /** Anchor on the settings page, e.g. "recommendations". */
  settingsHash: string;
  /** True to follow the global again (Reset), false to set the row's own (Override). */
  onChange: (inheriting: boolean) => void;
  /** Why the row can't go back to the global right now; null when it can. */
  disabledReason?: string | null;
}) {
  const reasonId = useId();
  const verb = inheriting ? "Override" : "Reset";
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 text-sm font-medium sm:w-52">{heading}</div>
        {inheriting && globalValue !== null && (
          <span className="min-w-0 text-sm">{globalValue}</span>
        )}
        <Badge variant={inheriting ? "outline" : "default"} className="font-normal text-muted-foreground">
          {inheriting ? "server default" : "overridden here"}
        </Badge>
        <span className="ml-auto flex items-center gap-3">
          {inheriting && (
            <Link
              to={`/settings#${settingsHash}`}
              className="text-sm text-muted-foreground underline underline-offset-2 hover:text-foreground"
            >
              Change the default
            </Link>
          )}
          <Button
            type="button"
            variant={inheriting ? "outline" : "ghost"}
            size="sm"
            aria-label={`${verb} ${name}`}
            disabled={disabledReason !== null}
            aria-describedby={disabledReason !== null ? reasonId : undefined}
            onClick={() => onChange(!inheriting)}
          >
            {verb}
          </Button>
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
