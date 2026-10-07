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
import { useId, useMemo, useState, type Dispatch, type FormEvent, type SetStateAction } from "react";

import { PageHeader } from "@/components/page-header";
import { EmptyState, ErrorState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, api, apiErrorMessage } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { useCollections, useLibraries } from "@/lib/queries";
import type {
  AssistantGrant,
  AssistantGrantConstraints,
  AssistantGrantPreset,
  AssistantStatus,
  Collection,
  PlexLibrary,
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
    library_keys: [],
    setting_groups: [],
    destination_ids: [],
    include_future_rows: true,
    include_future_libraries: true,
    max_batch_size: 25,
    max_work_per_operation: null,
    max_provider_calls: 0,
  };
}

function toggle<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function cloneConstraints(constraints: AssistantGrantConstraints): AssistantGrantConstraints {
  return {
    ...constraints,
    row_ids: [...constraints.row_ids],
    library_keys: [...constraints.library_keys],
    setting_groups: [...constraints.setting_groups],
    destination_ids: [...constraints.destination_ids],
  };
}

function sameValues(left: readonly (string | number)[], right: readonly (string | number)[]): boolean {
  return left.length === right.length && left.every((value) => right.includes(value));
}

function constraintPatch(
  current: AssistantGrantConstraints,
  original: AssistantGrantConstraints,
): Partial<AssistantGrantConstraints> {
  const patch: Partial<AssistantGrantConstraints> = {};
  (Object.keys(current) as Array<keyof AssistantGrantConstraints>).forEach((key) => {
    const currentValue = current[key];
    const originalValue = original[key];
    if (Array.isArray(currentValue) && Array.isArray(originalValue)) {
      if (!sameValues(currentValue, originalValue)) patch[key] = currentValue as never;
    } else if (currentValue !== originalValue) {
      patch[key] = currentValue as never;
    }
  });
  return patch;
}

function hasCustomPermissions(grant: AssistantGrant, presets: AssistantStatus["presets"]): boolean {
  const presetCapabilities = presets[grant.preset] ?? [];
  return !sameValues(grant.capabilities, presetCapabilities);
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

function ResourceLimitFields({
  constraints,
  setConstraints,
  destinations,
  setDestinations,
  rows,
  libraries,
  settingGroups,
  includeExtras = false,
  extras = [],
  setExtras,
}: {
  constraints: AssistantGrantConstraints;
  setConstraints: Dispatch<SetStateAction<AssistantGrantConstraints>>;
  destinations: string;
  setDestinations: (value: string) => void;
  rows: Collection[] | undefined;
  libraries: PlexLibrary[] | undefined;
  settingGroups: string[];
  includeExtras?: boolean;
  extras?: string[];
  setExtras?: Dispatch<SetStateAction<string[]>>;
}) {
  const destinationEditorId = useId();
  return (
    <>
      <section className="space-y-3">
        <div><h2 className="font-semibold">Rows and libraries</h2><p className="text-sm text-muted-foreground">This connection can work with all current and future people. Row audiences still control who sees each row.</p></div>
        <div className="grid gap-5 md:grid-cols-2">
          <div><h3 className="text-sm font-medium">Rows</h3>{rows?.map((row) => <Choice key={row.id} checked={constraints.row_ids.includes(row.id)} title={row.name} onChange={() => setConstraints((value) => ({ ...value, row_ids: toggle(value.row_ids, row.id) }))} />)}</div>
          <div><h3 className="text-sm font-medium">Libraries</h3>{libraries?.map((library) => <Choice key={library.key} checked={constraints.library_keys.includes(library.key)} title={library.title} onChange={() => setConstraints((value) => ({ ...value, library_keys: toggle(value.library_keys, library.key) }))} />)}</div>
        </div>
        <div className="border-t pt-2">
          <Choice checked={constraints.include_future_rows} title="Include rows created later" onChange={() => setConstraints((value) => ({ ...value, include_future_rows: !value.include_future_rows }))} />
          <Choice checked={constraints.include_future_libraries} title="Include libraries added later" onChange={() => setConstraints((value) => ({ ...value, include_future_libraries: !value.include_future_libraries }))} />
        </div>
      </section>

      <section className="space-y-3">
        <div><h2 className="font-semibold">Settings and external effects</h2><p className="text-sm text-muted-foreground">These controls are independent. Provider spend never implies permission to disclose viewing history.</p></div>
        <div className="grid gap-x-6 md:grid-cols-2">
          {settingGroups.map((group) => <Choice key={group} checked={constraints.setting_groups.includes(group)} title={`Manage ${group} settings`} onChange={() => setConstraints((value) => ({ ...value, setting_groups: toggle(value.setting_groups, group) }))} />)}
          {includeExtras && EXTRA_PERMISSIONS.map((permission) => <Choice key={permission.value} checked={extras.includes(permission.value)} title={permission.title} description={permission.description} onChange={() => setExtras?.((value) => toggle(value, permission.value))} />)}
        </div>
        <div className="space-y-1 text-sm">
          <label htmlFor={destinationEditorId} className="font-medium">Approved destination IDs or canonical URLs</label>
          <p id={`${destinationEditorId}-help`} className="text-muted-foreground">One exact destination per line. Changing a configured URL requires a new approval.</p>
          <textarea id={destinationEditorId} aria-describedby={`${destinationEditorId}-help`} value={destinations} onChange={(event) => setDestinations(event.target.value)} rows={3} className="mt-2 w-full rounded-md border bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </div>
      </section>

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label className="space-y-1 text-sm"><span className="font-medium">Max batch size</span><Input type="number" min={1} max={1000} value={constraints.max_batch_size ?? ""} onChange={(event) => setConstraints((value) => ({ ...value, max_batch_size: Number(event.target.value) || null }))} /></label>
        <label className="space-y-1 text-sm"><span className="font-medium">Max work per operation</span><Input type="number" min={1} max={100000} value={constraints.max_work_per_operation ?? ""} onChange={(event) => setConstraints((value) => ({ ...value, max_work_per_operation: Number(event.target.value) || null }))} /></label>
        <label className="space-y-1 text-sm"><span className="font-medium">Provider calls across this connection</span><span className="block text-xs text-muted-foreground">A finite lifetime allowance. Used or uncertain calls are not returned automatically.</span><Input type="number" min={0} max={100} value={constraints.max_provider_calls} onChange={(event) => setConstraints((value) => ({ ...value, max_provider_calls: Number(event.target.value) || 0 }))} /></label>
      </section>
    </>
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

function GrantRow({ grant, presets, onCredential, onEdit, onRevoke, onRemove }: {
  grant: AssistantGrant;
  presets: AssistantStatus["presets"];
  onCredential: (grant: AssistantGrant) => void;
  onEdit: (grant: AssistantGrant) => void;
  onRevoke: (grant: AssistantGrant) => void;
  onRemove: (grant: AssistantGrant) => void;
}) {
  const revoked = Boolean(grant.revoked_at);
  const expired = Boolean(grant.expires_at && new Date(grant.expires_at) <= new Date());
  const requiresApproval = !revoked && !expired && grant.requires_access_approval === true;
  const status = revoked ? "Revoked" : expired ? "Expired" : requiresApproval ? "Approval required" : "Active";
  return (
    <article className="px-4 py-4 sm:px-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-semibold">{grant.name}</h2>
            <Badge variant={revoked || expired ? "secondary" : requiresApproval ? "warning" : "success"}>{status}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            {PRESETS.find((preset) => preset.value === grant.preset)?.title ?? grant.preset}
            {grant.last_used_at ? ` · Used ${timeAgo(grant.last_used_at)}` : " · Never used"}
            {grant.expires_at ? ` · Expires ${formatDate(grant.expires_at)}` : " · No expiry"}
          </p>
        </div>
        {revoked ? (
          <Button variant="ghost" size="sm" className="text-destructive-text" onClick={() => onRemove(grant)}>
            <XCircle aria-hidden="true" /> Remove
          </Button>
        ) : !expired && (
          <div className="flex flex-wrap gap-2">
            {requiresApproval ? (
              <Button size="sm" onClick={() => onEdit(grant)}>
                <ShieldCheck aria-hidden="true" /> Approve updated access
              </Button>
            ) : <>
              <Button variant="outline" size="sm" onClick={() => onCredential(grant)}>
                <KeyRound aria-hidden="true" /> New local credential
              </Button>
              <Button variant="outline" size="sm" onClick={() => onEdit(grant)}>
                Edit resources
              </Button>
            </>}
            <Button variant="ghost" size="sm" className="text-destructive-text" onClick={() => onRevoke(grant)}>
              <XCircle aria-hidden="true" /> Revoke
            </Button>
          </div>
        )}
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {grant.capabilities.map((capability) => <Badge key={capability} variant="outline">{capability}</Badge>)}
        {hasCustomPermissions(grant, presets) && <Badge variant="outline">Custom permissions</Badge>}
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        All people · {grant.constraints.row_ids.length} rows · {grant.constraints.library_keys.length} libraries · {grant.local_credential_count ?? 0} local credentials
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
  const [removeTarget, setRemoveTarget] = useState<AssistantGrant | null>(null);
  const [editingTarget, setEditingTarget] = useState<AssistantGrant | null>(null);
  const [editingConstraints, setEditingConstraints] = useState<AssistantGrantConstraints>(emptyConstraints);
  const [editingDestinations, setEditingDestinations] = useState("");
  const [resourceUpdateNotice, setResourceUpdateNotice] = useState<string | null>(null);

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
  const remove = useMutation({
    mutationFn: (grant: AssistantGrant) => api.removeAssistantGrant(grant.id),
    onSuccess: () => {
      setRemoveTarget(null);
      void queryClient.invalidateQueries({ queryKey: assistantKeys.grants });
    },
  });
  const update = useMutation({
    mutationFn: (grant: AssistantGrant) => api.updateAssistantGrant(grant.id, {
      expected_revision: grant.revision,
      constraints: constraintPatch(
        { ...editingConstraints, destination_ids: editingDestinations.split("\n").map((value) => value.trim()).filter(Boolean) },
        grant.constraints,
      ),
    }),
    onSuccess: () => {
      setEditingTarget(null);
      setResourceUpdateNotice("Resources saved. New requests use the changed access. Older prepared changes need a new plan.");
      void queryClient.invalidateQueries({ queryKey: assistantKeys.grants });
    },
  });
  const approveAccess = useMutation({
    mutationFn: (grant: AssistantGrant) => api.updateAssistantGrant(grant.id, {
      expected_revision: grant.revision,
      approve_updated_access: true,
    }),
    onSuccess: () => {
      setEditingTarget(null);
      setResourceUpdateNotice("Updated access approved. This connection can now work with all current and future people.");
      void queryClient.invalidateQueries({ queryKey: assistantKeys.grants });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate();
  }

  function beginEditing(grant: AssistantGrant) {
    setEditingTarget(grant);
    setEditingConstraints(cloneConstraints(grant.constraints));
    setEditingDestinations(grant.constraints.destination_ids.join("\n"));
    update.reset();
    setResourceUpdateNotice(null);
  }

  function cancelEditing() {
    setEditingTarget(null);
    update.reset();
  }

  function submitEdit(event: FormEvent) {
    event.preventDefault();
    if (editingTarget) update.mutate(editingTarget);
  }

  function reloadAfterConflict() {
    cancelEditing();
    void grants.refetch();
  }

  if (status.isPending) return <div className="mx-auto max-w-5xl"><Skeleton className="h-96" /></div>;
  if (status.isError) return <div className="mx-auto max-w-5xl"><ErrorState error={status.error} onRetry={() => void status.refetch()} /></div>;

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="AI assistants"
        subtitle="Connect ChatGPT, Claude or Codex to set up and manage Shortlist. You choose each connection’s permissions and limits."
        actions={status.data.enabled && !creating && !editingTarget ? (
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
      <Dialog open={removeTarget !== null} onOpenChange={(open) => { if (!open) setRemoveTarget(null); }}>
        <DialogContent>
          <DialogTitle>Remove revoked connection?</DialogTitle>
          <DialogDescription>It is already disconnected. Removing it clears the old connection record and credentials; activity history is retained.</DialogDescription>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRemoveTarget(null)}>Keep connection</Button>
            <Button variant="destructive" loading={remove.isPending} onClick={() => removeTarget && remove.mutate(removeTarget)}>Remove connection</Button>
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

          <ResourceLimitFields
            constraints={constraints}
            setConstraints={setConstraints}
            destinations={destinations}
            setDestinations={setDestinations}
            rows={rows.data}
            libraries={libraries.data}
            settingGroups={status.data.setting_groups}
            includeExtras
            extras={extras}
            setExtras={setExtras}
          />
          <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <label className="space-y-1 text-sm"><span className="font-medium">Expires after days</span><Input type="number" min={1} max={365} value={expiresInDays} onChange={(event) => setExpiresInDays(Number(event.target.value))} /></label>
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
          {grants.data.map((grant) => editingTarget?.id === grant.id ? (
            grant.requires_access_approval ? <section key={grant.id} className="space-y-6 px-4 py-5 sm:px-5">
              <div className="space-y-2">
                <h2 className="font-semibold">Approve updated access for {grant.name}</h2>
                <p className="text-sm text-muted-foreground">This connection needs one owner approval to work with all current and future people, rows, and libraries. Row audiences and existing sharing rules still control who can see each row.</p>
                <p className="text-sm text-muted-foreground">The audience-size work limit is removed. Provider calls remain limited to {grant.constraints.max_provider_calls}.</p>
              </div>
              {approveAccess.isError && <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(approveAccess.error, "Could not approve updated access.")}</p>}
              <div className="flex justify-end border-t pt-4"><Button loading={approveAccess.isPending} onClick={() => approveAccess.mutate(grant)}>Confirm updated access</Button></div>
            </section> : <form key={grant.id} onSubmit={submitEdit} className="space-y-6 px-4 py-5 sm:px-5">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="font-semibold">Edit resources for {grant.name}</h2>
                  <Badge variant="outline">{PRESETS.find((presetOption) => presetOption.value === grant.preset)?.title ?? grant.preset}</Badge>
                  {hasCustomPermissions(grant, status.data.presets) && <Badge variant="outline">Custom permissions</Badge>}
                </div>
                <p className="text-sm text-muted-foreground">Permissions stay read-only here. Save only rows, libraries, settings, destinations, and limits below. Row audiences continue to protect each person's privacy.</p>
                <div className="flex flex-wrap gap-1.5">
                  {grant.capabilities.map((capability) => <Badge key={capability} variant="outline">{capability}</Badge>)}
                </div>
              </div>
              <ResourceLimitFields
                constraints={editingConstraints}
                setConstraints={setEditingConstraints}
                destinations={editingDestinations}
                setDestinations={setEditingDestinations}
                rows={rows.data}
                libraries={libraries.data}
                settingGroups={status.data.setting_groups}
              />
              {update.isError && (update.error instanceof ApiError && update.error.status === 409 ? (
                <div role="alert" className="space-y-2 text-sm text-destructive-text">
                  <p>This connection changed elsewhere. Reload its resources before making another edit.</p>
                  <Button type="button" variant="outline" onClick={reloadAfterConflict}>Reload resources</Button>
                </div>
              ) : <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(update.error, "Could not save these resources.")}</p>)}
              <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button type="button" variant="outline" onClick={cancelEditing} disabled={update.isPending}>Cancel</Button>
                <Button type="submit" loading={update.isPending} disabled={Object.keys(constraintPatch({ ...editingConstraints, destination_ids: editingDestinations.split("\n").map((value) => value.trim()).filter(Boolean) }, grant.constraints)).length === 0}>Save resources</Button>
              </div>
            </form>
          ) : <GrantRow key={grant.id} grant={grant} presets={status.data.presets} onCredential={(value) => issue.mutate(value)} onEdit={beginEditing} onRevoke={setRevokeTarget} onRemove={setRemoveTarget} />)}
        </div>
      )}

      {resourceUpdateNotice && <p role="status" className="mt-4 text-sm text-muted-foreground">{resourceUpdateNotice}</p>}
      {(issue.isError || revoke.isError || remove.isError) && <p role="alert" className="mt-4 text-sm text-destructive-text">{apiErrorMessage(issue.error ?? revoke.error ?? remove.error, "Could not update assistant access.")}</p>}
    </div>
  );
}
