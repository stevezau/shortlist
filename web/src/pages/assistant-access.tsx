import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Copy,
  KeyRound,
  Link2,
  Plus,
  ShieldCheck,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { PageHeader } from "@/components/page-header";
import { EmptyState, ErrorState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { useCollections, useLibraries, useUsers } from "@/lib/queries";
import type {
  AssistantGrant,
  AssistantGrantConstraints,
  AssistantGrantPreset,
} from "@/lib/types";
import { useCopy } from "@/lib/use-copy";

const assistantKeys = {
  status: ["assistant", "status"] as const,
  grants: ["assistant", "grants"] as const,
};

const PRESETS: Array<{ value: AssistantGrantPreset; title: string; description: string }> = [
  {
    value: "inspect",
    title: "Inspect and propose",
    description: "Read configuration and activity, then prepare changes for you to review in Shortlist.",
  },
  {
    value: "manage_selected_rows",
    title: "Manage selected rows",
    description: "Edit selected rows and create rows only in the people and libraries you choose.",
  },
  {
    value: "owner_automation",
    title: "Owner automation",
    description: "Operate approved settings, people, rows, schedules, and runs within the limits below.",
  },
];

const EXTRA_PERMISSIONS = [
  {
    value: "history.providers",
    title: "Send history-derived context to approved providers",
    description: "Allows summaries or search queries based on viewing history to reach only the exact destinations below.",
  },
  {
    value: "history.export",
    title: "Show viewing details to the connected assistant",
    description: "Allows watched titles, picks, and sensitive explanations to appear in tool results sent to this assistant.",
  },
  {
    value: "ai.generate",
    title: "Spend provider calls",
    description: "Allows Shortlist's configured AI provider to generate content, up to this connection's lifetime call allowance.",
  },
  {
    value: "requests.send",
    title: "Send acquisition requests",
    description: "Allows approved plans to send requests to a configured request service, within existing instance limits.",
  },
] as const;

function emptyConstraints(): AssistantGrantConstraints {
  return {
    row_ids: [],
    person_ids: [],
    library_keys: [],
    setting_groups: [],
    destination_ids: [],
    include_future_rows: false,
    include_future_people: false,
    include_future_libraries: false,
    max_batch_size: 25,
    max_work_per_operation: 100,
    max_provider_calls: 0,
  };
}

function toggle<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function Choice({ checked, title, description, onChange }: {
  checked: boolean;
  title: string;
  description?: string;
  onChange: () => void;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 py-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        className="mt-1 h-4 w-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <span className="min-w-0">
        <span className="block font-medium">{title}</span>
        {description && <span className="block text-muted-foreground">{description}</span>}
      </span>
    </label>
  );
}

function CredentialDialog({ value, expiresAt, onClose }: {
  value: string | null;
  expiresAt: string | null;
  onClose: () => void;
}) {
  const { state, copy } = useCopy();
  return (
    <Dialog open={value !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent>
        <DialogTitle>Copy this credential now</DialogTitle>
        <DialogDescription>
          Shortlist stores only a verifier. This credential cannot be shown again after you close this window.
        </DialogDescription>
        {value && (
          <div className="space-y-3">
            <code className="block break-all rounded-md border bg-muted p-3 font-mono text-xs">{value}</code>
            <Button type="button" className="w-full" onClick={() => copy(value)}>
              {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
              {state === "copied" ? "Copied" : "Copy credential"}
            </Button>
            {expiresAt && <p className="text-xs text-muted-foreground">Expires {formatDate(expiresAt)}.</p>}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>I saved it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function GrantRow({ grant, onCredential, onRevoke }: {
  grant: AssistantGrant;
  onCredential: (grant: AssistantGrant) => void;
  onRevoke: (grant: AssistantGrant) => void;
}) {
  const revoked = Boolean(grant.revoked_at);
  const expired = Boolean(grant.expires_at && new Date(grant.expires_at) <= new Date());
  const status = revoked ? "Revoked" : expired ? "Expired" : "Active";
  return (
    <article className="px-4 py-4 sm:px-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-semibold">{grant.name}</h2>
            <Badge variant={revoked || expired ? "secondary" : "success"}>{status}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            {PRESETS.find((preset) => preset.value === grant.preset)?.title ?? grant.preset}
            {grant.last_used_at ? ` · Used ${timeAgo(grant.last_used_at)}` : " · Never used"}
            {grant.expires_at ? ` · Expires ${formatDate(grant.expires_at)}` : " · No expiry"}
          </p>
        </div>
        {!revoked && !expired && (
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={() => onCredential(grant)}>
              <KeyRound aria-hidden="true" /> New local credential
            </Button>
            <Button variant="ghost" size="sm" className="text-destructive-text" onClick={() => onRevoke(grant)}>
              <XCircle aria-hidden="true" /> Revoke
            </Button>
          </div>
        )}
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {grant.capabilities.map((capability) => <Badge key={capability} variant="outline">{capability}</Badge>)}
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        {grant.constraints.person_ids.length} people · {grant.constraints.row_ids.length} rows ·{" "}
        {grant.constraints.library_keys.length} libraries · {grant.local_credential_count ?? 0} local credentials
      </p>
    </article>
  );
}

export function AssistantAccessPage() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: assistantKeys.status, queryFn: api.getAssistantStatus });
  const grants = useQuery({
    queryKey: assistantKeys.grants,
    queryFn: api.getAssistantGrants,
    enabled: status.data?.enabled === true,
  });
  const users = useUsers();
  const rows = useCollections();
  const libraries = useLibraries();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [preset, setPreset] = useState<AssistantGrantPreset>("inspect");
  const [constraints, setConstraints] = useState<AssistantGrantConstraints>(emptyConstraints);
  const [extras, setExtras] = useState<string[]>([]);
  const [expiresInDays, setExpiresInDays] = useState(90);
  const [destinations, setDestinations] = useState("");
  const [credential, setCredential] = useState<{ value: string; expiresAt: string } | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<AssistantGrant | null>(null);

  const capabilities = useMemo(() => {
    const base = status.data?.presets[preset] ?? [];
    return [...new Set([...base, ...extras])];
  }, [extras, preset, status.data]);

  const create = useMutation({
    mutationFn: () => api.createAssistantGrant({
      client_id: `local-${crypto.randomUUID()}`,
      name: name.trim(),
      preset,
      capabilities,
      constraints: {
        ...constraints,
        destination_ids: destinations.split("\n").map((value) => value.trim()).filter(Boolean),
      },
      expires_in_days: expiresInDays,
    }),
    onSuccess: () => {
      setCreating(false);
      setName("");
      setPreset("inspect");
      setConstraints(emptyConstraints());
      setExtras([]);
      void queryClient.invalidateQueries({ queryKey: assistantKeys.grants });
    },
  });
  const issue = useMutation({
    mutationFn: (grant: AssistantGrant) => api.issueAssistantCredential(grant.id, 90),
    onSuccess: (created) => {
      setCredential({ value: created.credential, expiresAt: created.expires_at });
      void queryClient.invalidateQueries({ queryKey: assistantKeys.grants });
    },
  });
  const revoke = useMutation({
    mutationFn: (grant: AssistantGrant) => api.revokeAssistantGrant(grant.id),
    onSuccess: () => {
      setRevokeTarget(null);
      void queryClient.invalidateQueries({ queryKey: assistantKeys.grants });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate();
  }

  if (status.isPending) return <div className="mx-auto max-w-5xl"><Skeleton className="h-96" /></div>;
  if (status.isError) return <div className="mx-auto max-w-5xl"><ErrorState error={status.error} onRetry={() => void status.refetch()} /></div>;

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="AI assistants"
        subtitle="Connect ChatGPT, Claude or Codex to set up and manage Shortlist. You choose each connection’s permissions and limits."
        actions={status.data.enabled && !creating ? (
          <Button onClick={() => setCreating(true)}><Plus aria-hidden="true" /> New connection</Button>
        ) : undefined}
      />

      <CredentialDialog
        value={credential?.value ?? null}
        expiresAt={credential?.expiresAt ?? null}
        onClose={() => setCredential(null)}
      />
      <Dialog open={revokeTarget !== null} onOpenChange={(open) => { if (!open) setRevokeTarget(null); }}>
        <DialogContent>
          <DialogTitle>Revoke {revokeTarget?.name}?</DialogTitle>
          <DialogDescription>Its OAuth tokens and local credentials stop working immediately. Existing rows and schedules remain configured.</DialogDescription>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRevokeTarget(null)}>Keep connection</Button>
            <Button variant="destructive" loading={revoke.isPending} onClick={() => revokeTarget && revoke.mutate(revokeTarget)}>Revoke connection</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {!status.data.enabled ? (
        <div className="rounded-lg border bg-card p-5">
          <div className="flex items-start gap-3">
            <TriangleAlert aria-hidden="true" className="mt-0.5 h-5 w-5 text-warning" />
            <div className="space-y-2">
              <h2 className="font-semibold">AI assistants are off</h2>
              <p className="max-w-prose text-sm text-muted-foreground">
                {status.data.configuration_error ?? status.data.configuration_hint}
              </p>
            </div>
          </div>
        </div>
      ) : creating ? (
        <form onSubmit={submit} className="space-y-8 rounded-lg border bg-card p-5">
          <section className="space-y-3">
            <div><h2 className="font-semibold">Name and access mode</h2><p className="text-sm text-muted-foreground">Name the app or device so you can recognize it later.</p></div>
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Living room Codex" autoFocus maxLength={255} required />
            <div className="grid gap-2 md:grid-cols-3">
              {PRESETS.map((option) => (
                <button key={option.value} type="button" onClick={() => setPreset(option.value)}
                  className={`rounded-md border p-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${preset === option.value ? "border-primary bg-elevated shadow-selected-y" : "hover:bg-elevated"}`}>
                  <span className="block text-sm font-semibold">{option.title}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">{option.description}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="space-y-3">
            <div><h2 className="font-semibold">People, rows, and libraries</h2><p className="text-sm text-muted-foreground">An empty selection grants no access to that resource type.</p></div>
            <div className="grid gap-5 md:grid-cols-3">
              <div><h3 className="text-sm font-medium">People</h3>{users.data?.map((user) => <Choice key={user.id} checked={constraints.person_ids.includes(user.id)} title={user.friendly_name || user.display_name} onChange={() => setConstraints((value) => ({ ...value, person_ids: toggle(value.person_ids, user.id) }))} />)}</div>
              <div><h3 className="text-sm font-medium">Rows</h3>{rows.data?.map((row) => <Choice key={row.id} checked={constraints.row_ids.includes(row.id)} title={row.name} onChange={() => setConstraints((value) => ({ ...value, row_ids: toggle(value.row_ids, row.id) }))} />)}</div>
              <div><h3 className="text-sm font-medium">Libraries</h3>{libraries.data?.map((library) => <Choice key={library.key} checked={constraints.library_keys.includes(library.key)} title={library.title} onChange={() => setConstraints((value) => ({ ...value, library_keys: toggle(value.library_keys, library.key) }))} />)}</div>
            </div>
            <div className="border-t pt-2">
              <Choice checked={constraints.include_future_people} title="Include people added later" description="Dynamic audiences may expand when the Plex roster changes." onChange={() => setConstraints((value) => ({ ...value, include_future_people: !value.include_future_people }))} />
              <Choice checked={constraints.include_future_rows} title="Include rows created later" onChange={() => setConstraints((value) => ({ ...value, include_future_rows: !value.include_future_rows }))} />
              <Choice checked={constraints.include_future_libraries} title="Include libraries added later" onChange={() => setConstraints((value) => ({ ...value, include_future_libraries: !value.include_future_libraries }))} />
            </div>
          </section>

          <section className="space-y-3">
            <div><h2 className="font-semibold">Settings and external effects</h2><p className="text-sm text-muted-foreground">These controls are independent. Provider spend never implies permission to disclose viewing history.</p></div>
            <div className="grid gap-x-6 md:grid-cols-2">
              {status.data.setting_groups.map((group) => <Choice key={group} checked={constraints.setting_groups.includes(group)} title={`Manage ${group} settings`} onChange={() => setConstraints((value) => ({ ...value, setting_groups: toggle(value.setting_groups, group) }))} />)}
              {EXTRA_PERMISSIONS.map((permission) => <Choice key={permission.value} checked={extras.includes(permission.value)} title={permission.title} description={permission.description} onChange={() => setExtras((value) => toggle(value, permission.value))} />)}
            </div>
            <label className="block space-y-1 text-sm"><span className="font-medium">Approved destination IDs or canonical URLs</span><span className="block text-muted-foreground">One exact destination per line. Changing a configured URL requires a new approval.</span><textarea value={destinations} onChange={(event) => setDestinations(event.target.value)} rows={3} className="mt-2 w-full rounded-md border bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" /></label>
          </section>

          <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <label className="space-y-1 text-sm"><span className="font-medium">Expires after days</span><Input type="number" min={1} max={365} value={expiresInDays} onChange={(event) => setExpiresInDays(Number(event.target.value))} /></label>
            <label className="space-y-1 text-sm"><span className="font-medium">Max batch size</span><Input type="number" min={1} max={1000} value={constraints.max_batch_size ?? ""} onChange={(event) => setConstraints((value) => ({ ...value, max_batch_size: Number(event.target.value) || null }))} /></label>
            <label className="space-y-1 text-sm"><span className="font-medium">Max work per operation</span><Input type="number" min={1} max={100000} value={constraints.max_work_per_operation ?? ""} onChange={(event) => setConstraints((value) => ({ ...value, max_work_per_operation: Number(event.target.value) || null }))} /></label>
            <label className="space-y-1 text-sm"><span className="font-medium">Provider calls across this connection</span><span className="block text-xs text-muted-foreground">A finite lifetime allowance. Used or uncertain calls are not returned automatically.</span><Input type="number" min={0} max={100} value={constraints.max_provider_calls} onChange={(event) => setConstraints((value) => ({ ...value, max_provider_calls: Number(event.target.value) || 0 }))} /></label>
          </section>

          {create.isError && <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(create.error, "Could not create this connection.")}</p>}
          <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
            <Button type="button" variant="outline" onClick={() => setCreating(false)}>Cancel</Button>
            <Button type="submit" loading={create.isPending} disabled={!name.trim()}><ShieldCheck aria-hidden="true" /> Create connection</Button>
          </div>
        </form>
      ) : grants.isPending ? (
        <Skeleton className="h-64" />
      ) : grants.isError ? (
        <ErrorState error={grants.error} onRetry={() => void grants.refetch()} />
      ) : grants.data.length === 0 ? (
        <EmptyState icon={Link2} title="No assistant connections" hint="Create a named connection, choose exactly what it can reach, then issue a local credential or approve an OAuth sign-in." action={<Button onClick={() => setCreating(true)}><Plus aria-hidden="true" /> New connection</Button>} />
      ) : (
        <div className="divide-y overflow-hidden rounded-lg border bg-card">
          {grants.data.map((grant) => <GrantRow key={grant.id} grant={grant} onCredential={(value) => issue.mutate(value)} onRevoke={setRevokeTarget} />)}
        </div>
      )}

      {(issue.isError || revoke.isError) && <p role="alert" className="mt-4 text-sm text-destructive-text">{apiErrorMessage(issue.error ?? revoke.error, "Could not update assistant access.")}</p>}
    </div>
  );
}
