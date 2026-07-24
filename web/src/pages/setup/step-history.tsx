import { useMutation } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { TestResult } from "@/components/test-result";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api";
import { settingString } from "@/lib/format";
import { useSettings } from "@/lib/queries";

import type { StepProps } from "./step-props";

/**
 * Step 2 — required TMDB key + optional Tautulli. Watch history is read straight from Plex per user
 * (no configuration), so Tautulli here is only for the friendlier display names it knows people by.
 * Saving writes the settings then tests them; skipping just uses each account's Plex username.
 */
export function StepHistory({ data, update, next }: StepProps) {
  const [tmdbKey, setTmdbKey] = useState("");
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const tmdbId = useId();
  const urlId = useId();
  const keyId = useId();

  // Re-entering this step (Back/Next) remounts it, so seed the fields from what's already saved,
  // once, when settings arrive. Keys come back redacted as "•••••" — that's what shows, and the
  // backend treats a re-sent "•••••" as "no change", so nothing gets clobbered on save.
  const settings = useSettings();
  const seeded = useRef(false);
  useEffect(() => {
    const saved = settings.data;
    if (seeded.current || !saved) return;
    seeded.current = true;
    // Functional updaters: if the fetch was slow and the owner already typed something, their input
    // wins; an empty saved value never overwrites what they entered.
    setTmdbKey((cur) => cur || settingString(saved, "tmdb.apikey"));
    setUrl((cur) => cur || settingString(saved, "tautulli.url"));
    setApiKey((cur) => cur || settingString(saved, "tautulli.apikey"));
  }, [settings.data]);

  // TMDB is not optional: it is how Shortlist finds titles similar to what someone watched, which
  // it then narrows down to what you actually own. Without it every run fails at the first user.
  const saveTmdb = useMutation({
    mutationFn: async () => {
      await api.putSettings({ "tmdb.apikey": tmdbKey });
      return api.testConnection("tmdb");
    },
    // Track the CURRENT key's validity: a failed test must clear the flag (not just leave the last
    // success standing), so an invalid key blocks Next instead of sailing through on a stale pass.
    onSuccess: (result) => update({ tmdb_set: result.ok === true }),
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
          onChange={(event) => {
            setTmdbKey(event.target.value);
            // Editing a previously-validated key un-verifies it: Next must wait for a fresh Save &
            // test, so you can't sail through on the old key's pass after changing it.
            if (data.tmdb_set) update({ tmdb_set: false });
          }}
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
          Watch history comes straight from Plex, per user, with no setup.
          Tautulli is only used for the friendlier names it knows people by
          &mdash; skip it and Shortlist uses each account&rsquo;s Plex username.
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
          Skip — use Plex usernames
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
        <Badge variant="success">Using Tautulli for display names</Badge>
      )}
      {data.history_source === "plex" && (
        <Badge variant="secondary">Using Plex usernames</Badge>
      )}
    </div>
  );
}
