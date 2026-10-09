import type { CSSProperties, ReactNode } from "react";
import { createPortal } from "react-dom";

import { GlobalDefaultToggle } from "@/components/rows/global-default-row";
import { overrideOrder, useOverridesTarget } from "@/components/rows/overrides-target";
import { Label } from "@/components/ui/label";
import type { RowSettingKey } from "@/lib/row-kinds";
import { cn } from "@/lib/utils";

/**
 * One "leave on the server's default, or override it here" setting in the row editor.
 *
 * The same shape is used for every inheritable dial (already-watched cap, cadence, recent-watches,
 * watch count, cold start…): a one-line row with the setting, its server value and Override / Reset,
 * and the control itself once the row overrides it. Each call site states only what's different —
 * its copy and its control. The row goes into the editor's "Server defaults" list when there is one.
 */
export function InheritableField({
  setting,
  label,
  labelFor,
  description,
  inheriting,
  globalValue,
  onToggle,
  toggleDisabledReason = null,
  before,
  after,
  children,
}: {
  /** The setting this block is, for the editor's visibility contract (`row-kinds.ts`). */
  setting: RowSettingKey;
  label: string;
  /** Set only when the field it labels has a matching `id` — some of these fields (RecentCountField,
   *  MaxSeedsField) already wire their own internal `<Label>`, so this heading stays a plain string. */
  labelFor?: string;
  /** Shown once the row overrides the setting, above its control. */
  description: ReactNode;
  inheriting: boolean;
  globalValue: string | null;
  onToggle: (usesGlobal: boolean) => void;
  /** Why the row can't go back to the global; null when it can. */
  toggleDisabledReason?: string | null;
  /** Extra content between the line and the control (the {top_seed} warning). */
  before?: ReactNode;
  /** Extra content after the control, shown regardless of inheriting (the unstarted-only switch). */
  after?: ReactNode;
  /** The control shown once the row overrides the global. */
  children: ReactNode;
}) {
  const target = useOverridesTarget();
  const row = (
    <div
      data-setting={setting}
      data-override-row=""
      style={target ? ({ order: overrideOrder(setting) } satisfies CSSProperties) : undefined}
      className={cn("space-y-3 border-t py-3", target ? "px-5" : "pt-4", !inheriting && "bg-raised/40")}
    >
      <GlobalDefaultToggle
        heading={
          labelFor ? (
            <Label htmlFor={labelFor} className="font-medium">
              {label}
            </Label>
          ) : (
            label
          )
        }
        name={label}
        inheriting={inheriting}
        globalValue={globalValue}
        settingsHash="recommendations"
        onChange={onToggle}
        disabledReason={toggleDisabledReason}
      />
      {before}
      {!inheriting && (
        <>
          <p className="text-sm text-muted-foreground">{description}</p>
          {children}
        </>
      )}
      {after}
    </div>
  );
  return target ? createPortal(row, target) : row;
}
