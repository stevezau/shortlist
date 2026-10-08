import { type Dispatch, type SetStateAction } from "react";

export type AssistantAccessRole = "view" | "manage";

/** One decision for local connections and new OAuth grants. */
export function AssistantPermissionFields({ role, setRole, limitedScopes = false, readOnlyScopes = false, manageAvailable = true }: {
  role: AssistantAccessRole | null;
  setRole: Dispatch<SetStateAction<AssistantAccessRole | null>>;
  limitedScopes?: boolean;
  readOnlyScopes?: boolean;
  manageAvailable?: boolean;
}) {
  return <fieldset className="space-y-3">
    <legend className="mb-2 font-semibold">Access</legend>
    <label className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-3 text-sm has-[:checked]:border-primary">
      <input type="radio" name="assistant-access-role" value="view" checked={role === "view"} onChange={() => setRole("view")} className="mt-0.5 h-4 w-4 shrink-0 accent-primary" />
      <span><span className="block font-medium">View only</span><span className="block text-muted-foreground">See Shortlist details without changing anything or starting runs.</span></span>
    </label>
    <label className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-3 text-sm has-[:checked]:border-primary has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-60">
      <input type="radio" name="assistant-access-role" value="manage" checked={role === "manage"} disabled={!manageAvailable} onChange={() => setRole("manage")} className="mt-0.5 h-4 w-4 shrink-0 accent-primary" />
      <span><span className="block font-medium">Manage Shortlist</span><span className="block text-muted-foreground">Manage rows and settings, send missing-title requests, and start runs using your configured services.</span></span>
    </label>
    <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
      {role === "manage"
        ? "Runs may incur provider charges under your Shortlist settings. The assistant can use viewing history and services you configure now or later. Passwords and API keys stay in Shortlist."
        : role === "view" ? "The assistant can read viewing history and the details you allow it to see. Passwords and API keys stay in Shortlist." : "Choose the access to give this connection. Its current access remains in place until you save."}
    </p>
    {readOnlyScopes && <p className="max-w-prose text-xs text-muted-foreground">This client requested read-only access. It cannot change your setup through this sign-in.</p>}
    {!readOnlyScopes && limitedScopes && <p className="max-w-prose text-xs text-muted-foreground">This client requested limited permissions. It can use only those permissions through this sign-in, even if you choose Manage Shortlist.</p>}
  </fieldset>;
}
