import { useId, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { AiInstructions } from "@/lib/types";

/** The API's limit on a row's AI text. */
const MAX_CHARS = 2000;

/** The "Add to the default" and "Write your own" text boxes, shared by the two AI editors. */
export function AiTextFields({
  value,
  onChange,
  blankText,
  addNote,
  ownLabel,
  hint,
  afterOwn,
}: {
  value: AiInstructions;
  onChange: (next: AiInstructions) => void;
  /** Shown while Add or Write your own has nothing written; the API refuses it. */
  blankText: string;
  addNote: string;
  ownLabel: string;
  hint?: string;
  /** Extra content under the Write your own controls. */
  afterOwn?: ReactNode;
}) {
  const textId = useId();
  const blankId = useId();
  if (value.mode === "default") return null;

  const blank = !value.text.trim();
  const own = value.mode === "own";
  return (
    <div className="space-y-2">
      <Label htmlFor={textId}>{own ? ownLabel : "Also tell the AI"}</Label>
      <Textarea
        id={textId}
        rows={own ? 6 : undefined}
        value={value.text}
        maxLength={MAX_CHARS}
        aria-invalid={blank || undefined}
        aria-describedby={blank ? blankId : undefined}
        onChange={(event) => onChange({ mode: value.mode, text: event.target.value })}
      />
      {blank && (
        <p id={blankId} className="text-sm text-destructive-text">
          {blankText}
        </p>
      )}
      {!own && <p className="text-sm text-muted-foreground">{addNote}</p>}
      {hint && <p className="text-sm text-muted-foreground">{hint}</p>}
      {own && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onChange({ mode: "default", text: value.text })}
        >
          Reset to the default
        </Button>
      )}
      {own && afterOwn}
    </div>
  );
}
