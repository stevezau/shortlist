import { useMutation } from "@tanstack/react-query";
import { RotateCcw, ScanEye } from "lucide-react";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { PROMPT_TONES } from "@/lib/types";

const TONE_LABELS: Record<string, string> = {
  balanced: "Balanced",
  warm: "Warm",
  concise: "Concise",
  cinephile: "Cinephile",
  playful: "Playful",
};

export interface CurationStyleValue {
  tone: string;
  guidance: string;
  template: string;
}

export function CurationStyleFields({
  value,
  onChange,
  perPerson = false,
  globalDefaults,
}: {
  value: CurationStyleValue;
  onChange: (next: CurationStyleValue) => void;
  /** Per-person forms add a "Use default" tone and frame guidance as additive. */
  perPerson?: boolean;
  /** The global recipe, so a per-person preview shows the *effective* prompt (default + override). */
  globalDefaults?: CurationStyleValue;
}) {
  const [showAdvanced, setShowAdvanced] = useState(Boolean(value.template));
  const guidanceId = useId();
  const templateId = useId();

  // Preview the effective prompt. For a per-person form that means merging the override over the
  // global default the same way the backend does (tone/template: override wins; guidance: additive).
  const effective = (): CurationStyleValue => {
    if (!perPerson || !globalDefaults) return value;
    return {
      tone: value.tone || globalDefaults.tone,
      guidance: [globalDefaults.guidance, value.guidance]
        .map((g) => g.trim())
        .filter(Boolean)
        .join("\n"),
      template: value.template || globalDefaults.template,
    };
  };
  const preview = useMutation({
    mutationFn: () => api.previewPrompt(effective()),
  });

  const toneOptions = perPerson ? ["", ...PROMPT_TONES] : [...PROMPT_TONES];

  return (
    <div className="space-y-4">
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Tone</legend>
        <div className="flex flex-wrap gap-2">
          {toneOptions.map((tone) => (
            <Button
              key={tone || "default"}
              type="button"
              size="sm"
              variant={value.tone === tone ? "default" : "outline"}
              aria-pressed={value.tone === tone}
              onClick={() => onChange({ ...value, tone })}
            >
              {tone === "" ? "Use default" : (TONE_LABELS[tone] ?? tone)}
            </Button>
          ))}
        </div>
      </fieldset>

      <div className="space-y-2">
        <Label htmlFor={guidanceId}>
          Guidance{" "}
          {perPerson && (
            <span className="font-normal text-muted-foreground">
              — added to the global guidance
            </span>
          )}
        </Label>
        <Textarea
          id={guidanceId}
          value={value.guidance}
          placeholder="e.g. Prefer hidden gems over blockbusters. Keep the reasons family-friendly."
          onChange={(event) =>
            onChange({ ...value, guidance: event.target.value })
          }
        />
        <p className="text-sm text-muted-foreground">
          Plain-English notes for the AI. It can only ever suggest titles
          already in your library.
        </p>
      </div>

      <div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setShowAdvanced((open) => !open)}
        >
          {showAdvanced
            ? "Hide advanced"
            : "Advanced — write the prompt yourself"}
        </Button>
      </div>

      {showAdvanced && (
        <div className="space-y-3 rounded-lg border bg-elevated p-3">
          <div className="space-y-2">
            <Label htmlFor={templateId}>Custom prompt</Label>
            <Textarea
              id={templateId}
              value={value.template}
              className="min-h-32 font-mono text-xs"
              placeholder="Leave blank to use the built-in prompt. Variables: $k, $max_reason_len, $guidance, $tone, $username"
              onChange={(event) =>
                onChange({ ...value, template: event.target.value })
              }
            />
            <p className="text-sm text-muted-foreground">
              Replaces the built-in instructions. The safety rules (only suggest
              titles you own, short reasons) are always added back, so this can
              never break a run.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onChange({ ...value, template: "" })}
            >
              <RotateCcw aria-hidden="true" />
              Restore default
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={preview.isPending}
              onClick={() => preview.mutate()}
            >
              {!preview.isPending && <ScanEye aria-hidden="true" />}
              Preview prompt
            </Button>
          </div>
          {preview.isSuccess && (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md border bg-card p-3 text-xs text-muted-foreground">
              {preview.data.system}
            </pre>
          )}
          {preview.isError && (
            <p role="alert" className="text-sm text-destructive">
              Couldn&rsquo;t build the preview. Check the prompt and try again.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
