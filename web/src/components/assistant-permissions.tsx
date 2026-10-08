import { useId, type Dispatch, type SetStateAction } from "react";

import { Input } from "@/components/ui/input";

export interface PaidDecision {
  enabled: boolean;
  limit: string;
  changed: boolean;
}

/** The same owner decision is shown for local and OAuth connections. */
export function AssistantPermissionFields({
  paid,
  setPaid,
  reserved = 0,
  paidAvailable = true,
  limitedScopes = false,
  readOnlyScopes = false,
}: {
  paid: PaidDecision;
  setPaid: Dispatch<SetStateAction<PaidDecision>>;
  reserved?: number;
  paidAvailable?: boolean;
  limitedScopes?: boolean;
  readOnlyScopes?: boolean;
}) {
  const helpId = useId();
  const limitId = useId();
  const remaining = Math.max(0, Number(paid.limit) - reserved);
  return (
    <div className="space-y-5">
      <div className="max-w-prose space-y-2 text-sm leading-relaxed">
        {readOnlyScopes ? <>
          <p>This assistant requested read-only access to Shortlist. It cannot change your setup.</p>
          <p className="text-muted-foreground">It can see only the information it requested. Passwords and API keys stay in Shortlist.</p>
        </> : limitedScopes ? <>
          <p>Allow this assistant to use the Shortlist permissions it requested. It cannot gain more through this approval.</p>
          <p className="text-muted-foreground">Requested permissions may include managing rows, viewing details, settings, missing-title requests, and using services you configure now or later. Passwords and API keys stay in Shortlist.</p>
        </> : <>
          <p>Allow this assistant to manage Shortlist, including everyone’s rows and viewing details, settings, and missing-title requests.</p>
          <p className="text-muted-foreground">It can use the services you configure now or later, including sending relevant viewing details to them. Passwords and API keys stay in Shortlist.</p>
        </>}
      </div>
      {paidAvailable && <div className="space-y-2 border-t pt-4">
        <label className="flex items-start gap-3 text-sm">
          <input
            type="checkbox"
            checked={paid.enabled}
            disabled={!paidAvailable || (!paid.enabled && reserved >= 100)}
            onChange={(event) => setPaid({
              enabled: event.target.checked,
              limit: event.target.checked ? String(Math.max(1, reserved + 1)) : "0",
              changed: true,
            })}
            className="mt-0.5 h-4 w-4 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <span className="font-medium">Allow this assistant to use paid services</span>
        </label>
        {paid.enabled && <div className="max-w-xs space-y-2 pl-7 text-sm">
          <label htmlFor={limitId} className="block font-medium">Lifetime call allowance</label>
          <Input
            id={limitId}
            aria-describedby={helpId}
            type="number"
            required
            min={paid.changed ? reserved + 1 : 0}
            max={100}
            step={1}
            value={paid.limit}
            onChange={(event) => setPaid((current) => ({ ...current, limit: event.target.value, changed: true }))}
          />
          <p className="text-xs text-muted-foreground">Used or uncertain: {reserved} · Remaining now: {remaining}{remaining === 0 ? " · Exhausted" : ""}</p>
        </div>}
        {!paid.enabled && reserved > 0 && <p className="pl-7 text-xs text-muted-foreground">Used or uncertain: {reserved} · Remaining: 0</p>}
        <p id={helpId} className="max-w-prose pl-7 text-xs leading-relaxed text-muted-foreground">Direct AI, search and image calls use this finite allowance. Saved recurring Shortlist runs use their existing settings and may incur charges separately.</p>
      </div>}
    </div>
  );
}
