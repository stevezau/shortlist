import type { ComponentPropsWithoutRef } from "react";
import { useId } from "react";

import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

/**
 * A `Switch` that can be closed off for a REASON, and says that reason to a mouse, a keyboard and a
 * screen reader alike.
 *
 * `aria-disabled`, never the native `disabled` attribute: a natively disabled control drops out of
 * the tab order, so its `title` and its `aria-describedby` are unreachable by anyone not holding a
 * mouse. `onCheckedChange` still needs the no-op guard — `aria-disabled` is advisory and does not
 * stop Radix firing the real event.
 *
 * Only for a precondition that is impossible right now no matter what else is configured (a Plex
 * setting Shortlist cannot write, an incompatible combination). A feature that would merely run in
 * a degraded way until it is set up should stay switchable and explain itself afterwards — see
 * `AiWebSearchCard` and `RecommendationsSection`, which do that deliberately; do not route those
 * through this component.
 */
export function GatedSwitch({
  checked,
  onCheckedChange,
  reason,
  className,
  ...props
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /** Why this cannot be turned on right now, and what to do about it. Omit for a normal switch. */
  reason?: string;
} & Omit<
  ComponentPropsWithoutRef<typeof Switch>,
  "checked" | "onCheckedChange" | "disabled"
>) {
  const reasonId = useId();
  return (
    <>
      <Switch
        checked={checked}
        onCheckedChange={reason ? () => {} : onCheckedChange}
        aria-disabled={reason ? true : undefined}
        aria-describedby={reason ? reasonId : undefined}
        title={reason}
        className={cn(reason && "cursor-not-allowed opacity-40", className)}
        {...props}
      />
      {reason && (
        <span id={reasonId} className="sr-only">
          {reason}
        </span>
      )}
    </>
  );
}
