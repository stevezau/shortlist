import { Link } from "react-router";

import { Switch } from "@/components/ui/switch";

/**
 * The "Use the global default" toggle, with the global's ACTUAL value spelled out.
 *
 * Without the value, a row that inherits tells you only that it inherits — you have to leave the
 * dialog, find the setting, and come back to learn what you agreed to. Naming it here is the whole
 * point of this component; the link is for changing it, not for finding out what it is.
 */
export function GlobalDefaultToggle({
  label,
  ariaLabel,
  inheriting,
  globalValue,
  settingsHash,
  onChange,
}: {
  label?: string;
  ariaLabel: string;
  inheriting: boolean;
  /** The resolved global, already formatted for reading ("0% — never re-suggest"). Null while
   *  settings are still loading, which renders the toggle with no claim about the value. */
  globalValue: string | null;
  /** Anchor on the settings page, e.g. "recommendations". */
  settingsHash: string;
  onChange: (inheriting: boolean) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-4">
        <span className="text-sm">{label ?? "Use the global default"}</span>
        <Switch
          checked={inheriting}
          onCheckedChange={onChange}
          aria-label={ariaLabel}
        />
      </div>
      {inheriting && globalValue !== null && (
        <p className="text-xs text-muted-foreground">
          Currently <strong className="text-foreground">{globalValue}</strong>.{" "}
          <Link
            to={`/settings#${settingsHash}`}
            className="underline underline-offset-2 hover:text-foreground"
          >
            Change the global default
          </Link>
        </p>
      )}
    </div>
  );
}
