import { useMutation } from "@tanstack/react-query";
import { ArrowUpRight, CheckCircle2, Loader2 } from "lucide-react";
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
 * Step 2 — required TMDB key (with signup walkthrough) + optional Tautulli, collapsed. Watch history is read straight from Plex per user
 * (no configuration), so Tautulli here is only for the friendlier display names it knows people by.
 * Testing writes the settings then tests them; leaving Tautulli alone just uses each account's Plex username.
 */
export function StepTmdb({ data, update }: StepProps) {
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
      <ol className="divide-y rounded-xl border bg-card shadow-elevated">
        <li className="flex flex-wrap items-center gap-4 px-5 py-4">
          <StepNumber n={1} />
          <p className="min-w-0 flex-1 basis-56 font-medium">Create a free account</p>
          <Button variant="outline" asChild>
            <a href="https://www.themoviedb.org/signup" target="_blank" rel="noreferrer">
              Open themoviedb.org/signup
              <ArrowUpRight aria-hidden="true" />
            </a>
          </Button>
        </li>
        <li className="flex items-start gap-4 px-5 py-4">
          <StepNumber n={2} />
          <div>
            <p className="font-medium">Request an API key</p>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Settings &rarr; API &rarr; Request an API key &rarr; Developer, personal use.
            </p>
          </div>
        </li>
        <li className="flex items-start gap-4 px-5 py-4">
          <StepNumber n={3} />
          <div>
            <p className="font-medium">Copy the API Key</p>
            <p className="mt-0.5 text-sm text-muted-foreground">
              It is 32 characters &mdash; not the long Read Access Token.
            </p>
          </div>
        </li>
      </ol>

      <div className="space-y-2">
        <Label htmlFor={tmdbId}>TMDB API key</Label>
        <div className="flex flex-wrap gap-2">
          <Input
            id={tmdbId}
            type="password"
            value={tmdbKey}
            onChange={(event) => {
              setTmdbKey(event.target.value);
              // Editing a previously-validated key un-verifies it: Next must wait for a fresh test,
              // so you can't sail through on the old key's pass after changing it.
              if (data.tmdb_set) update({ tmdb_set: false });
            }}
            placeholder="32 characters"
            autoComplete="off"
            className="min-w-0 flex-1 basis-64"
          />
          <Button
            variant="outline"
            onClick={() => saveTmdb.mutate()}
            disabled={saveTmdb.isPending || tmdbKey.trim().length === 0}
          >
            {saveTmdb.isPending && <Loader2 className="animate-spin" aria-hidden="true" />}
            Test key
          </Button>
        </div>
        {data.tmdb_set && (
          <p className="flex items-center gap-2 text-sm text-success">
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            TMDB key works
          </p>
        )}
        {saveTmdb.isSuccess && !saveTmdb.data.ok && (
          <p role="alert" className="text-sm text-destructive-text">
            {saveTmdb.data.message}
          </p>
        )}
        {saveTmdb.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(saveTmdb.error, "Could not save that TMDB key.")}
          </p>
        )}
      </div>

      {/* Optional, so it costs one line until someone opens it. Open by default when already
          connected, so a returning owner sees what is saved. */}
      <details className="group rounded-xl border bg-card" open={data.history_source === "tautulli" || undefined}>
        <summary className="cursor-pointer px-5 py-3 text-sm font-medium text-muted-foreground">
          Use Tautulli for friendlier names (optional)
        </summary>
        <div className="space-y-4 border-t px-5 py-4">
          <p className="text-sm text-muted-foreground">
            Watch history comes straight from Plex, per user, with no setup. Tautulli is only used for
            the friendlier names it knows people by &mdash; skip it and Shortlist uses each
            account&rsquo;s Plex username.
          </p>
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
          <Button
            variant="outline"
            onClick={() => saveAndTest.mutate()}
            disabled={saveAndTest.isPending || url.trim().length === 0 || apiKey.trim().length === 0}
          >
            {saveAndTest.isPending && <Loader2 className="animate-spin" aria-hidden="true" />}
            Save & test
          </Button>
          {saveAndTest.isSuccess && <TestResult result={saveAndTest.data} />}
          {saveAndTest.isError && (
            <TestResult
              error={saveAndTest.error}
              errorFallback="Could not reach Tautulli. Check the URL and key."
            />
          )}
          {data.history_source === "tautulli" && <Badge variant="success">Using Tautulli for display names</Badge>}
        </div>
      </details>
    </div>
  );
}

function StepNumber({ n }: { n: number }) {
  return (
    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-raised text-sm font-semibold">
      {n}
    </span>
  );
}
