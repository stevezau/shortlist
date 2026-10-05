import { ArrowRight, Bot, TriangleAlert } from "lucide-react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";

/** Status and entry point only; detailed grant management has its own full-width screen. */
export function AssistantAccessCard() {
  const status = useQuery({ queryKey: ["assistant", "status"], queryFn: api.getAssistantStatus });

  return (
    <section id="assistant-access" aria-labelledby="assistant-access-heading" className="scroll-mt-32 space-y-3 md:scroll-mt-8">
      <h2 id="assistant-access-heading" className="text-base font-semibold tracking-tight">AI assistants</h2>
      <Card>
        <CardContent className="pt-6">
          {status.isPending ? <Skeleton className="h-24" /> : status.isError ? (
            <div className="flex items-start gap-3" role="alert"><TriangleAlert aria-hidden="true" className="mt-0.5 h-5 w-5 text-destructive-text" /><div><p className="text-sm font-medium">Couldn’t check assistant access</p><Button variant="outline" size="sm" className="mt-3" onClick={() => void status.refetch()}>Try again</Button></div></div>
          ) : (
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="flex min-w-0 flex-1 basis-72 items-start gap-3">
                <Bot aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 space-y-2">
                  <Badge variant={status.data.enabled ? "success" : "secondary"}>{status.data.enabled ? "Enabled" : "Off"}</Badge>
                  <p className="max-w-prose text-sm font-medium">Connect ChatGPT, Claude or Codex to set up and manage Shortlist.</p>
                  <p className="max-w-prose text-sm text-muted-foreground">You choose which people, rows and libraries each assistant can access, plus its permissions and limits. Connect through MCP without sharing your owner API token, and revoke access at any time.</p>
                  {!status.data.enabled && <p className="text-xs text-muted-foreground">{status.data.configuration_error ?? status.data.configuration_hint}</p>}
                </div>
              </div>
              <Button asChild variant="outline"><Link to="/assistant-access">{status.data.enabled ? "Manage connections" : "Connect an assistant"} <ArrowRight aria-hidden="true" /></Link></Button>
            </div>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
