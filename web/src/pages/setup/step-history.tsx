import { useMutation } from "@tanstack/react-query";
import { Check, Loader2 } from "lucide-react";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";

import type { StepProps } from "./step-props";

/**
 * Step 2 — optional Tautulli. Saving writes the settings then tests them;
 * skipping falls back to Plex's own history API (always works, zero config).
 */
export function StepHistory({ data, update, next }: StepProps) {
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const urlId = useId();
  const keyId = useId();

  const saveAndTest = useMutation({
    mutationFn: async () => {
      await api.putSettings({
        "tautulli.url": url,
        "tautulli.apikey": apiKey,
      });
      return api.testConnection("tautulli");
    },
    onSuccess: (result) => {
      if (result.ok) update({ history_source: "tautulli" });
    },
  });

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Label htmlFor={urlId}>Tautulli URL</Label>
        <Input
          id={urlId}
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="http://localhost:8181"
          autoComplete="off"
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor={keyId}>Tautulli API key</Label>
        <Input
          id={keyId}
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          autoComplete="off"
        />
        <p className="text-sm text-muted-foreground">
          Settings → Web Interface → API key, inside Tautulli.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          onClick={() => saveAndTest.mutate()}
          disabled={
            saveAndTest.isPending ||
            url.trim().length === 0 ||
            apiKey.trim().length === 0
          }
        >
          {saveAndTest.isPending && (
            <Loader2 className="animate-spin" aria-hidden="true" />
          )}
          Save & test
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            update({ history_source: "plex" });
            next();
          }}
        >
          Skip — Plex's own history works without it
        </Button>
      </div>

      {saveAndTest.isSuccess &&
        (saveAndTest.data.ok ? (
          <p className="inline-flex items-center gap-2 text-sm text-success">
            <Check className="h-4 w-4" aria-hidden="true" />
            {saveAndTest.data.message}
          </p>
        ) : (
          <p role="alert" className="text-sm text-destructive">
            {saveAndTest.data.message}
          </p>
        ))}
      {saveAndTest.isError && (
        <p role="alert" className="text-sm text-destructive">
          {saveAndTest.error instanceof ApiError
            ? saveAndTest.error.message
            : "Could not reach Tautulli. Check the URL and key."}
        </p>
      )}

      {data.history_source === "tautulli" && (
        <Badge variant="success">Using Tautulli for watch history</Badge>
      )}
      {data.history_source === "plex" && (
        <Badge variant="secondary">Using Plex's built-in history</Badge>
      )}
    </div>
  );
}
