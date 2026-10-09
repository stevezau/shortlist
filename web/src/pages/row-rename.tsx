import { useQueryClient } from "@tanstack/react-query";
import { Check, Clock, Loader2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router";

import { MAX_SEEDS_LABEL } from "@/components/max-seeds-field";
import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { RowName } from "@/components/rows/row-name";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ProgressBar } from "@/components/ui/progress-bar";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiUrl } from "@/lib/api";
import { TOP_SEED } from "@/lib/placeholders";
import { useCollections, useUsers } from "@/lib/queries";
import type { Collection, User } from "@/lib/types";

interface RenameEvent {
  user?: string;
  display_name?: string;
  old?: string;
  new?: string;
  libraries?: string[];
  library?: string;
  /** Plex only lets a new collection share this name, so the row's next run rebuilds it under it. */
  next_run?: boolean;
  done?: boolean;
  total?: number;
  /** With `user`: that one person's collection could not be renamed, and the rest carry on. */
  error?: string;
}

/**
 * How many people this row is built for: its audience, less anyone switched off or gone from the
 * server. It is the most a rename can reach — someone with too little history may have no collection
 * yet — and the stream's own total says how many were renamed in the end. An exact count before the
 * click needs a preview the server does not offer: the rename stream's `dry_run` saves the new name
 * before it previews anything.
 */
function renameReach(collection: Collection, users: User[]): number {
  const live = users.filter((user) => user.enabled && !user.departed);
  if (collection.audience === "everyone") return live.length;
  const audience = new Set(collection.audience_user_ids);
  return live.filter((user) => audience.has(user.id)).length;
}

function reachSentence(collection: Collection, people: number): string {
  if (people === 0) return "Nobody enabled has this row yet, so only its name changes.";
  const who = `${people} ${people === 1 ? "person" : "people"}`;
  return collection.build === "shared"
    ? `Renames this row’s shared collection, seen by ${who}.`
    : `Renames this row’s collection for ${who}.`;
}

export function RowRenamePage() {
  const { id } = useParams();
  const collectionId = Number(id);
  const queryClient = useQueryClient();
  const collections = useCollections();
  const collection = collections.data?.find((c) => c.id === collectionId);
  const users = useUsers();
  const location = useLocation();
  const navState = location.state as {
    proposedName?: string;
    /** With `alreadySaved`: the title the collections on Plex still carry. */
    oldTemplate?: string;
    /** The editor saved this name already, with the kind switch that needed it. */
    alreadySaved?: boolean;
  } | null;

  // Arriving WITH a proposed name means the editor's Rename button sent us, and that click was the
  // decision — it is only enabled once the name actually differs from the saved one, and it sits
  // under a paragraph saying what renaming does. Asking again on arrival made the button mean
  // "go to a page where you can press rename", which is not what it says.
  //
  // Arriving with NO state is someone at the URL directly, who has decided nothing yet: they get
  // the form. That is the only path that still asks.
  const autoStart = !!navState?.proposedName?.trim();
  const [confirmed, setConfirmed] = useState(autoStart);
  const [newNameDraft, setNewName] = useState<string | null>(
    // Carried from the editor when you typed a name there, so you don't retype it.
    navState?.proposedName ||
      collection?.name_template ||
      collection?.name ||
      null,
  );
  const newName = newNameDraft ?? (collection?.name_template || collection?.name || "");
  const [saving, setSaving] = useState(false);

  const [events, setEvents] = useState<RenameEvent[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  async function startRename(template: string, prevTemplate: string) {
    setRunning(true);
    setSaving(false);
    try {
      const response = await fetch(
        apiUrl(`/api/collections/${collectionId}/rename`),
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-shortlist-csrf": "1",
          },
          credentials: "include",
          body: JSON.stringify({
            name_template: template,
            old_template: prevTemplate,
          }),
        },
      );
      if (!response.ok || !response.body) {
        setError(`Server returned ${response.status}`);
        setRunning(false);
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() ?? "";
        for (const chunk of lines) {
          const dataLine = chunk
            .split("\n")
            .find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          // One malformed chunk must not abort the display: the server is still renaming on Plex.
          let event: RenameEvent;
          try {
            event = JSON.parse(dataLine.slice(6));
          } catch {
            continue;
          }
          // Only an error about the whole rename stops it. One person's refusal is theirs: the server
          // carries on with everyone else, and stopping here hid every rename that followed it.
          if (event.error && !event.user) {
            setError(event.error);
            setRunning(false);
            return;
          }
          setEvents((prev) => [...prev, event]);
          if (event.done) {
            setRunning(false);
            queryClient.invalidateQueries({ queryKey: ["collections"] });
          }
        }
      }
      setRunning(false);
      queryClient.invalidateQueries({ queryKey: ["collections"] });
    } catch (e) {
      setError((e as Error).message);
      setRunning(false);
    }
  }

  // Start as soon as the collection has loaded. `handleSubmit`, NOT `startRename`: the name still
  // has to be SAVED before it is applied to Plex. The old auto-start called `startRename("", prev)`
  // because the card's dialog had already saved it on the way here — that dialog is gone, and
  // reusing its path from the editor would have streamed a rename to the name already on record.
  //
  // Guarded by a ref rather than by `running`/`events`, so a re-render between the click and the
  // first streamed event cannot start a second rename over the top of the first.
  const started = useRef(false);
  useEffect(() => {
    if (autoStart && collection && !started.current) {
      started.current = true;
      handleSubmit();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStart, collection]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [events]);

  const results = events.filter((e) => e.user && !e.done);
  const refused = results.filter((e) => e.error);
  const nextRun = results.filter((e) => !e.error && e.next_run);
  const doneEvent = events.find((e) => e.done);

  async function handleSubmit() {
    if (!collection) return;
    // A kind switch saved the new name together with the settings that needed it (a season name the
    // API refuses once the row follows no season can't be saved first and renamed after). The row on
    // record is already renamed, so only the stream is left, from the title Plex still shows.
    if (navState?.alreadySaved && navState.oldTemplate) {
      setConfirmed(true);
      startRename(newName, navState.oldTemplate);
      return;
    }
    const prev = collection.name_template || collection.name;
    setSaving(true);
    try {
      await api.updateCollection(collection.id, {
        name: newName,
        name_template: newName,
        // This page streams the rename itself below. Without this the PATCH also renamed everything
        // inline, so the stream found nothing left to do and told the owner "renamed 0 collections"
        // straight after a rename that had in fact rewritten every one.
        defer_rename: true,
      });
      setConfirmed(true);
      startRename(newName, prev);
    } catch (e) {
      setError((e as Error).message);
      setSaving(false);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        className="mb-0"
        title={
          <>
            <Link to="/rows" className="font-normal text-muted-foreground hover:text-foreground">
              Rows
            </Link>
            <span className="font-normal text-faint-foreground">{" / "}</span>
            {confirmed ? "Renaming " : "Rename "}
            {collection?.name ? <RowName name={collection.name} className="" /> : "row"}
          </>
        }
        subtitle={
          confirmed
            ? "Renaming every collection on Plex for every user who has this row."
            : "This renames every collection on Plex for every user who has this row."
        }
      />

      {/* Loading, a failed fetch, and "the URL names a row that does not exist" all fell through
          this `collection &&` guard and rendered the header above a blank space — no skeleton, no
          error, no "that row is gone". Three answers, one silence. */}
      {!confirmed && collections.isPending && (
        <div className="max-w-md space-y-3">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-32" />
        </div>
      )}
      {!confirmed && collections.isError && (
        <div className="max-w-md space-y-2">
          <p className="text-sm text-destructive-text" role="alert">
            Couldn&rsquo;t load this row.
          </p>
          <Button variant="outline" onClick={() => collections.refetch()}>
            Try again
          </Button>
        </div>
      )}
      {!confirmed && collections.isSuccess && !collection && (
        <div className="max-w-md space-y-2">
          <p className="text-sm text-muted-foreground">
            That row no longer exists — it may have been deleted.
          </p>
          <Button variant="outline" asChild>
            <Link to="/rows">Back to rows</Link>
          </Button>
        </div>
      )}
      {!confirmed && collection && (
        <div className="max-w-md space-y-3">
          <div className="space-y-2">
            <Label htmlFor="rename-input">New name</Label>
            <Input
              id="rename-input"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="e.g. ✨ {library_name} Picked for You"
            />
            <p className="text-xs text-muted-foreground">
              Use {"{library_name}"} for the library, {"{user}"} for each
              person's name, {"{top_seed}"} for a title they recently watched.
            </p>
            {newName.includes(TOP_SEED) && (
              <p className="rounded-md bg-muted/60 p-3 text-xs text-muted-foreground">
                A {"{top_seed}"} name promises the row is about one title. By
                default the row is built from their 30 most recent watches, so
                the name says one thing and the contents come from thirty. Lower{" "}
                <strong>{MAX_SEEDS_LABEL}</strong> in the row editor to make the
                name true &mdash; it tells you the right number for this row.
              </p>
            )}
          </div>
          {/* The reach comes before the button, and the button waits for it: a rename retitles a
              collection on every account that has this row, so the number is the decision. */}
          {users.isPending && (
            <p role="status" className="text-sm text-muted-foreground">
              Counting the people this row reaches…
            </p>
          )}
          {users.isError && (
            <MutationAlert
              error={users.error}
              lead="Rename stays locked until this count loads."
              fallback="Couldn’t count the people this row reaches. Try again."
              onRetry={() => void users.refetch()}
              retryDisabled={users.isFetching}
            />
          )}
          {users.data && (
            <p className="text-sm font-medium">{reachSentence(collection, renameReach(collection, users.data))}</p>
          )}
          <Button
            loading={saving}
            disabled={!newName.trim() || !users.isSuccess}
            onClick={handleSubmit}
          >
            Rename on Plex
          </Button>
        </div>
      )}

      {running && (
        <ProgressBar
          done={results.length}
          total={undefined}
          label="Renaming collections"
        />
      )}

      {error && (
        <div className="space-y-1 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive-text">
          {/* The editor already saved the name, so "Rename" failing here is not "nothing happened":
              the row carries its new name, and its next delivery retitles the collections. */}
          {navState?.alreadySaved && (
            <p className="font-medium">
              {
                "Your settings and the new name were saved, but the rename didn't finish on Plex. It's applied the next time the row runs."
              }
            </p>
          )}
          <p>{error}</p>
        </div>
      )}

      {doneEvent && !error && (
        <div className="flex items-center gap-2 rounded-lg border bg-success/10 p-4 text-sm text-success">
          <Check className="h-4 w-4" aria-hidden="true" />
          <span>
            Done — renamed {doneEvent.total} collection
            {doneEvent.total === 1 ? "" : "s"} on Plex.
            {nextRun.length > 0 &&
              ` ${nextRun.length} take${nextRun.length === 1 ? "s" : ""} the new name at this row's next run.`}
            {refused.length > 0 && ` ${refused.length} could not be renamed.`}
          </span>
        </div>
      )}

      <div
        ref={logRef}
        className="max-h-[28rem] overflow-y-auto rounded-lg border bg-background"
      >
        {results.length === 0 && running && (
          <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Starting rename...
          </div>
        )}
        {results.length === 0 && !running && !error && doneEvent && (
          <p className="p-4 text-sm text-muted-foreground">
            Nothing to rename — every collection already has the correct title.
          </p>
        )}
        {results.length > 0 && (
          <ul aria-label="Rename results">
            {results.map((e, i) => (
              <li
                key={i}
                className="flex min-w-0 flex-wrap items-center gap-3 border-b px-4 py-2 text-sm [overflow-wrap:anywhere] last:border-b-0"
              >
                {e.error ? (
                  <X
                    className="h-3.5 w-3.5 shrink-0 text-destructive-text"
                    aria-hidden="true"
                  />
                ) : e.next_run ? (
                  <Clock
                    className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
                    aria-hidden="true"
                  />
                ) : (
                  <Check
                    className="h-3.5 w-3.5 shrink-0 text-success"
                    aria-hidden="true"
                  />
                )}
                {e.error ? (
                  <span className="text-destructive-text">
                    {e.display_name ? `${e.display_name}: ` : ""}
                    {e.error}
                  </span>
                ) : (
                  <>
                    <span className="font-medium">
                      {e.display_name || e.user}
                    </span>
                    <span className="text-muted-foreground">
                      {e.next_run
                        ? `takes “${e.new}” at this row's next run`
                        : `${e.old} → ${e.new}`}
                    </span>
                  </>
                )}
                {(e.libraries?.length || e.library) && (
                  <Badge variant="secondary" className="ml-auto max-w-full whitespace-normal">
                    {e.libraries?.join(", ") || e.library}
                  </Badge>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
