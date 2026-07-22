import { useMutation } from "@tanstack/react-query";
import { PlugZap } from "lucide-react";
import { useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { TestResult } from "@/components/test-result";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { useAutosavedSettings } from "@/lib/autosave";
import type { Settings } from "@/lib/types";

/**
 * Opt-in: read watched flags straight from the Plex database.
 *
 * Plex's history API only reports things people actually PLAYED. Anything marked watched — a bulk
 * "mark season as watched", a title ticked off without playing it — is invisible to it, so those
 * titles keep getting recommended back. The PMS database is the only place that state exists.
 *
 * Deliberately off by default and never auto-detected: it needs Shortlist on the same machine as
 * Plex with the file mounted, and reading someone's Plex database should be a choice they make, not
 * something that quietly starts happening.
 */
export function PlexDbCard({ settings }: { settings: Settings }) {
  const [path, setPath] = useState(
    (settings["plex.db_path"] as string | undefined) ?? "",
  );
  const save = useAutosavedSettings({ path }, () => ({
    "plex.db_path": path.trim(),
  }));
  const test = useMutation({ mutationFn: () => api.testConnection("plexdb") });

  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <div className="space-y-1">
          <Label htmlFor="plex-db-path" className="font-medium">
            Read watched state from the Plex database
          </Label>
          <p className="text-sm text-muted-foreground">
            Plex&rsquo;s history only records what people actually{" "}
            <em>played</em>. Anything <strong>marked</strong> watched — ticking
            a film off, marking a whole season — leaves no play record, so
            Shortlist can&rsquo;t see it and may recommend it back.
            <br />
            Pointing this at your Plex database fixes that for everyone on the
            server at once. It is opened <strong>read-only</strong> and nothing
            is ever written to it.
            <br />
            Only possible if Shortlist runs on the same machine as Plex, with
            the database folder mounted. Leave it empty to skip this.
          </p>
        </div>
        <Input
          id="plex-db-path"
          value={path}
          spellCheck={false}
          placeholder="/plexdb/com.plexapp.plugins.library.db"
          onChange={(e) => setPath(e.target.value)}
        />
        {/* A wrong path otherwise saves cleanly and the feature just never does anything — one
            warning per user, buried in a 49-user run log. Test says so immediately. */}
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => test.mutate()}
            loading={test.isPending}
            disabled={!path.trim()}
          >
            {!test.isPending && <PlugZap aria-hidden="true" />}
            Test
          </Button>
        </div>
        {test.isSuccess && <TestResult result={test.data} />}
        {test.isError && <TestResult error={test.error} />}
        <p className="text-xs text-muted-foreground">
          The folder works too. On a standard install the file lives in{" "}
          <code>
            Plex Media Server/Plug-in
            Support/Databases/com.plexapp.plugins.library.db
          </code>
          .
        </p>
        <SaveStatus
          isPending={save.isPending}
          isError={save.isError}
          error={save.error}
          saved={save.saved}
          onRetry={save.retry}
        />
      </CardContent>
    </Card>
  );
}
