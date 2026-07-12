import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";
import { PlexPinButton } from "@/components/plex-pin-button";
import { queryKeys, useSession } from "@/lib/queries";
import type { PlexServer, ProbeCheck, ProbeResult } from "@/lib/types";

import type { StepProps } from "./step-props";

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

function connectionLabel(connection: PlexServer["connections"][number]) {
  if (connection.relay) return "relay";
  return connection.local ? "local" : "remote";
}

/**
 * Step 1 — connect your Plex account, pick the server, check it, link it.
 *
 * Signing in with Plex is not a gate in front of the wizard; it IS this step. There is nothing to
 * sign in TO until a server is linked, so a fresh install starts here and this is where you
 * connect the account. Your token never reaches the browser — the backend holds it — and once
 * you're connected this opens straight onto the servers your account can see, with every address
 * Plex advertises for them already tried. Only your network knows which one works from where
 * Rowarr runs, so we test rather than guess. The URL stays editable regardless.
 */
export function StepConnect({ data, update }: StepProps) {
  const [plexUrl, setPlexUrl] = useState(data.plex_url ?? "");
  const urlId = useId();
  const session = useSession();
  const queryClient = useQueryClient();
  const connected = session.data?.authenticated ?? false;

  const servers = useQuery({
    queryKey: ["setup", "servers"],
    queryFn: api.getServers,
    enabled: connected && !data.linked,
    staleTime: 30_000,
  });

  // Preselect the first address that actually answered — the common case is one click.
  useEffect(() => {
    if (plexUrl || !servers.data) return;
    for (const server of servers.data) {
      const working = server.connections.find((connection) => connection.ok);
      if (working) {
        setPlexUrl(working.uri);
        return;
      }
    }
  }, [servers.data, plexUrl]);

  const probe = useMutation({
    mutationFn: () => api.setupProbe({ plex_url: plexUrl }),
  });

  const link = useMutation({
    mutationFn: (result: ProbeResult) =>
      api.setupLink({
        plex_url: plexUrl,
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

  // Nothing here is possible until Rowarr can ask Plex what servers you have — so connecting the
  // account IS the first thing this step does. No password ever reaches Rowarr, and the token it
  // gets back stays on the server.
  if (!connected) {
    return (
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Connect the Plex account that owns your server. Rowarr asks Plex which
          servers you have — it never sees your password, and your Plex token
          stays on the server, never in this browser.
        </p>
        <PlexPinButton
          onLinked={() => {
            void queryClient.invalidateQueries({ queryKey: queryKeys.session });
          }}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <h2 className="text-sm font-medium">Your servers</h2>

        {servers.isPending ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Asking Plex which servers you have, and trying every address they
            advertise…
          </p>
        ) : null}

        {servers.isError ? (
          <p className="text-sm text-destructive" role="alert">
            {servers.error instanceof ApiError
              ? servers.error.message
              : "Could not reach plex.tv."}{" "}
            You can still type the address yourself below.
          </p>
        ) : null}

        {servers.data?.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Plex says this account owns no servers. If that&rsquo;s wrong, type
            the address below.
          </p>
        ) : null}

        <div className="space-y-3">
          {servers.data?.map((server) => (
            <Card
              key={server.machine_id}
              data-testid={`server-${server.machine_id}`}
            >
              <CardContent className="space-y-2 pt-6">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{server.name}</span>
                  {server.owned ? <Badge>owned</Badge> : null}
                  {server.version ? (
                    <span className="text-xs text-muted-foreground">
                      {server.version}
                    </span>
                  ) : null}
                </div>
                <fieldset className="space-y-1">
                  <legend className="sr-only">
                    Addresses for {server.name}
                  </legend>
                  {server.connections.map((connection) => (
                    <Button
                      key={connection.uri}
                      type="button"
                      variant={
                        plexUrl === connection.uri ? "default" : "outline"
                      }
                      size="sm"
                      aria-pressed={plexUrl === connection.uri}
                      disabled={!connection.ok}
                      onClick={() => setPlexUrl(connection.uri)}
                      className="mr-2 font-mono text-xs"
                    >
                      {connection.ok ? (
                        <Check className="h-3 w-3" aria-hidden="true" />
                      ) : (
                        <X className="h-3 w-3" aria-hidden="true" />
                      )}
                      {connection.uri}
                      <span className="ml-1 opacity-70">
                        ({connectionLabel(connection)}
                        {connection.ok ? "" : ", unreachable"})
                      </span>
                    </Button>
                  ))}
                </fieldset>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      <div className="space-y-2">
        <Label htmlFor={urlId}>Plex server URL</Label>
        <Input
          id={urlId}
          value={plexUrl}
          onChange={(event) => setPlexUrl(event.target.value)}
          placeholder="http://192.168.1.10:32400"
          autoComplete="off"
          className="font-mono"
        />
        <p className="text-sm text-muted-foreground">
          Always editable — auto-discovery never traps you. A self-signed
          certificate is fine: use the plain http:// address on your LAN.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => probe.mutate()}
          disabled={!plexUrl || probe.isPending}
        >
          {probe.isPending ? (
            <Loader2 className="animate-spin" aria-hidden="true" />
          ) : null}
          Run checks
        </Button>
        {requiredChecksPass && probe.data ? (
          <Button
            variant="default"
            onClick={() => link.mutate(probe.data)}
            disabled={link.isPending}
          >
            {link.isPending ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : null}
            Link this server
          </Button>
        ) : null}
      </div>

      {probe.isError ? (
        <p className="text-sm text-destructive" role="alert">
          {probe.error instanceof ApiError
            ? probe.error.message
            : "That server did not answer."}
        </p>
      ) : null}

      {probe.data ? (
        <Card>
          <CardContent className="space-y-3 pt-6">
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
            </ul>
            <p className="text-sm text-muted-foreground">
              {probe.data.libraries.length} libraries:{" "}
              {probe.data.libraries
                .map(
                  (library) =>
                    `${library.title} (${library.count} ${library.type}s)`,
                )
                .join(", ")}
            </p>
            {!requiredChecksPass ? (
              <p className="text-sm text-destructive">
                Rowarr can&rsquo;t keep rows private on this server yet — fix
                the failing check above, then run the checks again.
              </p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {link.isError ? (
        <p className="text-sm text-destructive" role="alert">
          {link.error instanceof ApiError
            ? link.error.message
            : "Could not link that server."}
        </p>
      ) : null}
    </div>
  );
}
