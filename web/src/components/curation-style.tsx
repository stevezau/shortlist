import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { TONE_LABELS, TONE_STARTERS } from "@/lib/constants";
import { PROMPT_TONES } from "@/lib/types";

export interface CurationStyleValue {
  tone: string;
  guidance: string;
  template: string;
}

/**
 * One box, not three. The old UI split style into a `tone` preset, a `guidance` note, and an
 * "advanced" full-prompt `template` — which read as duplicate boxes for the same thing. Now there's a
 * single "Instructions" box (stored as `guidance`, which is layered on top of the built-in prompt so
 * the safety rules always survive), and the tone buttons are quick-fills that drop editable starter
 * text into it. `tone`/`template` are no longer set from here (always sent empty).
 *
 * The exact prompt the AI receives is shown live below (the built-in default when nothing's set, so
 * you can always see what's in effect), with a one-click reset back to that default.
 */
export function CurationStyleFields({
  value,
  onChange,
  allowInherit = false,
}: {
  value: CurationStyleValue;
  onChange: (next: CurationStyleValue) => void;
  /** Per-person overrides: an empty box means "inherit the row/global style". Shown as a hint. */
  allowInherit?: boolean;
}) {
  const instructionsId = useId();

  // Everything the user types lives in `guidance`; tone/template stay empty so the one box is the
  // whole story (the built-in prompt + its safety rules are always applied underneath it).
  const setInstructions = (guidance: string) =>
    onChange({ tone: "", template: "", guidance });

  const isCustomized = Boolean(
    value.tone || value.guidance.trim() || value.template,
  );

  // Debounce the live preview so typing doesn't fire a request per keystroke. It always runs (even
  // with a blank recipe) so the built-in default prompt is visible, not hidden behind a button.
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), 400);
    return () => clearTimeout(id);
  }, [value.tone, value.guidance, value.template]);
  const preview = useQuery({
    queryKey: [
      "prompt-preview",
      debounced.tone,
      debounced.guidance,
      debounced.template,
    ],
    queryFn: () => api.previewPrompt(debounced),
    staleTime: 60_000,
  });

  return (
    <div className="space-y-4">
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Quick styles</legend>
        <p className="text-sm text-muted-foreground">
          Tap one to fill the box below with a starting point, then edit it
          however you like.
        </p>
        <div className="flex flex-wrap gap-2">
          {PROMPT_TONES.filter((tone) => tone !== "balanced").map((tone) => (
            <Button
              key={tone}
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setInstructions(TONE_STARTERS[tone] ?? "")}
            >
              {TONE_LABELS[tone] ?? tone}
            </Button>
          ))}
        </div>
      </fieldset>

      <div className="space-y-2">
        <Label htmlFor={instructionsId}>
          Instructions for the AI (optional)
        </Label>
        <Textarea
          id={instructionsId}
          value={value.guidance}
          placeholder="e.g. Prefer hidden gems over blockbusters. Keep the reasons family-friendly."
          onChange={(event) => setInstructions(event.target.value)}
        />
        <p className="text-sm text-muted-foreground">
          {allowInherit
            ? "Leave blank to use this row’s style. Anything here overrides it just for this person."
            : "Plain-English notes for the AI, added on top of the built-in prompt. It can only ever suggest titles already in your library."}
        </p>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <Label>The prompt the AI receives</Label>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={!isCustomized}
            onClick={() => onChange({ tone: "", guidance: "", template: "" })}
          >
            {allowInherit ? "Clear override" : "Reset to default"}
          </Button>
        </div>
        {preview.isSuccess ? (
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md border bg-card p-3 text-xs text-muted-foreground">
            {preview.data.system}
          </pre>
        ) : preview.isError ? (
          <p role="alert" className="text-sm text-destructive">
            Couldn’t build the preview. Check the instructions and try again.
          </p>
        ) : (
          <div className="h-24 animate-pulse rounded-md border bg-card" />
        )}
        <p className="text-xs text-muted-foreground">
          {isCustomized
            ? "This is exactly what the AI receives — your instructions are folded into the built-in prompt."
            : "The built-in default. Add instructions above to tailor it."}
        </p>
      </div>
    </div>
  );
}
