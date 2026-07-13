import { useMutation } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useId, useState } from "react";

import { TestResult } from "@/components/test-result";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api";

import type { StepProps } from "./step-props";

/**
 * Step 2 — optional Tautulli. Saving writes the settings then tests them;
 * skipping falls back to Plex's own history API (always works, zero config).
 */
export function StepHistory({ data, update, next }: StepProps) {
  const [tmdbKey, setTmdbKey] = useState("");
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const tmdbId = useId();
  const urlId = useId();
  const keyId = useId();

  // TMDB is not optional: it is how Shortlist finds titles similar to what someone watched, which
  // it then narrows down to what you actually own. Without it every run fails at the first user.
  const saveTmdb = useMutation({
    mutationFn: async () => {
      await api.putSettings({ "tmdb.apikey": tmdbKey });
      return api.testConnection("tmdb");
    },
    onSuccess: (result) => {
      if (result.ok) update({ tmdb_set: true });
    },
  });

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
        <Label htmlFor={tmdbId}>TMDB API key (required)</Label>
        <Input
          id={tmdbId}
          type="password"
          value={tmdbKey}
          onChange={(event) => setTmdbKey(event.target.value)}
          autoComplete="off"
        />
        <p className="text-sm text-muted-foreground">
          Shortlist asks TMDB which titles are similar to the ones each user
          watched, then keeps only the ones already in your library. A key is
          free from themoviedb.org → Settings → API.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          onClick={() => saveTmdb.mutate()}
          disabled={saveTmdb.isPending || tmdbKey.trim().length === 0}
        >
          {saveTmdb.isPending && (
            <Loader2 className="animate-spin" aria-hidden="true" />
          )}
          Save TMDB key
        </Button>
        {data.tmdb_set && <Badge variant="success">TMDB key works</Badge>}
      </div>

      {saveTmdb.isSuccess && !saveTmdb.data.ok && (
        <p role="alert" className="text-sm text-destructive">
          {saveTmdb.data.message}
        </p>
      )}
      {saveTmdb.isError && (
        <p role="alert" className="text-sm text-destructive">
          {apiErrorMessage(saveTmdb.error, "Could not save that TMDB key.")}
        </p>
      )}

      <hr className="border-border" />

      <div className="space-y-1">
        <h3 className="text-sm font-medium">
          Tautulli{" "}
          <span className="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <p className="text-sm text-muted-foreground">
          Tautulli gives richer, more accurate watch history. Skip it and
          Shortlist uses Plex&rsquo;s own history instead — that always works
          with no setup, so you can move on without it.
        </p>
      </div>

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
          disabled={!data.tmdb_set}
          onClick={() => {
            update({ history_source: "plex" });
            next();
          }}
        >
          Skip — Plex's own history works without it
        </Button>
      </div>

      {saveAndTest.isSuccess && <TestResult result={saveAndTest.data} />}
      {saveAndTest.isError && (
        <TestResult
          error={saveAndTest.error}
          errorFallback="Could not reach Tautulli. Check the URL and key."
        />
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
