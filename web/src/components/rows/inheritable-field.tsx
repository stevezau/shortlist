import type { ReactNode } from "react";

import { GlobalDefaultToggle } from "@/components/rows/global-default-row";
import { Label } from "@/components/ui/label";
import type { RowSettingKey } from "@/lib/row-kinds";

/**
 * One "leave on the global default, or override it here" field in the row editor.
 *
 * The same shape is used for every inheritable dial (already-watched cap, cadence, recent-watches,
 * watch count, cold start…): a label, a description, the `GlobalDefaultToggle`, and the field itself
 * once the row overrides it. Each call site states only what's different — its copy and its control.
 */
export function InheritableField({
  setting,
  label,
  labelFor,
  description,
  ariaLabel,
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
  description: ReactNode;
  ariaLabel: string;
  inheriting: boolean;
  globalValue: string | null;
  onToggle: (usesGlobal: boolean) => void;
  /** Why the "use the global default" toggle can't be used on this row; null when it can. */
  toggleDisabledReason?: string | null;
  /** Extra content between the description and the toggle (the {top_seed} warning). */
  before?: ReactNode;
  /** Extra content after the field, shown regardless of inheriting (the unstarted-only switch). */
  after?: ReactNode;
  /** The control shown once the row overrides the global. */
  children: ReactNode;
}) {
  return (
    <div data-setting={setting} className="space-y-3 border-t pt-4">
      {labelFor ? (
        <Label htmlFor={labelFor}>{label}</Label>
      ) : (
        <p className="text-sm font-medium">{label}</p>
      )}
      <p className="text-sm text-muted-foreground">{description}</p>
      {before}
      <GlobalDefaultToggle
        ariaLabel={ariaLabel}
        inheriting={inheriting}
        globalValue={globalValue}
        settingsHash="recommendations"
        onChange={onToggle}
        disabledReason={toggleDisabledReason}
      />
      {!inheriting && children}
      {after}
    </div>
  );
}
