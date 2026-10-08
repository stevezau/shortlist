import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, KeyRound, Link2, Plus, ShieldCheck, TriangleAlert, XCircle } from "lucide-react";
import { useState, type FormEvent } from "react";

import { AssistantPermissionFields, type AssistantAccessRole } from "@/components/assistant-permissions";
import { PageHeader } from "@/components/page-header";
import { EmptyState, ErrorState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, api, apiErrorMessage } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import type { AssistantGrant } from "@/lib/types";
import { useCopy } from "@/lib/use-copy";

const grantKey = ["assistant", "grants"] as const;

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
        <DialogDescription>Shortlist stores only a verifier. This credential cannot be shown again after you close this window.</DialogDescription>
        {value && <div className="space-y-3">
          <code className="block break-all rounded-md border bg-muted p-3 font-mono text-xs">{value}</code>
          <Button type="button" className="w-full" onClick={() => copy(value)}>
            {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
            {state === "copied" ? "Copied" : "Copy credential"}
          </Button>
          {expiresAt && <p className="text-xs text-muted-foreground">Expires {formatDate(expiresAt)}.</p>}
        </div>}
        <DialogFooter><Button variant="outline" onClick={onClose}>I saved it</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function GrantRow({ grant, onCredential, onEdit, onRevoke, onRemove }: {
  grant: AssistantGrant;
  onCredential: (grant: AssistantGrant) => void;
  onEdit: (grant: AssistantGrant) => void;
  onRevoke: (grant: AssistantGrant) => void;
  onRemove: (grant: AssistantGrant) => void;
}) {
  const revoked = Boolean(grant.revoked_at);
  const expired = Boolean(grant.expires_at && new Date(grant.expires_at) <= new Date());
  const blocked = grant.requires_access_approval === true;
  const status = revoked ? "Disconnected" : expired ? "Expired" : blocked ? "Approval required" : "Active";
  const access = grant.access_role === "view" ? "View only" : grant.access_role === "manage"
    ? (grant.full_management ? "Manage Shortlist" : "Manage Shortlist · limited by client permissions") : "Existing access";
  return <article className="rounded-lg border bg-card px-4 py-4 sm:px-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-semibold">{grant.name}</h2>
          <Badge variant={revoked || expired ? "secondary" : blocked ? "warning" : "success"}>{status}</Badge>
        </div>
        <p className="text-sm text-muted-foreground">
          {access}
          {grant.last_used_at ? ` · Used ${timeAgo(grant.last_used_at)}` : " · Never used"}
          {grant.expires_at ? ` · Expires ${formatDate(grant.expires_at)}` : " · No expiry"}
        </p>
        <p className="text-xs text-muted-foreground">{grant.local_credential_count ?? 0} local credentials</p>
      </div>
      {revoked ? <Button variant="ghost" size="sm" className="text-destructive-text" onClick={() => onRemove(grant)}><XCircle aria-hidden="true" /> Remove</Button> : !expired && <div className="flex flex-wrap gap-2">
        {!blocked && <Button variant="outline" size="sm" onClick={() => onCredential(grant)}><KeyRound aria-hidden="true" /> New local credential</Button>}
        <Button variant="outline" size="sm" onClick={() => onEdit(grant)}>Change access</Button>
        <Button variant="ghost" size="sm" className="text-destructive-text" onClick={() => onRevoke(grant)}><XCircle aria-hidden="true" /> Disconnect</Button>
      </div>}
    </div>
    {!grant.access_role && !revoked && <p className="mt-3 text-xs text-muted-foreground">Its existing permissions remain in force until you choose and save a new access level.</p>}
  </article>;
}

export function AssistantAccessPage() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["assistant", "status"], queryFn: api.getAssistantStatus });
  const grants = useQuery({ queryKey: grantKey, queryFn: api.getAssistantGrants, enabled: status.data?.enabled === true });
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState<AssistantAccessRole | null>("manage");
  const [editing, setEditing] = useState<AssistantGrant | null>(null);
  const [credential, setCredential] = useState<{ value: string; expiresAt: string } | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<AssistantGrant | null>(null);
  const [removeTarget, setRemoveTarget] = useState<AssistantGrant | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => api.createAssistantGrant({
      client_id: `local-${crypto.randomUUID()}`,
      name: name.trim(),
      preset: "owner_automation",
      access_role: role ?? "manage",
    }),
    onSuccess: () => {
      setCreating(false);
      setName("");
      setRole("manage");
      void queryClient.invalidateQueries({ queryKey: grantKey });
    },
  });
  const update = useMutation({
    mutationFn: (grant: AssistantGrant) => api.updateAssistantGrant(grant.id, {
      expected_revision: grant.revision,
      access_role: role ?? "view",
    }),
    onSuccess: () => {
      setEditing(null);
      setNotice("Connection updated. Existing OAuth clients must reconnect to request newly available permissions.");
      void queryClient.invalidateQueries({ queryKey: grantKey });
    },
  });
  const issue = useMutation({
    mutationFn: (grant: AssistantGrant) => api.issueAssistantCredential(grant.id, 90),
    onSuccess: (created) => {
      setCredential({ value: created.credential, expiresAt: created.expires_at });
      void queryClient.invalidateQueries({ queryKey: grantKey });
    },
  });
  const revoke = useMutation({
    mutationFn: (grant: AssistantGrant) => api.revokeAssistantGrant(grant.id),
    onSuccess: () => { setRevokeTarget(null); void queryClient.invalidateQueries({ queryKey: grantKey }); },
  });
  const remove = useMutation({
    mutationFn: (grant: AssistantGrant) => api.removeAssistantGrant(grant.id),
    onSuccess: () => { setRemoveTarget(null); void queryClient.invalidateQueries({ queryKey: grantKey }); },
  });

  function beginEdit(grant: AssistantGrant) {
    setEditing(grant);
    setRole(grant.access_role ?? null);
    setNotice(null);
    update.reset();
  }
  function submitCreate(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate();
  }
  function submitEdit(event: FormEvent) {
    event.preventDefault();
    if (editing && role) update.mutate(editing);
  }

  if (status.isPending) return <div className="mx-auto max-w-5xl"><Skeleton className="h-96" /></div>;
  if (status.isError) return <div className="mx-auto max-w-5xl"><ErrorState error={status.error} onRetry={() => void status.refetch()} /></div>;

  return <div className="mx-auto max-w-5xl">
    <PageHeader
      title="AI assistants"
      subtitle="Connect ChatGPT, Claude or Codex to manage Shortlist with your approval."
      actions={status.data.enabled && !creating && !editing ? <Button onClick={() => { setRole("manage"); setCreating(true); }}><Plus aria-hidden="true" /> New connection</Button> : undefined}
    />
    <CredentialDialog value={credential?.value ?? null} expiresAt={credential?.expiresAt ?? null} onClose={() => setCredential(null)} />
    <Dialog open={revokeTarget !== null} onOpenChange={(open) => { if (!open) setRevokeTarget(null); }}>
      <DialogContent>
        <DialogTitle>Disconnect {revokeTarget?.name}?</DialogTitle>
        <DialogDescription>Its OAuth tokens and local credentials stop working immediately. Existing rows and schedules remain configured.</DialogDescription>
        <DialogFooter>
          <Button variant="outline" onClick={() => setRevokeTarget(null)}>Keep connection</Button>
          <Button variant="destructive" loading={revoke.isPending} onClick={() => revokeTarget && revoke.mutate(revokeTarget)}>Disconnect</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={removeTarget !== null} onOpenChange={(open) => { if (!open) setRemoveTarget(null); }}>
      <DialogContent>
        <DialogTitle>Remove disconnected connection?</DialogTitle>
        <DialogDescription>Removing it clears the old connection record and credentials; activity history is retained.</DialogDescription>
        <DialogFooter>
          <Button variant="outline" onClick={() => setRemoveTarget(null)}>Keep connection</Button>
          <Button variant="destructive" loading={remove.isPending} onClick={() => removeTarget && remove.mutate(removeTarget)}>Remove connection</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    {!status.data.enabled ? <div className="rounded-lg border bg-card p-5"><div className="flex items-start gap-3">
      <TriangleAlert aria-hidden="true" className="mt-0.5 h-5 w-5 text-warning" />
      <div className="space-y-2"><h2 className="font-semibold">AI assistants are off</h2><p className="max-w-prose text-sm text-muted-foreground">{status.data.configuration_error ?? status.data.configuration_hint}</p></div>
    </div></div> : creating ? <form onSubmit={submitCreate} className="max-w-2xl space-y-5 rounded-lg border bg-card p-5 sm:p-6">
      <div className="space-y-2"><label htmlFor="assistant-name" className="block font-semibold">Connection name</label><Input id="assistant-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="My assistant" autoFocus maxLength={255} required /><p className="text-xs text-muted-foreground">Name the app or device so you can recognize it later.</p></div>
      <AssistantPermissionFields role={role} setRole={setRole} />
      {create.isError && <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(create.error, "Could not create this connection.")}</p>}
      <div className="flex flex-wrap justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={() => setCreating(false)}>Cancel</Button><Button type="submit" loading={create.isPending} disabled={!name.trim()}><ShieldCheck aria-hidden="true" /> Connect</Button></div>
    </form> : grants.isPending ? <Skeleton className="h-64" /> : grants.isError ? <ErrorState error={grants.error} onRetry={() => void grants.refetch()} /> : grants.data.length === 0 ? <EmptyState icon={Link2} title="No assistant connections" hint="Connect an assistant, then issue a local credential or approve its sign-in." action={<Button onClick={() => setCreating(true)}><Plus aria-hidden="true" /> New connection</Button>} /> : <div className="space-y-3">
      {grants.data.map((grant) => editing?.id === grant.id ? <form key={grant.id} onSubmit={submitEdit} className="space-y-5 rounded-lg border bg-card px-4 py-5 sm:px-5">
        <div className="space-y-1"><h2 className="font-semibold">Change access for {grant.name}</h2><p className="text-sm text-muted-foreground">Save a selected level to grant its current permissions, including when this connection has limited client access. Its expiry and past usage remain unchanged. OAuth clients must reconnect to request newly available permissions.</p></div>
        <AssistantPermissionFields role={role} setRole={setRole} />
        {update.isError && (update.error instanceof ApiError && update.error.status === 409 ? <div role="alert" className="space-y-2 text-sm text-destructive-text"><p>This connection changed elsewhere. Reload it before continuing.</p><Button type="button" variant="outline" onClick={() => { setEditing(null); void grants.refetch(); }}>Reload connection</Button></div> : <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(update.error, "Could not update this connection.")}</p>)}
        <div className="flex flex-wrap justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={() => setEditing(null)} disabled={update.isPending}>Cancel</Button><Button type="submit" loading={update.isPending} disabled={!role || (role === grant.access_role && grant.full_management)}>Save access</Button></div>
      </form> : <GrantRow key={grant.id} grant={grant} onCredential={(value) => issue.mutate(value)} onEdit={beginEdit} onRevoke={setRevokeTarget} onRemove={setRemoveTarget} />)}
    </div>}
    {notice && <p role="status" className="mt-4 text-sm text-muted-foreground">{notice}</p>}
    {(issue.isError || revoke.isError || remove.isError) && <p role="alert" className="mt-4 text-sm text-destructive-text">{apiErrorMessage(issue.error ?? revoke.error ?? remove.error, "Could not update assistant access.")}</p>}
  </div>;
}
