import { useState } from "react";

import { Segmented } from "@/components/segmented";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useSettings } from "@/lib/queries";
import { SOURCES, sourceBlockedReason } from "@/lib/sources";
import type { Settings } from "@/lib/types";

/** The sources a row actually gathers from: its own override, else the server's set. Exported so
 *  the editor can hide settings that only affect a source this row does not use. */
export function effectiveSources(
  rowSources: string[],
  settings: Settings | undefined,
): string[] {
  return rowSources.length > 0 ? rowSources : globalSources(settings);
}

function globalSources(settings: Settings | undefined): string[] {
  const value = settings?.["candidates.sources"];
  return Array.isArray(value)
    ? value.filter((x): x is string => typeof x === "string")
    : ["tmdb_similar", "tmdb_discover"];
}

/** The inherited set, read back as names ("TMDB Similar and TMDB Discover"). Null until settings
 *  load, so the sentence stays true rather than naming a set this server may not have. */
function globalSourceLabels(settings: Settings | undefined): string | null {
  if (!settings) return null;
  const labels = globalSources(settings).map(
    (id) => SOURCES.find((source) => source.id === id)?.label ?? id,
  );
  if (labels.length === 0) return null;
  if (labels.length === 1) return labels[0] ?? null;
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

/**
 * Per-row discovery-source override. An empty array means "inherit the global Settings →
 * Recommendations set" (the default); choosing "Custom" seeds from the current global set so the
 * owner starts from what's active, then tweaks. Intent-based like the global picker: a source can be
 * ticked even if its key/curator isn't set up (a note says what's needed — the key itself is entered
 * globally, since it's not per-row); the engine no-ops it and the row card badges it "Needs setup".
 */
export function RowSourcesField({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const settings = useSettings();
  // The mode is the owner's CHOICE, not a guess from the list's length. Deriving it meant unticking
  // the last source silently threw the row back onto the global default, with the switches gone.
  const [mode, setMode] = useState<"global" | "custom">(
    value.length > 0 ? "custom" : "global",
  );
  const custom = mode === "custom";

  // Until the settings land we can't tell which sources are runnable, so the switches stay locked to
  // keep the dependency notes accurate. The owner's CHOICE persists regardless (intent) — a source
  // whose global key/curator isn't set yet no-ops in the engine and the row card badges it "Needs
  // setup", so it's never silently on-but-dead (matching the global Recommendations picker).
  const loaded = settings.data !== undefined;

  const chooseMode = (next: string) => {
    if (next === "global") {
      setMode("global");
      onChange([]);
    } else {
      setMode("custom");
      onChange(globalSources(settings.data)); // seed from the current global set as-is
    }
  };

  const toggle = (id: string) =>
    onChange(
      value.includes(id) ? value.filter((x) => x !== id) : [...value, id],
    );

  return (
    <div className="space-y-3 border-t pt-4">
      <Label>Recommendation sources</Label>
      <p className="text-sm text-muted-foreground">
        Where this row looks for titles. Pick different ones for different rows
        to give each its own character &mdash; a Trakt-only &ldquo;What to watch
        next&rdquo;, or an AI web search &ldquo;Everyone&rsquo;s talking about
        it&rdquo;.
      </p>
      <Segmented
        value={custom ? "custom" : "global"}
        onChange={chooseMode}
        options={[
          { value: "global", label: "Use global default" },
          { value: "custom", label: "Choose for this row" },
        ]}
      />
      {settings.isError && (
        <p role="alert" className="text-sm text-warning">
          Couldn&rsquo;t check which sources your connections allow, so they
          stay locked. Reload the page to try again.
        </p>
      )}
      {!custom ? (
        <p className="text-sm text-muted-foreground">
          This row uses the sources you enabled in Settings → Finding titles
          {globalSourceLabels(settings.data) &&
            ` — currently ${globalSourceLabels(settings.data)}`}
          .
        </p>
      ) : (
        <div className="space-y-3">
          {SOURCES.map((source) => {
            const blockedReason = settings.data
              ? sourceBlockedReason(source, settings.data)
              : null;
            const blocked = blockedReason !== null;
            return (
              <div
                key={source.id}
                className="flex items-start justify-between gap-4"
              >
                <div className="space-y-0.5">
                  <p className="text-sm font-medium">{source.label}</p>
                  <p className="text-sm text-muted-foreground">{source.desc}</p>
                  {blocked && (
                    <p className="text-xs text-warning">{blockedReason}</p>
                  )}
                </div>
                <Switch
                  checked={value.includes(source.id)}
                  disabled={!loaded}
                  onCheckedChange={() => toggle(source.id)}
                  aria-label={`Enable ${source.label} for this row`}
                />
              </div>
            );
          })}
          {value.length === 0 ? (
            // The row does fall back to the global set — but that must be said out loud, not
            // inferred from switches the owner just turned all the way off. Advisory, not an error,
            // so it's role="status".
            <p role="status" className="text-sm text-warning">
              Nothing ticked, so this row falls back to the sources you switched
              on in Settings → Finding titles, the set every row starts from.
              Tick at least one above to give this row its own.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              This row pools only the sources ticked above.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
