import { useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Pen } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useLocation, useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { Badge } from "@/components/ui/badge";
import { ProgressBar } from "@/components/ui/progress-bar";
import { apiUrl } from "@/lib/api";
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
  const oldTemplate = (location.state as { oldTemplate?: string } | null)
    ?.oldTemplate;
  const [events, setEvents] = useState<RenameEvent[]>([]);
  const [running, setRunning] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!collection) return;
    const controller = new AbortController();

    async function stream() {
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
              name_template: "",
              old_template: oldTemplate ?? "",
            }),
            signal: controller.signal,
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
        if ((e as Error).name !== "AbortError") {
          setError((e as Error).message);
          setRunning(false);
        }
      }
    }

    stream();
    return () => controller.abort();
  }, [collection, collectionId]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [events]);

  const renamed = events.filter((e) => e.user && !e.done);
  const doneEvent = events.find((e) => e.done);

  return (
    <div className="space-y-6">
      <BackLink to="/rows" label="Back to Rows" />
      <header className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Pen className="h-5 w-5" aria-hidden="true" />
          Renaming {collection?.name || "row"}
        </h1>
        <p className="text-sm text-muted-foreground">
          Renaming every collection on Plex for every user who has this row.
        </p>
      </header>

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
