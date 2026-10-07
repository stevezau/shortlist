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
import { useId, useRef, useState, type Dispatch, type FormEvent, type SetStateAction } from "react";

import { AssistantPermissionFields } from "@/components/assistant-permissions";
import { baselineCapabilities, classifyAccess, selectedDestinationSnapshots, type AssistantMode, type SelectedDestination } from "@/lib/assistant-permission-model";
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
  AssistantStatus,
  AssistantDestination,
  Collection,
  PlexLibrary,
} from "@/lib/types";
import { useCopy } from "@/lib/use-copy";

const assistantKeys = {
  status: ["assistant", "status"] as const,
  grants: ["assistant", "grants"] as const,
};

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

function Choice({ checked, title, description, onChange }: {
  checked: boolean;
  title: string;
  description?: string;
  onChange: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  return (
    <label className="flex cursor-pointer items-start gap-3 py-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        className="mt-1 h-4 w-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <span className="min-w-0">
        <span id={titleId} className="block font-medium">{title}</span>
        {description && <span id={descriptionId} className="block text-muted-foreground">{description}</span>}
      </span>
    </label>
  );
}

function SelectionActions({ label, onSelectAll, onClear, disabled = false }: {
  label: string;
  onSelectAll: () => void;
  onClear: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-wrap gap-3 text-xs">
      <button type="button" className="text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50" onClick={onSelectAll} disabled={disabled}>Select all {label}</button>
      <button type="button" aria-label={`Clear ${label} selection`} className="text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={onClear}>Clear selection</button>
    </div>
  );
}

function ResourceLimitFields({
  constraints,
  setConstraints,
  rows,
  libraries,
  settingGroups,
  choices,
  capabilities,
  onGroupsCustomized,
  forceAdvancedOpen = false,
  expiresInDays,
  setExpiresInDays,
}: {
  constraints: AssistantGrantConstraints;
  setConstraints: Dispatch<SetStateAction<AssistantGrantConstraints>>;
  rows: Collection[] | undefined;
  libraries: PlexLibrary[] | undefined;
  settingGroups: string[];
  choices: AssistantDestination[];
  capabilities: string[];
  onGroupsCustomized: () => void;
  forceAdvancedOpen?: boolean;
  expiresInDays?: number;
  setExpiresInDays?: (value: number) => void;
}) {
  const rowScopeName = useId();
  const libraryScopeName = useId();
  const advancedId = useId();
  const batchId = useId();
  const workId = useId();
  const expiryId = useId();
  const batchRef = useRef<HTMLInputElement>(null);
  const workRef = useRef<HTMLInputElement>(null);
  const expiryRef = useRef<HTMLInputElement>(null);
  const [advancedOpen, setAdvancedOpen] = useState(
    forceAdvancedOpen || !constraints.include_future_rows || !constraints.include_future_libraries ||
    constraints.max_batch_size !== 25 || constraints.max_work_per_operation !== null ||
    (setExpiresInDays !== undefined && expiresInDays !== 90),
  );
  const historicalDestinations = constraints.destination_ids.filter((id) => !choices.some((choice) => choice.destination_id === id));
  function toggleAdvanced() {
    if (advancedOpen) {
      const invalid = [batchRef.current, workRef.current, expiryRef.current].find((field) => field && !field.checkValidity());
      if (invalid) {
        invalid.focus();
        invalid.reportValidity();
        return;
      }
    }
    setAdvancedOpen(!advancedOpen);
  }
  return (
    <>
      <section className="space-y-4 border-t pt-4">
        <button type="button" aria-expanded={advancedOpen} aria-controls={advancedId} onClick={toggleAdvanced} className="text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{advancedOpen ? "Hide advanced access and limits" : "Advanced access and limits"}</button>
        {advancedOpen && <div id={advancedId} className="space-y-5">
          <div className="grid gap-5 md:grid-cols-2">
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Rows</legend>
              <label className="flex items-center gap-2 text-sm"><input type="radio" name={rowScopeName} checked={constraints.include_future_rows} onChange={() => setConstraints((value) => ({ ...value, include_future_rows: true }))} className="accent-primary focus-visible:ring-2 focus-visible:ring-ring" />All current and future rows</label>
              <label className="flex items-center gap-2 text-sm"><input type="radio" name={rowScopeName} checked={!constraints.include_future_rows} onChange={() => setConstraints((value) => ({ ...value, include_future_rows: false }))} className="accent-primary focus-visible:ring-2 focus-visible:ring-ring" />Selected rows</label>
              {!constraints.include_future_rows && <div className="pl-6">
                <SelectionActions label="current rows" onSelectAll={() => setConstraints((value) => ({ ...value, row_ids: [...new Set([...value.row_ids, ...(rows ?? []).map((row) => row.id)])] }))} onClear={() => setConstraints((value) => ({ ...value, row_ids: [] }))} disabled={!rows?.length} />
                {rows?.map((row) => <Choice key={row.id} checked={constraints.row_ids.includes(row.id)} title={row.name} onChange={() => setConstraints((value) => ({ ...value, row_ids: toggle(value.row_ids, row.id) }))} />)}
              </div>}
            </fieldset>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Libraries</legend>
              <label className="flex items-center gap-2 text-sm"><input type="radio" name={libraryScopeName} checked={constraints.include_future_libraries} onChange={() => setConstraints((value) => ({ ...value, include_future_libraries: true }))} className="accent-primary focus-visible:ring-2 focus-visible:ring-ring" />All current and future libraries</label>
              <label className="flex items-center gap-2 text-sm"><input type="radio" name={libraryScopeName} checked={!constraints.include_future_libraries} onChange={() => setConstraints((value) => ({ ...value, include_future_libraries: false }))} className="accent-primary focus-visible:ring-2 focus-visible:ring-ring" />Selected libraries</label>
              {!constraints.include_future_libraries && <div className="pl-6">
                <SelectionActions label="current libraries" onSelectAll={() => setConstraints((value) => ({ ...value, library_keys: [...new Set([...value.library_keys, ...(libraries ?? []).map((library) => library.key)])] }))} onClear={() => setConstraints((value) => ({ ...value, library_keys: [] }))} disabled={!libraries?.length} />
                {libraries?.map((library) => <Choice key={library.key} checked={constraints.library_keys.includes(library.key)} title={library.title} onChange={() => setConstraints((value) => ({ ...value, library_keys: toggle(value.library_keys, library.key) }))} />)}
              </div>}
            </fieldset>
          </div>
          <div className="space-y-2 border-t pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-medium">Setting groups</h3>{settingGroups.length > 0 && <SelectionActions label="settings groups" onSelectAll={() => { onGroupsCustomized(); setConstraints((value) => ({ ...value, setting_groups: [...new Set([...value.setting_groups, ...settingGroups])] })); }} onClear={() => { onGroupsCustomized(); setConstraints((value) => ({ ...value, setting_groups: [] })); }} />}</div>
            <div className="grid gap-x-6 md:grid-cols-2">{settingGroups.map((group) => <Choice key={group} checked={constraints.setting_groups.includes(group)} title={`Manage ${group.replace(/_/g, " ")} settings`} onChange={() => { onGroupsCustomized(); setConstraints((value) => ({ ...value, setting_groups: toggle(value.setting_groups, group) })); }} />)}</div>
          </div>
          {historicalDestinations.length > 0 && <div className="space-y-2 border-t pt-4"><h3 className="text-sm font-medium">Previously approved service endpoints</h3><p className="text-xs text-muted-foreground">These no longer match a configured service. They stay approved until you remove them.</p>{historicalDestinations.map((id) => <div key={id} className="flex flex-wrap items-center gap-2 text-xs"><code className="break-all">{id}</code><button type="button" className="text-primary hover:underline" onClick={() => setConstraints((value) => ({ ...value, destination_ids: value.destination_ids.filter((item) => item !== id) }))}>Remove endpoint</button></div>)}</div>}
          <div className="border-t pt-4 text-xs text-muted-foreground"><p>Current permission details</p><p className="mt-1 break-words">{capabilities.join(" · ")}</p></div>
          <div className="grid gap-4 border-t pt-4 sm:grid-cols-2 lg:grid-cols-3">
            <div className="space-y-1 text-sm"><label htmlFor={batchId} className="block font-medium">Max batch size</label><p id={`${batchId}-help`} className="text-xs text-muted-foreground">Limits picks, settings, or rows changed in one operation.</p><Input ref={batchRef} id={batchId} aria-describedby={`${batchId}-help`} type="number" min={1} max={1000} value={constraints.max_batch_size ?? ""} onChange={(event) => setConstraints((value) => ({ ...value, max_batch_size: Number(event.target.value) || null }))} /></div>
            <div className="space-y-1 text-sm"><label htmlFor={workId} className="block font-medium">Max work per operation</label><p id={`${workId}-help`} className="text-xs text-muted-foreground">Optional work limit based on affected rows and people. Blank adds no extra limit.</p><Input ref={workRef} id={workId} aria-describedby={`${workId}-help`} type="number" min={1} max={100000} value={constraints.max_work_per_operation ?? ""} onChange={(event) => setConstraints((value) => ({ ...value, max_work_per_operation: Number(event.target.value) || null }))} /></div>
            {setExpiresInDays && <div className="space-y-1 text-sm"><label htmlFor={expiryId} className="block font-medium">Connection expires after days</label><p id={`${expiryId}-help`} className="text-xs text-muted-foreground">The access grant expires after this many days; local credentials have their own expiry.</p><Input ref={expiryRef} id={expiryId} aria-describedby={`${expiryId}-help`} type="number" min={1} max={365} value={expiresInDays ?? 90} onChange={(event) => setExpiresInDays(Number(event.target.value))} /></div>}
          </div>
        </div>}
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

function GrantRow({ grant, status: assistantStatus, onCredential, onEdit, onRevoke, onRemove }: {
  grant: AssistantGrant;
  status: AssistantStatus;
  onCredential: (grant: AssistantGrant) => void;
  onEdit: (grant: AssistantGrant) => void;
  onRevoke: (grant: AssistantGrant) => void;
  onRemove: (grant: AssistantGrant) => void;
}) {
  const mode = classifyAccess(grant.capabilities, assistantStatus);
  const restricted = !grant.constraints.include_future_rows || !grant.constraints.include_future_libraries ||
    grant.constraints.setting_groups.length < assistantStatus.setting_groups.length;
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
            {mode === "custom" ? "Custom access" : mode === "manage" ? "Manage Shortlist" : "Suggest changes"}
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
                Edit connection
              </Button>
            </>}
            <Button variant="ghost" size="sm" className="text-destructive-text" onClick={() => onRevoke(grant)}>
              <XCircle aria-hidden="true" /> Revoke
            </Button>
          </div>
        )}
      </div>
      {restricted && <p className="mt-2 text-xs text-muted-foreground">Restricted resources or settings groups</p>}
      <p className="mt-3 text-xs text-muted-foreground">
        All people · {grant.constraints.include_future_rows ? "All rows" : `Selected rows (${grant.constraints.row_ids.length})`} · {grant.constraints.include_future_libraries ? "All libraries" : `Selected libraries (${grant.constraints.library_keys.length})`} · {grant.local_credential_count ?? 0} local credentials
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
  const choices = useQuery({ queryKey: ["assistant", "destinations"], queryFn: api.getAssistantDestinations, enabled: status.data?.enabled === true });
  const rows = useCollections();
  const libraries = useLibraries();
  const [creating, setCreating] = useState(false);
  const [draftStarted, setDraftStarted] = useState(false);
  const [name, setName] = useState("");
  const [mode, setMode] = useState<AssistantMode>("manage");
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [constraints, setConstraints] = useState<AssistantGrantConstraints>(emptyConstraints);
  const [selectedDestinations, setSelectedDestinations] = useState<SelectedDestination[]>([]);
  const [groupsCustomized, setGroupsCustomized] = useState(false);
  const [expiresInDays, setExpiresInDays] = useState(90);
  const [credential, setCredential] = useState<{ value: string; expiresAt: string } | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<AssistantGrant | null>(null);
  const [removeTarget, setRemoveTarget] = useState<AssistantGrant | null>(null);
  const [editingTarget, setEditingTarget] = useState<AssistantGrant | null>(null);
  const [editingConstraints, setEditingConstraints] = useState<AssistantGrantConstraints>(emptyConstraints);
  const [editingSelectedDestinations, setEditingSelectedDestinations] = useState<SelectedDestination[]>([]);
  const [editingCapabilities, setEditingCapabilities] = useState<string[]>([]);
  const [editingMode, setEditingMode] = useState<AssistantMode>("custom");
  const [resourceUpdateNotice, setResourceUpdateNotice] = useState<string | null>(null);

  function beginCreating() {
    if (!draftStarted) {
      setMode("manage");
      setCapabilities(baselineCapabilities(status.data!, "manage"));
      setConstraints({ ...emptyConstraints(), setting_groups: [...status.data!.setting_groups] });
      setSelectedDestinations([]);
      setGroupsCustomized(false);
      setDraftStarted(true);
    }
    setCreating(true);
  }

  const create = useMutation({
    mutationFn: () => api.createAssistantGrant({
      client_id: `local-${crypto.randomUUID()}`,
      name: name.trim(),
      preset: mode === "suggest" ? "inspect" : "owner_automation",
      capabilities,
      constraints,
      selected_destinations: selectedDestinationSnapshots(constraints.destination_ids, [], selectedDestinations),
      expires_in_days: expiresInDays,
    }),
    onSuccess: () => {
      setCreating(false);
      setDraftStarted(false);
      setName("");
      setMode("manage");
      setCapabilities([]);
      setConstraints(emptyConstraints());
      setSelectedDestinations([]);
      setGroupsCustomized(false);
      setExpiresInDays(90);
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
      constraints: constraintPatch(editingConstraints, grant.constraints),
      ...(sameValues(editingCapabilities, grant.capabilities) ? {} : { capabilities: editingCapabilities }),
      selected_destinations: selectedDestinationSnapshots(editingConstraints.destination_ids, grant.constraints.destination_ids, editingSelectedDestinations),
    }),
    onSuccess: () => {
      setEditingTarget(null);
      setResourceUpdateNotice("Connection saved. New requests use the changed access. Reconnect the assistant to request any newly allowed OAuth permissions.");
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
    setEditingSelectedDestinations([]);
    setEditingCapabilities([...grant.capabilities]);
    setEditingMode(classifyAccess(grant.capabilities, status.data!));
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
          <Button onClick={beginCreating}><Plus aria-hidden="true" /> New connection</Button>
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
            <div><h2 className="font-semibold">Connection name</h2><p className="text-sm text-muted-foreground">Name the app or device so you can recognize it later.</p></div>
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Living room Codex" autoFocus maxLength={255} required />
          </section>
          <AssistantPermissionFields status={status.data} mode={mode} setMode={setMode} capabilities={capabilities} setCapabilities={setCapabilities} constraints={constraints} setConstraints={setConstraints} choices={choices.data ?? []} setSelectedDestinations={setSelectedDestinations} groupsCustomized={groupsCustomized} />
          <ResourceLimitFields
            constraints={constraints}
            setConstraints={setConstraints}
            rows={rows.data}
            libraries={libraries.data}
            settingGroups={status.data.setting_groups}
            choices={choices.data ?? []}
            capabilities={capabilities}
            onGroupsCustomized={() => setGroupsCustomized(true)}
            expiresInDays={expiresInDays}
            setExpiresInDays={setExpiresInDays}
          />

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
        <EmptyState icon={Link2} title="No assistant connections" hint="Create a named connection, choose its access, then issue a local credential or approve an OAuth sign-in." action={<Button onClick={beginCreating}><Plus aria-hidden="true" /> New connection</Button>} />
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
                  <h2 className="font-semibold">Edit connection for {grant.name}</h2>
                  {editingMode === "custom" && <Badge variant="outline">Custom access</Badge>}
                </div>
                <p className="text-sm text-muted-foreground">Changes affect new requests. Existing OAuth connections must reconnect to request newly allowed permissions.</p>
              </div>
              <AssistantPermissionFields status={status.data} mode={editingMode} setMode={setEditingMode} capabilities={editingCapabilities} setCapabilities={setEditingCapabilities} constraints={editingConstraints} setConstraints={setEditingConstraints} choices={choices.data ?? []} setSelectedDestinations={setEditingSelectedDestinations} reserved={grant.provider_call_quota?.reserved ?? 0} groupsCustomized />
              <ResourceLimitFields
                constraints={editingConstraints}
                setConstraints={setEditingConstraints}
                rows={rows.data}
                libraries={libraries.data}
                settingGroups={status.data.setting_groups}
                choices={choices.data ?? []}
                capabilities={editingCapabilities}
                onGroupsCustomized={() => undefined}
                forceAdvancedOpen={editingMode === "custom" || editingConstraints.setting_groups.length < status.data.setting_groups.length}
              />
              {update.isError && (update.error instanceof ApiError && update.error.status === 409 ? (
                <div role="alert" className="space-y-2 text-sm text-destructive-text">
                  <p>This connection changed elsewhere. Reload its resources before making another edit.</p>
                  <Button type="button" variant="outline" onClick={reloadAfterConflict}>Reload resources</Button>
                </div>
              ) : <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(update.error, "Could not save this connection.")}</p>)}
              <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button type="button" variant="outline" onClick={cancelEditing} disabled={update.isPending}>Cancel</Button>
                <Button type="submit" loading={update.isPending} disabled={Object.keys(constraintPatch(editingConstraints, grant.constraints)).length === 0 && sameValues(editingCapabilities, grant.capabilities)}>Save connection</Button>
              </div>
            </form>
          ) : <GrantRow key={grant.id} grant={grant} status={status.data} onCredential={(value) => issue.mutate(value)} onEdit={beginEditing} onRevoke={setRevokeTarget} onRemove={setRemoveTarget} />)}
        </div>
      )}

      {resourceUpdateNotice && <p role="status" className="mt-4 text-sm text-muted-foreground">{resourceUpdateNotice}</p>}
      {(issue.isError || revoke.isError || remove.isError) && <p role="alert" className="mt-4 text-sm text-destructive-text">{apiErrorMessage(issue.error ?? revoke.error ?? remove.error, "Could not update assistant access.")}</p>}
    </div>
  );
}
