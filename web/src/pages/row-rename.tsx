import { useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Pen } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useLocation, useParams } from "react-router";

import { BackLink } from "@/components/back-link";
import { MAX_SEEDS_LABEL } from "@/components/max-seeds-field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ProgressBar } from "@/components/ui/progress-bar";
import { api, apiUrl } from "@/lib/api";
import { useCollections } from "@/lib/queries";

interface RenameEvent {
  user?: string;
  display_name?: string;
  old?: string;
  new?: string;
  libraries?: string[];
  done?: boolean;
  total?: number;
  error?: string;
}

export function RowRenamePage() {
  const { id } = useParams();
  const collectionId = Number(id);
  const queryClient = useQueryClient();
  const collections = useCollections();
  const collection = collections.data?.find((c) => c.id === collectionId);
  const location = useLocation();
  const navState = location.state as { proposedName?: string } | null;

  // Arriving WITH a proposed name means the editor's Rename button sent us, and that click was the
  // decision — it is only enabled once the name actually differs from the saved one, and it sits
  // under a paragraph saying what renaming does. Asking again on arrival made the button mean
  // "go to a page where you can press rename", which is not what it says.
  //
  // Arriving with NO state is someone at the URL directly, who has decided nothing yet: they get
  // the form. That is the only path that still asks.
  const autoStart = !!navState?.proposedName?.trim();
  const [confirmed, setConfirmed] = useState(autoStart);
  const [newName, setNewName] = useState(
    // Carried from the editor when you typed a name there, so you don't retype it.
    navState?.proposedName ||
      collection?.name_template ||
      collection?.name ||
      "",
  );
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
          const event: RenameEvent = JSON.parse(dataLine.slice(6));
          if (event.error) {
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

  const renamed = events.filter((e) => e.user && !e.done);
  const doneEvent = events.find((e) => e.done);

  async function handleSubmit() {
    if (!collection) return;
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
      <BackLink to="/rows" label="Back to Rows" />
      <header className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Pen className="h-5 w-5" aria-hidden="true" />
          {confirmed
            ? `Renaming ${collection?.name || "row"}`
            : `Rename ${collection?.name || "row"}`}
        </h1>
        <p className="text-sm text-muted-foreground">
          {confirmed
            ? "Renaming every collection on Plex for every user who has this row."
            : "This renames every collection on Plex for every user who has this row."}
        </p>
      </header>

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
            {newName.includes("{top_seed}") && (
              <p className="rounded-md bg-muted/60 p-3 text-xs text-muted-foreground">
                A {"{top_seed}"} name promises the row is about one title. By
                default the row is built from their 30 most recent watches, so
                the name says one thing and the contents come from thirty. Lower{" "}
                <strong>{MAX_SEEDS_LABEL}</strong> in the row editor to make the
                name true &mdash; it tells you the right number for this row.
              </p>
            )}
          </div>
          <Button
            loading={saving}
            disabled={!newName.trim()}
            onClick={handleSubmit}
          >
            Rename on Plex
          </Button>
        </div>
      )}

      {running && (
        <ProgressBar
          done={renamed.length}
          total={undefined}
          label="Renaming collections"
        />
      )}

      {error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive-text">
          {error}
        </div>
      )}

      {doneEvent && !error && (
        <div className="flex items-center gap-2 rounded-lg border bg-success/10 p-4 text-sm text-success">
          <Check className="h-4 w-4" aria-hidden="true" />
          Done — renamed {doneEvent.total} collection
          {doneEvent.total === 1 ? "" : "s"} on Plex.
        </div>
      )}

      <div
        ref={logRef}
        className="max-h-[28rem] overflow-y-auto rounded-lg border bg-background"
      >
        {renamed.length === 0 && running && (
          <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Starting rename...
          </div>
        )}
        {renamed.length === 0 && !running && !error && (
          <p className="p-4 text-sm text-muted-foreground">
            Nothing to rename — every collection already has the correct title.
          </p>
        )}
        {renamed.map((e, i) => (
          <div
            key={i}
            className="flex items-center gap-3 border-b px-4 py-2 text-sm last:border-b-0"
          >
            <Check
              className="h-3.5 w-3.5 shrink-0 text-success"
              aria-hidden="true"
            />
            <span className="font-medium">{e.display_name || e.user}</span>
            <span className="text-muted-foreground">
              {e.old} → {e.new}
            </span>
            {e.libraries && e.libraries.length > 0 && (
              <Badge variant="secondary" className="ml-auto shrink-0">
                {e.libraries.join(", ")}
              </Badge>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
