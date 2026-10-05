import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Logo } from "@/components/brand";
import { PlexPinButton } from "@/components/plex-pin-button";
import { ErrorState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { queryKeys, useSession } from "@/lib/queries";
import type { AssistantConsentFlow, AssistantGrant, AssistantGrantConstraints } from "@/lib/types";

const SCOPE_DESCRIPTIONS: Record<string, string> = {
  "instance.read": "See the Shortlist version, setup state, safe mode, and aggregate health.",
  "config.read": "Read non-secret configuration and whether integrations are configured.",
  "catalog.read": "Read supported settings, row templates, and product guides.",
  "people.read": "See approved people and their directory display names.",
  "activity.read": "Read approved job, run, and diagnostic summaries.",
  "changes.prepare": "Prepare bounded changes for standing authorization or owner review.",
  "history.use": "Use approved viewing history locally for recommendations.",
  "history.providers": "Send history-derived context to exact approved provider destinations.",
  "history.export": "Return viewing details and sensitive explanations to this assistant.",
  "rows.create": "Create rows inside approved people and libraries.",
  "rows.update": "Change approved rows.",
  "rows.activate": "Turn approved rows on or off.",
  "rows.delete": "Delete approved rows through reviewed Shortlist operations.",
  "audiences.write": "Change who receives approved rows, including required privacy reconciliation.",
  "themes.write": "Create or edit approved themes and supplied title picks.",
  "seasons.write": "Create or edit seasonal definitions used by approved rows.",
  "config.write": "Change approved non-secret setting groups.",
  "people.write": "Change approved people settings; parental controls stay owner-only.",
  "schedules.write": "Create or change schedules that may continue after this connection expires.",
  "connections.manage": "Configure supported integrations without reading saved credential values.",
  "runs.preview": "Preview approved rows without delivering them.",
  "runs.execute": "Run and deliver approved rows to Plex.",
  "jobs.cancel": "Cancel eligible background work started through approved operations.",
  "ai.generate": "Spend from this connection's finite provider-call allowance.",
  "requests.read": "Read approved acquisition request status.",
  "requests.manage": "Prepare and manage approved acquisition requests.",
  "requests.send": "Send approved requests to the configured request service.",
  "maintenance.execute": "Run an exact owner-approved protected maintenance operation.",
};

function emptyConstraints(): AssistantGrantConstraints {
  return {
    row_ids: [], person_ids: [], library_keys: [], setting_groups: [], destination_ids: [],
    include_future_rows: false, include_future_people: false, include_future_libraries: false,
    max_batch_size: 25, max_work_per_operation: 100, max_provider_calls: 0,
  };
}

function ConsentLoading() {
  return <main className="mx-auto flex min-h-screen max-w-2xl items-center px-4 py-10"><Skeleton className="h-96 w-full" /></main>;
}

function ConsentFrame({ children }: { children: ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl items-center px-4 py-10">
      <div className="w-full space-y-6">
        <div className="flex items-center gap-3"><Logo size="sm" /><div><p className="font-semibold">Shortlist</p><p className="text-xs text-muted-foreground">Assistant authorization</p></div></div>
        {children}
      </div>
    </main>
  );
}

export function AssistantConsentPage() {
  const queryClient = useQueryClient();
  const session = useSession();
  const initialized = useRef(false);
  const [flow, setFlow] = useState<AssistantConsentFlow | null>(null);
  const [selectedGrant, setSelectedGrant] = useState<string | null>(null);
  const params = useMemo(() => Object.fromEntries(new URLSearchParams(window.location.search)), []);
  const grants = useQuery({
    queryKey: ["assistant", "grants", flow?.client.id],
    queryFn: api.getAssistantGrants,
    enabled: flow !== null,
  });
  const begin = useMutation({
    mutationFn: () => api.beginAssistantConsent(params),
    onSuccess: setFlow,
  });
  const decide = useMutation({
    mutationFn: async (approved: boolean) => {
      if (!flow) throw new Error("The authorization request is not ready.");
      let grantId = selectedGrant ?? grants.data?.find((grant) =>
        !grant.revoked_at && grant.client_id === flow.client.id &&
        flow.requested_scopes.every((scope) => grant.capabilities.includes(scope)),
      )?.id ?? null;
      if (approved && !grantId) {
        const created = await api.createAssistantGrant({
          client_id: flow.client.id,
          name: flow.client.name,
          preset: "inspect",
          capabilities: flow.requested_scopes,
          constraints: emptyConstraints(),
          expires_in_days: 90,
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
  if (!session.data.authenticated) {
    return (
      <ConsentFrame>
        <Card>
          <CardHeader><CardTitle>Sign in to review this connection</CardTitle><CardDescription>Use the Plex account that owns this Shortlist server. The assistant never receives your Plex token.</CardDescription></CardHeader>
          <CardContent><PlexPinButton onLinked={() => void queryClient.invalidateQueries({ queryKey: queryKeys.session })} /></CardContent>
        </Card>
      </ConsentFrame>
    );
  }
  if (begin.isPending || (!begin.isError && !flow)) return <ConsentLoading />;
  if (begin.isError) return <ConsentFrame><ErrorState error={begin.error} onRetry={() => { initialized.current = true; begin.mutate(); }} /></ConsentFrame>;
  if (!flow) return <ConsentLoading />;

  const compatible = (grants.data ?? []).filter((grant: AssistantGrant) =>
    !grant.revoked_at && grant.client_id === flow.client.id &&
    flow.requested_scopes.every((scope) => grant.capabilities.includes(scope)),
  );
  const effectiveGrant = selectedGrant ?? compatible[0]?.id ?? null;

  return (
    <ConsentFrame>
      <Card>
        <CardHeader>
          <div className="flex items-start gap-3"><ShieldAlert aria-hidden="true" className="mt-0.5 h-5 w-5 text-warning" /><div><CardTitle>{flow.client.name} wants to connect</CardTitle><CardDescription className="mt-1">Approve this exact resource and permission set. You can revoke the connection from Assistant access.</CardDescription></div></div>
        </CardHeader>
        <CardContent className="space-y-6">
          <section className="space-y-2"><h2 className="text-sm font-semibold">Resource</h2><code className="block break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">{flow.resource}</code></section>
          <section className="space-y-2">
            <h2 className="text-sm font-semibold">Requested permissions</h2>
            <div className="divide-y rounded-md border">
              {flow.requested_scopes.map((scope) => <div key={scope} className="px-3 py-3"><div className="flex items-center gap-2"><CheckCircle2 aria-hidden="true" className="h-4 w-4 text-success" /><p className="text-sm font-medium">{scope}</p></div><p className="mt-1 pl-6 text-sm text-muted-foreground">{SCOPE_DESCRIPTIONS[scope] ?? "Use this named Shortlist permission."}</p></div>)}
            </div>
          </section>
          {compatible.length > 0 && (
            <section className="space-y-2"><h2 className="text-sm font-semibold">Connection grant</h2>{compatible.map((grant) => <label key={grant.id} className="flex cursor-pointer items-center justify-between gap-3 rounded-md border px-3 py-3"><span><span className="block text-sm font-medium">{grant.name}</span><span className="text-xs text-muted-foreground">Expires {grant.expires_at ? formatDate(grant.expires_at) : "never"}</span></span><input type="radio" name="grant" checked={effectiveGrant === grant.id} onChange={() => setSelectedGrant(grant.id)} className="h-4 w-4 accent-primary" /></label>)}</section>
          )}
          {compatible.length === 0 && !grants.isPending && <p className="rounded-md border bg-elevated px-3 py-3 text-sm text-muted-foreground">Approving creates a new 90-day grant for this client and these permissions. Resource-bound actions remain unavailable until you add people, rows, libraries, or destinations on the Assistant access page.</p>}
          {decide.isError && <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(decide.error, "Could not finish this authorization request.")}</p>}
          <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
            <Button variant="outline" loading={decide.isPending} onClick={() => decide.mutate(false)}>Deny</Button>
            <Button loading={decide.isPending || grants.isPending} onClick={() => decide.mutate(true)}>Allow connection</Button>
          </div>
          <div className="flex flex-wrap gap-1.5">{flow.requested_scopes.map((scope) => <Badge key={scope} variant="outline">{scope}</Badge>)}</div>
        </CardContent>
      </Card>
    </ConsentFrame>
  );
}
