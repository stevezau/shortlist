import {
  Check,
  Copy,
  Eye,
  EyeOff,
  KeyRound,
  RefreshCw,
  TriangleAlert,
  Trash2,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { apiErrorMessage, apiUrl } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { useCopy } from "@/lib/use-copy";
import {
  useApiToken,
  useCreateApiToken,
  useRevokeApiToken,
} from "@/lib/queries";

/** A copy-to-clipboard button that flips to a check for a moment — or an error, since the token is
 *  the whole point of this card and a silent clipboard failure here leaves someone with nothing. */
function CopyButton({
  value,
  label = "Copy",
}: {
  value: string;
  label?: string;
}) {
  const { state, copy } = useCopy();
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={() => copy(value)}
    >
      {state === "copied" ? (
        <Check aria-hidden="true" />
      ) : state === "error" ? (
        <TriangleAlert aria-hidden="true" />
      ) : (
        <Copy aria-hidden="true" />
      )}
      {state === "copied"
        ? "Copied"
        : state === "error"
          ? "Couldn’t copy"
          : label}
    </Button>
  );
}

/** The active token, masked by default with a show/hide toggle and copy — like Sonarr/Radarr. */
function TokenField({ token }: { token: string }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <code className="flex-1 break-all rounded bg-muted px-2 py-1.5 font-mono text-xs">
        {shown ? token : "•".repeat(Math.min(token.length, 40))}
      </code>
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-pressed={shown}
        onClick={() => setShown((s) => !s)}
      >
        {shown ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
        {shown ? "Hide" : "Show"}
      </Button>
      <CopyButton value={token} />
    </div>
  );
}

/**
 * Generate a personal API token so scripts can call Shortlist's API without the browser login.
 * The token is stored encrypted and stays revealable here (show/copy any time); regenerating or
 * revoking invalidates the old one immediately.
 */
export function ApiAccessCard() {
  const status = useApiToken();
  const create = useCreateApiToken();
  const revoke = useRevokeApiToken();

  const token = status.data?.token ?? null;
  const enabled = status.data?.enabled ?? false;
  const exampleUrl = apiUrl("/api/runs");

  return (
    <section aria-labelledby="api-access-heading" className="space-y-3">
      <h2 id="api-access-heading" className="text-lg font-semibold">
        API access
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <p className="text-sm text-muted-foreground">
            Generate a personal token to call Shortlist’s API from a script.
            <br />
            Send it as a header:{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              Authorization: Bearer &lt;token&gt;
            </code>
            .
            <br />
            The token has the same full access you do, so keep it secret — treat
            it like a password.
          </p>

          {status.isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : enabled && token ? (
            <div className="space-y-3">
              <TokenField token={token} />
              {status.data?.created_at && (
                <p
                  className="text-xs text-muted-foreground"
                  title={formatDate(status.data.created_at)}
                >
                  Created {timeAgo(status.data.created_at)}.
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  onClick={() => create.mutate()}
                  loading={create.isPending}
                >
                  {!create.isPending && <RefreshCw aria-hidden="true" />}
                  Regenerate
                </Button>
                <Button
                  variant="ghost"
                  className="text-destructive hover:text-destructive"
                  onClick={() => revoke.mutate()}
                  loading={revoke.isPending}
                >
                  {!revoke.isPending && <Trash2 aria-hidden="true" />}
                  Revoke
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Regenerating replaces the current token; revoking turns it off.
                Either way, any script still using the old token stops working
                right away.
              </p>
            </div>
          ) : (
            <Button onClick={() => create.mutate()} loading={create.isPending}>
              {!create.isPending && <KeyRound aria-hidden="true" />}
              Generate token
            </Button>
          )}

          {(create.isError || revoke.isError) && (
            <p role="alert" className="text-sm text-destructive">
              {apiErrorMessage(
                create.error ?? revoke.error,
                "Something went wrong. Try again.",
              )}
            </p>
          )}

          {/* A ready-to-run example so it's obvious how to use the token. */}
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-muted-foreground">Example</p>
            <div className="flex items-start gap-2">
              <code className="flex-1 break-all rounded bg-muted px-2 py-1.5 font-mono text-xs">
                curl -H "Authorization: Bearer &lt;token&gt;" {exampleUrl}
              </code>
              <CopyButton
                value={`curl -H "Authorization: Bearer <token>" ${exampleUrl}`}
              />
            </div>
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
