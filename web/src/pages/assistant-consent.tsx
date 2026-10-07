import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Logo } from "@/components/brand";
import { AssistantPermissionFields } from "@/components/assistant-permissions";
import { baselineCapabilities, selectedDestinationSnapshots, type AssistantMode, type SelectedDestination } from "@/lib/assistant-permission-model";
import { PlexPinButton } from "@/components/plex-pin-button";
import { ErrorState } from "@/components/query-boundary";
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
    row_ids: [], library_keys: [], setting_groups: [], destination_ids: [],
    include_future_rows: true, include_future_libraries: true,
    max_batch_size: 25, max_work_per_operation: null, max_provider_calls: 0,
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
  const [mode, setMode] = useState<AssistantMode>("suggest");
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [constraints, setConstraints] = useState<AssistantGrantConstraints>(emptyConstraints);
  const [selectedDestinations, setSelectedDestinations] = useState<SelectedDestination[]>([]);
  const initializedPermissions = useRef(false);
  const params = useMemo(() => Object.fromEntries(new URLSearchParams(window.location.search)), []);
  const grants = useQuery({
    queryKey: ["assistant", "grants", flow?.client.id],
    queryFn: api.getAssistantGrants,
    enabled: flow !== null,
  });
  const status = useQuery({ queryKey: ["assistant", "status"], queryFn: api.getAssistantStatus, enabled: flow !== null });
  const choices = useQuery({ queryKey: ["assistant", "destinations"], queryFn: api.getAssistantDestinations, enabled: flow !== null });
  const begin = useMutation({
    mutationFn: () => api.beginAssistantConsent(params),
    onSuccess: setFlow,
  });
  const decide = useMutation({
    mutationFn: async (approved: boolean) => {
      if (!flow) throw new Error("The authorization request is not ready.");
      const compatible = (grants.data ?? []).filter((grant) =>
        !grant.revoked_at && !grant.requires_access_approval && grant.client_id === flow.client.id,
      );
      let grantId = selectedGrant ?? compatible[0]?.id ?? "new";
      if (approved && grantId === "new") {
        const approvedCapabilities = capabilities.filter((capability) => flow.requested_scopes.includes(capability));
        const created = await api.createAssistantGrant({
          client_id: flow.client.id,
          name: flow.client.name,
          preset: mode === "manage" ? "owner_automation" : "inspect",
          capabilities: approvedCapabilities,
          constraints,
          selected_destinations: selectedDestinationSnapshots(constraints.destination_ids, [], selectedDestinations),
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

  useEffect(() => {
    if (!flow || !status.data || initializedPermissions.current) return;
    initializedPermissions.current = true;
    const suggest = status.data.presets.inspect;
    const manage = status.data.presets.owner_automation;
    const nextMode = flow.requested_scopes.some((scope) => manage.includes(scope) && !suggest.includes(scope)) ? "manage" : "suggest";
    setMode(nextMode);
    setCapabilities(baselineCapabilities(status.data, nextMode));
    setConstraints({ ...emptyConstraints(), setting_groups: nextMode === "manage" ? [...status.data.setting_groups] : [] });
  }, [flow, status.data]);

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
    !grant.revoked_at && !grant.requires_access_approval && grant.client_id === flow.client.id,
  );
  const effectiveGrant = selectedGrant ?? compatible[0]?.id ?? "new";
  const selectedExisting = compatible.find((grant) => grant.id === effectiveGrant);
  const chosenCapabilities = selectedExisting?.capabilities ?? capabilities;
  const approvedScopes = flow.requested_scopes.filter((scope) => chosenCapabilities.includes(scope));
  const ready = !grants.isPending && !status.isPending && !choices.isPending && !status.isError && !choices.isError;

  return (
    <ConsentFrame>
      <Card>
        <CardHeader>
          <div className="flex items-start gap-3"><ShieldAlert aria-hidden="true" className="mt-0.5 h-5 w-5 text-warning" /><div><CardTitle>{flow.client.name} wants to connect</CardTitle><CardDescription className="mt-1">Choose what this connection can do. Shortlist grants only permissions this assistant requested.</CardDescription></div></div>
        </CardHeader>
        <CardContent>
          <form className="space-y-6" onSubmit={(event) => { event.preventDefault(); decide.mutate(true); }}>
          {compatible.length > 0 && (
            <section className="space-y-2"><h2 className="text-sm font-semibold">Connection grant</h2>{compatible.map((grant) => <label key={grant.id} className="flex cursor-pointer items-center justify-between gap-3 rounded-md border px-3 py-3"><span><span className="block text-sm font-medium">{grant.name}</span><span className="text-xs text-muted-foreground">Expires {grant.expires_at ? formatDate(grant.expires_at) : "never"}</span></span><input type="radio" name="grant" checked={effectiveGrant === grant.id} onChange={() => setSelectedGrant(grant.id)} className="h-4 w-4 accent-primary" /></label>)}<label className="flex cursor-pointer items-center justify-between gap-3 rounded-md border px-3 py-3 text-sm"><span>Create a new connection</span><input type="radio" name="grant" checked={effectiveGrant === "new"} onChange={() => setSelectedGrant("new")} className="h-4 w-4 accent-primary" /></label></section>
          )}
          {selectedExisting ? <p className="text-sm text-muted-foreground">This grant keeps its current rows, libraries, settings and service approvals. {selectedExisting.constraints.include_future_rows ? "All current and future rows" : `${selectedExisting.constraints.row_ids.length} selected rows`}; {selectedExisting.constraints.include_future_libraries ? "all current and future libraries" : `${selectedExisting.constraints.library_keys.length} selected libraries`}.</p> : status.data && <AssistantPermissionFields status={status.data} mode={mode} setMode={setMode} capabilities={capabilities} setCapabilities={setCapabilities} constraints={constraints} setConstraints={setConstraints} choices={choices.data ?? []} setSelectedDestinations={setSelectedDestinations} requestedScopes={flow.requested_scopes} />}
          <p className="text-sm text-muted-foreground">{approvedScopes.length > 0 ? `This approval grants ${approvedScopes.length} of ${flow.requested_scopes.length} requested permissions.` : "No requested permissions match this choice. Choose another mode or grant."}</p>
          <details className="border-t pt-3 text-sm"><summary className="cursor-pointer text-primary">Advanced request details</summary><div className="mt-3 space-y-2"><p>Resource: <code className="break-all font-mono text-xs">{flow.resource}</code></p><p>Requested permissions</p>{flow.requested_scopes.map((scope) => <p key={scope} className="text-xs"><code>{scope}</code> — {SCOPE_DESCRIPTIONS[scope] ?? "Named Shortlist permission."}</p>)}</div></details>
          {(status.isError || choices.isError || grants.isError) && <p role="alert" className="text-sm text-destructive-text">Could not load current connection choices. Reload this page and try again.</p>}
          {decide.isError && <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(decide.error, "Could not finish this authorization request.")}</p>}
          <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
            <Button type="button" variant="outline" loading={decide.isPending} onClick={() => decide.mutate(false)}>Deny</Button>
            <Button type="submit" disabled={!ready || approvedScopes.length === 0} loading={decide.isPending}>Allow connection</Button>
          </div>
          </form>
        </CardContent>
      </Card>
    </ConsentFrame>
  );
}
