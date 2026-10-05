import { useId } from "react";
import { Link } from "react-router";

import { QueryBoundary } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { CONNECTIONS_SETTINGS } from "@/lib/row-kinds";
import { buildPrompt, themeGuidance, useThemeCapabilities, useThemePrompts } from "@/lib/themes";
import type { AiInstructions, CollectionInput, ThemePrompts } from "@/lib/types";

type Mode = AiInstructions["mode"];

/** The API's limit on a row's guidance text. */
const MAX_CHARS = 2000;
const PROMPT_CLASS =
  "max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 font-mono text-xs";

const MODES: { value: Mode; label: string }[] = [
  { value: "default", label: "Use the default" },
  { value: "add", label: "Add to the default" },
  { value: "own", label: "Write your own" },
];

function LoadingPrompt() {
  return (
    <div role="status" aria-label="Loading the prompt" className="space-y-2">
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-4/5" />
      <Skeleton className="h-16 w-full" />
    </div>
  );
}

/**
 * What "Write the list" and "Adjust the list" tell the AI (#138): the guidance, which the owner can add to or
 * replace for this row, and the mechanics, which they can't. The mechanics are what make the answer
 * readable by code (the reply format, real titles with their years); a prompt without them returns prose
 * the parser can't use. The words are kept in the row's AI instructions, saved with Save changes.
 */
export function AiPromptsSection({
  input,
  set,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
}) {
  const capabilities = useThemeCapabilities();
  return (
    <QueryBoundary query={capabilities} skeleton={<LoadingPrompt />}>
      {({ ai }) =>
        ai ? (
          <PromptEditor input={input} set={set} />
        ) : (
          <p className="text-sm text-muted-foreground">
            The prompt is only used when an AI provider is set. Add one in{" "}
            <Link to={CONNECTIONS_SETTINGS} className="underline underline-offset-2 hover:text-foreground">
              Settings › Connections
            </Link>
            .
          </p>
        )
      }
    </QueryBoundary>
  );
}

function PromptEditor({
  input,
  set,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
}) {
  const prompts = useThemePrompts();
  return (
    <QueryBoundary query={prompts} skeleton={<LoadingPrompt />}>
      {(data) => <Prompt value={input.ai_instructions} onChange={(ai_instructions) => set({ ai_instructions })} prompts={data} />}
    </QueryBoundary>
  );
}

function Prompt({
  value,
  onChange,
  prompts,
}: {
  value: AiInstructions;
  onChange: (next: AiInstructions) => void;
  prompts: ThemePrompts;
}) {
  const textId = useId();
  const blankId = useId();
  const guidance = themeGuidance(value, prompts.guidance);
  // The API refuses Add or Write your own with nothing written; say so here, before Save does.
  const blank = value.mode !== "default" && !value.text.trim();
  const setText = (text: string) => onChange({ mode: value.mode, text });
  const blankMessage = blank && (
    <p id={blankId} className="text-sm text-destructive-text">
      Write your guidance, or choose Use the default.
    </p>
  );

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        This is what the AI is told when you press Write the list or Adjust the list. Changing it only affects lists
        written after you save.
      </p>
      <Segmented value={value.mode} options={MODES} ariaLabel="AI guidance" onChange={(mode) => onChange({ mode, text: value.text })} />

      {value.mode === "default" && (
        <div className="space-y-1">
          <p className="text-sm font-medium">Guidance</p>
          <pre className={PROMPT_CLASS}>{prompts.guidance}</pre>
        </div>
      )}
      {value.mode === "add" && (
        <div className="space-y-2">
          <Label htmlFor={textId}>Also tell the AI</Label>
          <Textarea
            id={textId}
            value={value.text}
            maxLength={MAX_CHARS}
            aria-invalid={blank || undefined}
            aria-describedby={blank ? blankId : undefined}
            onChange={(event) => setText(event.target.value)}
          />
          {blankMessage}
          <p className="text-sm text-muted-foreground">Added after the default guidance, for this row only.</p>
        </div>
      )}
      {value.mode === "own" && (
        <div className="space-y-2">
          <Label htmlFor={textId}>Your guidance</Label>
          <Textarea
            id={textId}
            rows={6}
            value={value.text}
            maxLength={MAX_CHARS}
            aria-invalid={blank || undefined}
            aria-describedby={blank ? blankId : undefined}
            onChange={(event) => setText(event.target.value)}
          />
          {blankMessage}
          <Button type="button" variant="outline" size="sm" onClick={() => onChange({ mode: "default", text: value.text })}>
            Reset to the default
          </Button>
        </div>
      )}

      <div className="space-y-1 rounded-md border border-dashed p-3">
        <p className="text-sm font-medium">Shortlist always adds this</p>
        <p className="text-sm text-muted-foreground">
          The reply format and the rules that keep titles real. You can read it but not change it: without it the
          answer can’t be read.
        </p>
        <pre className={PROMPT_CLASS}>{prompts.mechanics}</pre>
      </div>

      <details className="space-y-1">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          Exactly what’s sent
        </summary>
        <pre aria-label="Exactly what’s sent" className={PROMPT_CLASS}>
          {buildPrompt(guidance, prompts.guidance, prompts.mechanics)}
        </pre>
        <p className="text-sm text-muted-foreground">
          Then your description, and for an adjustment, the current list. Titles only, never names.
        </p>
      </details>
    </div>
  );
}
