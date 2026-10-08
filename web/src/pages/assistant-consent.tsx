import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Logo } from "@/components/brand";
import { AssistantPermissionFields, type PaidDecision } from "@/components/assistant-permissions";
import { PlexPinButton } from "@/components/plex-pin-button";
import { ErrorState } from "@/components/query-boundary";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { queryKeys, useSession } from "@/lib/queries";
import type { AssistantConsentFlow, AssistantGrant } from "@/lib/types";

const READ_ONLY_SCOPES = new Set([
  "instance.read", "config.read", "catalog.read", "people.read", "activity.read",
  "history.use", "history.export", "requests.read",
]);

function ConsentLoading() {
  return <main className="mx-auto flex min-h-screen max-w-2xl items-center px-4 py-10"><Skeleton className="h-96 w-full" /></main>;
}

function ConsentFrame({ children }: { children: ReactNode }) {
  return <main className="mx-auto flex min-h-screen max-w-2xl items-center px-4 py-10">
    <div className="w-full space-y-6">
      <div className="flex items-center gap-3"><Logo size="sm" /><div><p className="font-semibold">Shortlist</p><p className="text-xs text-muted-foreground">Assistant authorization</p></div></div>
      {children}
    </div>
  </main>;
}

export function AssistantConsentPage() {
  const queryClient = useQueryClient();
  const session = useSession();
  const initialized = useRef(false);
  const [flow, setFlow] = useState<AssistantConsentFlow | null>(null);
  const [selectedGrant, setSelectedGrant] = useState<string | null>(null);
  const [paid, setPaid] = useState<PaidDecision>({ enabled: false, limit: "0", changed: false });
  const params = useMemo(() => Object.fromEntries(new URLSearchParams(window.location.search)), []);
  const grants = useQuery({ queryKey: ["assistant", "grants", flow?.client.id], queryFn: api.getAssistantGrants, enabled: flow !== null });
  const status = useQuery({ queryKey: ["assistant", "status"], queryFn: api.getAssistantStatus, enabled: flow !== null });
  const begin = useMutation({ mutationFn: () => api.beginAssistantConsent(params), onSuccess: setFlow });
  const decide = useMutation({
    mutationFn: async (approved: boolean) => {
      if (!flow) throw new Error("The authorization request is not ready.");
      const compatible = (grants.data ?? []).filter((grant) =>
        !grant.revoked_at && !grant.requires_access_approval && grant.client_id === flow.client.id,
      );
      let grantId = selectedGrant ?? compatible[0]?.id ?? "new";
      if (approved && grantId === "new") {
        const created = await api.createAssistantGrant({
          client_id: flow.client.id,
          name: flow.client.name,
          preset: "owner_automation",
          owner_managed: true,
          capabilities: flow.requested_scopes,
          constraints: { max_provider_calls: paid.enabled ? Number(paid.limit) : 0 },
        });
        grantId = created.id;
      }
      return api.decideAssistantConsent({
        flow_id: flow.flow_id,
        csrf_token: flow.csrf_token,
        approved,
        grant_id: approved ? grantId : null,
      });
    },
    onSuccess: ({ redirect_to }) => window.location.assign(redirect_to),
  });

  useEffect(() => {
    if (!session.data?.authenticated || initialized.current) return;
    initialized.current = true;
    begin.mutate();
  }, [begin, session.data?.authenticated]);

  if (session.isPending) return <ConsentLoading />;
  if (session.isError) return <ConsentFrame><ErrorState error={session.error} onRetry={() => void session.refetch()} /></ConsentFrame>;
  if (!session.data.authenticated) return <ConsentFrame><Card>
    <CardHeader><CardTitle>Sign in to review this connection</CardTitle><CardDescription>Use the Plex account that owns this Shortlist server. The assistant never receives your Plex token.</CardDescription></CardHeader>
    <CardContent><PlexPinButton onLinked={() => void queryClient.invalidateQueries({ queryKey: queryKeys.session })} /></CardContent>
  </Card></ConsentFrame>;
  if (begin.isPending || (!begin.isError && !flow)) return <ConsentLoading />;
  if (begin.isError) return <ConsentFrame><ErrorState error={begin.error} onRetry={() => { initialized.current = true; begin.mutate(); }} /></ConsentFrame>;
  if (!flow) return <ConsentLoading />;

  const compatible = (grants.data ?? []).filter((grant: AssistantGrant) =>
    !grant.revoked_at && !grant.requires_access_approval && grant.client_id === flow.client.id,
  );
  const effectiveGrant = selectedGrant ?? compatible[0]?.id ?? "new";
  const selectedExisting = compatible.find((grant) => grant.id === effectiveGrant);
  const baseProfileScopes = new Set([
    ...(status.data?.presets.owner_automation ?? []),
    "history.export", "history.providers", "requests.send", "maintenance.execute",
  ]);
  const limitedScopes = [...baseProfileScopes].some((scope) => !flow.requested_scopes.includes(scope));
  const readOnlyScopes = flow.requested_scopes.every((scope) => READ_ONLY_SCOPES.has(scope));
  const profileScopes = new Set([...baseProfileScopes, ...(paid.enabled ? ["ai.generate"] : [])]);
  const approvedScopes = flow.requested_scopes.filter((scope) =>
    selectedExisting ? selectedExisting.capabilities.includes(scope) : profileScopes.has(scope),
  );
  const ready = !grants.isPending && !status.isPending && !grants.isError && !status.isError;
  return <ConsentFrame><Card>
    <CardHeader><div className="flex items-start gap-3"><ShieldAlert aria-hidden="true" className="mt-0.5 h-5 w-5 text-warning" /><div><CardTitle>{flow.client.name} wants to connect</CardTitle><CardDescription className="mt-1">Review its access before you allow it.</CardDescription></div></div></CardHeader>
    <CardContent><form className="space-y-5" onSubmit={(event) => { event.preventDefault(); decide.mutate(true); }}>
      {compatible.length > 0 && <section className="space-y-2"><h2 className="text-sm font-semibold">Connection</h2>
        {compatible.map((grant) => <label key={grant.id} className="flex cursor-pointer items-center justify-between gap-3 rounded-md border px-3 py-3"><span><span className="block text-sm font-medium">{grant.name}</span><span className="text-xs text-muted-foreground">{grant.full_management ? "Full Shortlist access" : "Existing limited access"} · Expires {grant.expires_at ? formatDate(grant.expires_at) : "never"}</span></span><input type="radio" name="grant" checked={effectiveGrant === grant.id} onChange={() => setSelectedGrant(grant.id)} className="h-4 w-4 accent-primary" /></label>)}
        <label className="flex cursor-pointer items-center justify-between gap-3 rounded-md border px-3 py-3 text-sm"><span>Create a new connection</span><input type="radio" name="grant" checked={effectiveGrant === "new"} onChange={() => setSelectedGrant("new")} className="h-4 w-4 accent-primary" /></label>
      </section>}
      {selectedExisting ? <p className="text-sm text-muted-foreground">{readOnlyScopes ? "This sign-in is read-only. Your saved connection keeps its existing access." : "This sign-in can use only the permissions the client requested. Your saved connection keeps its existing access."} Approving this sign-in does not upgrade the connection.</p> : <AssistantPermissionFields paid={paid} setPaid={setPaid} paidAvailable={flow.requested_scopes.includes("ai.generate")} limitedScopes={limitedScopes} readOnlyScopes={readOnlyScopes} />}
      {approvedScopes.length === 0 && <p className="text-xs text-muted-foreground">This connection cannot use any permission it requested.</p>}
      {(status.isError || grants.isError) && <p role="alert" className="text-sm text-destructive-text">Could not load current connection choices. Reload this page and try again.</p>}
      {decide.isError && <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(decide.error, "Could not finish this authorization request.")}</p>}
      <div className="flex flex-wrap justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" loading={decide.isPending} onClick={() => decide.mutate(false)}>Deny</Button><Button type="submit" disabled={!ready || approvedScopes.length === 0} loading={decide.isPending}>Allow connection</Button></div>
    </form></CardContent>
  </Card></ConsentFrame>;
}
