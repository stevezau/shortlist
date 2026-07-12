import { useMutation } from "@tanstack/react-query";
import { Check, Loader2, X } from "lucide-react";
import { useId, useState } from "react";

import { PlexPinButton } from "@/components/plex-pin-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";
import type { ProbeCheck, ProbeResult } from "@/lib/types";

import type { StepProps } from "./step-props";

const DEFAULT_PLEX_URL = "http://localhost:32400";

function CheckLine({ label, check }: { label: string; check: ProbeCheck }) {
  return (
    <li className="flex items-start gap-2 text-sm">
      {check.ok ? (
        <Check
          className="mt-0.5 h-4 w-4 shrink-0 text-success"
          aria-hidden="true"
        />
      ) : (
        <X
          className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
          aria-hidden="true"
        />
      )}
      <span>
        <span className="font-medium">{label}:</span>{" "}
        <span className="text-muted-foreground">{check.message}</span>
      </span>
    </li>
  );
}

/**
 * Step 1 — Login with Plex (PIN), probe the server with a live checklist,
 * then link it. The scoped Plex token lives in component state only.
 */
export function StepConnect({ data, update }: StepProps) {
  // Memory only — never persisted (rules/plex-safety.md #9). A page refresh
  // before linking simply asks for the Plex login again.
  const [token, setToken] = useState<string | null>(null);
  const [plexUrl, setPlexUrl] = useState(data.plex_url ?? DEFAULT_PLEX_URL);
  const urlId = useId();

  const probe = useMutation({
    mutationFn: () =>
      api.setupProbe({ plex_url: plexUrl, plex_token: token ?? "" }),
  });

  const link = useMutation({
    mutationFn: (result: ProbeResult) =>
      api.setupLink({
        plex_url: plexUrl,
        plex_token: token ?? "",
        machine_id: result.machine_id,
        server_name: result.server_name,
        version: result.checks.pms_version.value ?? "",
        owner_account_id: result.owner_account_id,
        plex_pass: result.checks.plex_pass.ok,
      }),
    onSuccess: (_ignored, result) => {
      update({
        linked: true,
        plex_url: plexUrl,
        server_name: result.server_name,
      });
    },
  });

  if (data.linked) {
    return (
      <div className="space-y-3">
        <p className="inline-flex items-center gap-2 text-success">
          <Check className="h-5 w-5" aria-hidden="true" />
          Linked to {data.server_name ?? "your Plex server"}
        </p>
        <p className="text-sm text-muted-foreground">
          The server URL stays editable later under Settings → Connections. Hit
          Next to choose a history source.
        </p>
      </div>
    );
  }

  const requiredChecksPass =
    probe.data !== undefined &&
    probe.data.checks.pms_version.ok &&
    probe.data.checks.plex_pass.ok &&
    probe.data.checks.libraries.ok;

  return (
    <div className="space-y-6">
      {!token && (
        <PlexPinButton
          onLinked={(status) => {
            if (status.token) setToken(status.token);
          }}
        />
      )}

      {token && (
        <>
          <div className="space-y-2">
            <Label htmlFor={urlId}>Plex server URL</Label>
            <Input
              id={urlId}
              value={plexUrl}
              onChange={(event) => setPlexUrl(event.target.value)}
              placeholder={DEFAULT_PLEX_URL}
              autoComplete="off"
            />
            <p className="text-sm text-muted-foreground">
              Always editable — auto-discovery never traps you. Self-signed
              certificate? Use the http:// address, or flip the insecure toggle
              later in Settings → Connections.
            </p>
          </div>

          <Button
            onClick={() => probe.mutate()}
            disabled={probe.isPending || plexUrl.trim().length === 0}
          >
            {probe.isPending && (
              <Loader2 className="animate-spin" aria-hidden="true" />
            )}
            Run checks
          </Button>

          {probe.isError && (
            <p role="alert" className="text-sm text-destructive">
              {probe.error instanceof ApiError
                ? probe.error.message
                : "The checks could not run. Verify the URL and try again."}
            </p>
          )}

          {probe.data && (
            <Card>
              <CardContent className="space-y-4 pt-6">
                <p className="font-medium">
                  {probe.data.server_name}
                  <Badge variant="secondary" className="ml-2">
                    {probe.data.libraries.length} libraries
                  </Badge>
                </p>
                <ul className="space-y-2">
                  <CheckLine
                    label="Plex version"
                    check={probe.data.checks.pms_version}
                  />
                  <CheckLine
                    label="Plex Pass"
                    check={probe.data.checks.plex_pass}
                  />
                  <CheckLine
                    label="Libraries"
                    check={probe.data.checks.libraries}
                  />
                  {probe.data.checks.tautulli && (
                    <CheckLine
                      label="Tautulli"
                      check={probe.data.checks.tautulli}
                    />
                  )}
                </ul>
                {probe.data.libraries.length > 0 && (
                  <p className="text-sm text-muted-foreground">
                    {probe.data.libraries
                      .map((lib) => `${lib.title} (${lib.count} ${lib.type}s)`)
                      .join(" · ")}
                  </p>
                )}

                {requiredChecksPass ? (
                  <Button
                    onClick={() => link.mutate(probe.data)}
                    disabled={link.isPending}
                  >
                    {link.isPending && (
                      <Loader2 className="animate-spin" aria-hidden="true" />
                    )}
                    Link this server
                  </Button>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    Fix the failing checks above, then run the checks again.
                    Rowarr needs Plex ≥ 1.43.2 and Plex Pass for private rows.
                  </p>
                )}
                {link.isError && (
                  <p role="alert" className="text-sm text-destructive">
                    {link.error instanceof ApiError
                      ? link.error.message
                      : "Linking failed. Run the checks and try again."}
                  </p>
                )}
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
