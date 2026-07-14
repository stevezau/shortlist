import { Segmented } from "@/components/segmented";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useSettings } from "@/lib/queries";
import { SOURCES, sourceBlockedReason } from "@/lib/sources";
import type { Settings } from "@/lib/types";

function globalSources(settings: Settings | undefined): string[] {
  const value = settings?.["candidates.sources"];
  return Array.isArray(value)
    ? value.filter((x): x is string => typeof x === "string")
    : ["tmdb_similar", "tmdb_discover"];
}

/**
 * Per-row discovery-source override. An empty array means "inherit the global Settings →
 * Recommendations set" (the default); choosing "Custom" seeds from the current global set so the
 * owner starts from what's active, then tweaks. Dependency-gated exactly like the global picker.
 */
export function RowSourcesField({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const settings = useSettings();
  const custom = value.length > 0;

  const setMode = (mode: string) => {
    if (mode === "global") onChange([]);
    else onChange(globalSources(settings.data)); // seed Custom from the current global set
  };

  const toggle = (id: string) =>
    onChange(
      value.includes(id) ? value.filter((x) => x !== id) : [...value, id],
    );

  return (
    <div className="space-y-3 border-t pt-4">
      <Label>Where this row looks</Label>
      <Segmented
        value={custom ? "custom" : "global"}
        onChange={setMode}
        options={[
          { value: "global", label: "Use global default" },
          { value: "custom", label: "Choose for this row" },
        ]}
      />
      {!custom ? (
        <p className="text-sm text-muted-foreground">
          This row uses the sources you enabled in Settings → Recommendations.
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
                  checked={value.includes(source.id) && !blocked}
                  disabled={blocked}
                  onCheckedChange={() => toggle(source.id)}
                  aria-label={`Enable ${source.label} for this row`}
                />
              </div>
            );
          })}
          <p className="text-xs text-muted-foreground">
            An empty selection falls back to the global default.
          </p>
        </div>
      )}
    </div>
  );
}
