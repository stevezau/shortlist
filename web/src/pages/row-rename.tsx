import { useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Pen } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useLocation, useParams } from "react-router";

import { BackLink } from "@/components/back-link";
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
  const navOldTemplate = (location.state as { oldTemplate?: string } | null)
    ?.oldTemplate;

  // If we arrived from the row-card dialog, oldTemplate is set and we auto-start.
  // If we arrived from the editor's Rename button, we need to ask first.
  const [confirmed, setConfirmed] = useState(!!navOldTemplate);
  const [newName, setNewName] = useState(
    collection?.name_template || collection?.name || "",
  );
  const [saving, setSaving] = useState(false);
  const [oldTemplate, setOldTemplate] = useState(navOldTemplate ?? "");

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

  // Auto-start if we came from the row-card dialog (oldTemplate is set).
  // Suppressed deliberately: this fires an async mutation on mount, not a state sync. Triggering
  // work when a condition first holds is exactly what an effect is for.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (confirmed && collection && !running && events.length === 0) {
      startRename("", oldTemplate);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmed, collection]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
      } as never);
      setOldTemplate(prev);
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
              person's name.
            </p>
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
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
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
