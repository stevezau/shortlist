import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Clock3, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import { useParams } from "react-router";

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

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(valueText).join(", ") || "None";
  return Object.entries(value as Record<string, unknown>)
    .map(([key, item]) => `${key.replaceAll("_", " ")}: ${valueText(item)}`)
    .join(" · ");
}

function ReviewFrame({ children }: { children: ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl items-center px-4 py-10">
      <div className="w-full space-y-6">
        <div className="flex items-center gap-3"><Logo size="sm" /><div><p className="font-semibold">Shortlist</p><p className="text-xs text-muted-foreground">Exact change review</p></div></div>
        {children}
      </div>
    </main>
  );
}

export function AssistantChangePage() {
  const { changeId = "" } = useParams();
  const queryClient = useQueryClient();
  const session = useSession();
  const change = useQuery({
    queryKey: ["assistant", "change", changeId],
    queryFn: () => api.getAssistantChange(changeId),
    enabled: session.data?.authenticated === true && Boolean(changeId),
  });
  const approve = useMutation({
    mutationFn: () => api.approveAssistantChange(changeId),
    onSuccess: () => void change.refetch(),
  });

  if (session.isPending) return <ReviewFrame><Skeleton className="h-96" /></ReviewFrame>;
  if (session.isError) return <ReviewFrame><ErrorState error={session.error} onRetry={() => void session.refetch()} /></ReviewFrame>;
  if (!session.data.authenticated) {
    return (
      <ReviewFrame>
        <Card><CardHeader><CardTitle>Sign in to review this change</CardTitle><CardDescription>Use the Plex account that owns this Shortlist server. Approval is bound to this exact saved plan.</CardDescription></CardHeader><CardContent><PlexPinButton onLinked={() => void queryClient.invalidateQueries({ queryKey: queryKeys.session })} /></CardContent></Card>
      </ReviewFrame>
    );
  }
  if (change.isPending) return <ReviewFrame><Skeleton className="h-96" /></ReviewFrame>;
  if (change.isError) return <ReviewFrame><ErrorState error={change.error} onRetry={() => void change.refetch()} /></ReviewFrame>;

  const expired = new Date(change.data.expires_at) <= new Date();
  return (
    <ReviewFrame>
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-3"><ShieldAlert aria-hidden="true" className="mt-0.5 h-5 w-5 text-warning" /><div><CardTitle>{valueText(change.data.summary.description ?? change.data.kind)}</CardTitle><CardDescription className="mt-1">Review what Shortlist resolved, including downstream effects. Approval applies only to this content hash and grant revision.</CardDescription></div></div>
            <Badge variant={change.data.approved ? "success" : expired ? "secondary" : "warning"}>{change.data.approved ? "Approved" : expired ? "Expired" : "Needs approval"}</Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid gap-3 text-sm sm:grid-cols-2">
            <div><p className="text-xs text-muted-foreground">Operation type</p><p className="font-medium">{change.data.kind}</p></div>
            <div><p className="text-xs text-muted-foreground">Review expires</p><p className="font-medium">{formatDate(change.data.expires_at)}</p></div>
          </div>
          <section className="space-y-2"><h2 className="text-sm font-semibold">Authority required</h2><div className="divide-y rounded-md border">{Object.entries(change.data.requirements).map(([key, value]) => <div key={key} className="grid gap-1 px-3 py-3 text-sm sm:grid-cols-[10rem_1fr]"><span className="font-medium capitalize">{key.replaceAll("_", " ")}</span><span className="text-muted-foreground">{valueText(value)}</span></div>)}</div></section>
          <section className="space-y-2"><h2 className="text-sm font-semibold">Proposed result</h2><div className="divide-y rounded-md border">{Object.entries(change.data.summary).filter(([key]) => key !== "description").map(([key, value]) => <div key={key} className="grid gap-1 px-3 py-3 text-sm sm:grid-cols-[10rem_1fr]"><span className="font-medium capitalize">{key.replaceAll("_", " ")}</span><span className="text-muted-foreground">{valueText(value)}</span></div>)}</div></section>
          <section className="space-y-2"><h2 className="text-sm font-semibold">Effects</h2><div className="divide-y rounded-md border">{change.data.effects.length ? change.data.effects.map((effect, index) => <div key={`${valueText(effect)}-${index}`} className="px-3 py-3 text-sm">{Object.entries(effect).map(([key, value]) => <p key={key}><span className="font-medium capitalize">{key.replaceAll("_", " ")}:</span> <span className="text-muted-foreground">{valueText(value)}</span></p>)}</div>) : <p className="px-3 py-3 text-sm text-muted-foreground">No external effects were recorded.</p>}</div></section>
          <div className="rounded-md bg-elevated px-3 py-3 text-sm text-muted-foreground"><p className="flex items-center gap-2 font-medium text-foreground"><Clock3 aria-hidden="true" className="h-4 w-4" />One exact approval</p><p className="mt-1">Changing the proposal, its dependencies, or the connection grant invalidates this approval. It cannot mint credentials or authorize future work.</p></div>
          {approve.isError && <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(approve.error, "Could not approve this change.")}</p>}
          {approve.isSuccess && <div className="flex items-center gap-2 text-sm text-success"><CheckCircle2 aria-hidden="true" className="h-4 w-4" />Approval recorded. The assistant can apply this unchanged plan before it expires.</div>}
          {!change.data.approved && !expired && <div className="flex justify-end border-t pt-4"><Button loading={approve.isPending} onClick={() => approve.mutate()}><CheckCircle2 aria-hidden="true" />Approve exact change</Button></div>}
          <p className="break-all font-mono text-[11px] text-muted-foreground">Plan {change.data.content_hash}</p>
        </CardContent>
      </Card>
    </ReviewFrame>
  );
}
