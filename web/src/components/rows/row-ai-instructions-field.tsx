import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Link } from "react-router";

import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { settingString } from "@/lib/format";
import { useSettings } from "@/lib/queries";
import type { AiInstructionsInert } from "@/lib/sources";
import type { AiInstructions } from "@/lib/types";
import { useDebouncedValue } from "@/lib/use-debounced-value";

type Mode = AiInstructions["mode"];

/** The API's `maxLength` on the row's text and on the server-wide default. */
const MAX_CHARS = 2000;
const PREVIEW_DEBOUNCE_MS = 400;
const DEFAULTS_HREF = "/settings/defaults#sources";
const LINK_CLASS = "underline underline-offset-2 hover:text-foreground";
const PROMPT_CLASS =
  "max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 font-mono text-xs";

const PLACEHOLDERS_HINT = "You can use {count}, {year} and {last_year}.";

/** What the server adds after the system prompt, by search backend (`curator/base.py`'s user prompts). */
const PREVIEW_FOOTNOTES: Record<string, string> = {
  native: "Then the person's 20 most recent watches are added.",
  exa: "Then the person's 20 most recent watches and the titles Exa found are added.",
  searxng: "Then the person's 20 most recent watches and excerpts from the articles found are added.",
};

/** Native search also ends its user message by asking for recent releases, unless guidance replaces the default. */
const RECENCY_LINE = " It ends by asking for titles released in the last two years.";

const MODES: { value: Mode; label: string }[] = [
  { value: "default", label: "Use the default" },
  { value: "add", label: "Add to the default" },
  { value: "own", label: "Write your own" },
];

/** "A", "A and B", "A, B and C". */
function listOf(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** What the search backend lets the instructions steer; null when there is nothing to add (native). */
function backendNote(backend: string, inert: AiInstructionsInert): string | null {
  // With no AI provider nothing reads the instructions: Exa's titles are kept as found
  // (`candidates._titles_as_proposals`) and native and SearXNG search do not run at all.
  if (inert === "no_provider") {
    return backend === "exa"
      ? "With no AI provider, Exa's titles are used as found, so these instructions have no effect. Add an AI provider in Settings → Connections."
      : "AI web search needs an AI provider, so these instructions have no effect yet. Add one in Settings → Connections.";
  }
  if (inert === "no_native_search") {
    return "Your AI provider can't search the web itself, so these instructions have no effect. Choose Exa or SearXNG in Settings → Connections.";
  }
  if (backend === "exa") {
    return "You search with Exa. Exa's searches start from each person's recent watches and are shared between people, so these instructions decide which of Exa's titles the AI keeps, not what Exa looks for.";
  }
  if (backend === "searxng") {
    return "You search with SearXNG. Its results start from each person's recent watches; these instructions decide which titles the AI picks from them.";
  }
  return null;
}

/**
 * A row's instructions for AI web search (#138): use the server-wide default, add to it, or replace
 * it. Shown only while the row uses AI web search (`visibleSettings`), and saved with the row.
 */
export function RowAiInstructionsField({
  value,
  onChange,
  otherSources,
  backend,
  inert,
}: {
  value: AiInstructions;
  onChange: (next: AiInstructions) => void;
  /** Short names of the row's other sources, none of which read these instructions. */
  otherSources: string[];
  /** `llm_web.search_provider`: "native", "exa" or "searxng". */
  backend: string;
  /** Why nothing reads the instructions under the current settings, or null when the AI does. */
  inert: AiInstructionsInert;
}) {
  const textId = useId();
  const blankId = useId();
  const [previewOpen, setPreviewOpen] = useState(false);
  const note = backendNote(backend, inert);
  const setText = (text: string) => onChange({ mode: value.mode, text });
  // The API refuses Add or Write your own with nothing written; say so here, before Save does.
  const blank = value.mode !== "default" && !value.text.trim();
  const blankMessage = blank && (
    <p id={blankId} className="text-sm text-destructive-text">
      Write the instructions, or choose Use the default.
    </p>
  );

  return (
    <div className="space-y-3 border-t pt-4">
      <p className="text-sm font-medium">AI instructions</p>
      <p className="text-sm text-muted-foreground">
        What AI web search should look for in this row.
      </p>
      <Segmented
        value={value.mode}
        options={MODES}
        ariaLabel="AI instructions"
        onChange={(mode) => onChange({ mode, text: value.text })}
      />

      {value.mode === "default" && <DefaultInstructions backend={backend} />}

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
          <p className="text-sm text-muted-foreground">
            Added after the default instructions, for this row only.
          </p>
          <p className="text-sm text-muted-foreground">{PLACEHOLDERS_HINT}</p>
        </div>
      )}

      {value.mode === "own" && (
        <div className="space-y-2">
          <Label htmlFor={textId}>Your instructions</Label>
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
          <p className="text-sm text-muted-foreground">{PLACEHOLDERS_HINT}</p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onChange({ mode: "default", text: value.text })}
          >
            Reset to the default
          </Button>
          <div className="space-y-1 rounded-md border border-dashed p-3">
            <p className="text-sm font-medium">Shortlist always adds these</p>
            <p className="text-sm text-muted-foreground">
              Today&apos;s year, and that the AI must search rather than answer from memory. The exact
              title and release year for every pick, so it can be found in your library. Only titles
              already out. The reply format.
            </p>
          </div>
        </div>
      )}

      {otherSources.length > 0 && (
        <p className="text-sm text-warning">
          {listOf(otherSources)} {otherSources.length === 1 ? "doesn't" : "don't"} read these
          instructions, so this row will be a mix.
        </p>
      )}
      {note && <p className="text-sm text-muted-foreground">{note}</p>}
      {value.mode !== "default" && (
        <p className="text-sm text-muted-foreground">
          Rows with different instructions can&apos;t share one AI web search, so this row may make
          its own AI call per person each run.
        </p>
      )}

      <details onToggle={(event) => setPreviewOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer text-sm font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          Exactly what&apos;s sent
        </summary>
        {previewOpen && (
          <div className="mt-3">
            <PromptPreview value={value} backend={backend} />
          </div>
        )}
      </details>
    </div>
  );
}

/** The instructions a row on the default gets: the owner's server-wide text, else the built-in wording. */
function DefaultInstructions({ backend }: { backend: string }) {
  const settings = useSettings();
  const serverText = settings.data ? settingString(settings.data, "llm_web.instructions").trim() : "";
  // The server reads blank server-wide text as "use the built-in wording" (`resolve_guidance`).
  const needsBuiltin = settings.isSuccess && serverText === "";
  const builtin = useQuery({
    queryKey: ["web-prompt-preview", "builtin", backend],
    queryFn: () => api.previewWebPrompt({}),
    enabled: needsBuiltin,
  });

  let body;
  if (settings.isError || (needsBuiltin && builtin.isError)) {
    body = (
      <LoadError
        message="Couldn't load the default instructions."
        onRetry={() => void (settings.isError ? settings.refetch() : builtin.refetch())}
      />
    );
  } else if (settings.isPending || (needsBuiltin && builtin.isPending)) {
    body = <Skeleton className="h-4 w-full" />;
  } else {
    body = <pre className={PROMPT_CLASS}>{serverText || builtin.data?.builtin_guidance}</pre>;
  }

  return (
    <div className="space-y-2">
      {body}
      <p className="text-sm text-muted-foreground">
        The default every row starts with. Change it in{" "}
        <Link to={DEFAULTS_HREF} className={LINK_CLASS}>
          Settings → Defaults → Title sources
        </Link>
        .
      </p>
    </div>
  );
}

/** The system prompt this row's AI web search would send, as the server builds it. */
function PromptPreview({ value, backend }: { value: AiInstructions; backend: string }) {
  const settings = useSettings();
  const text = useDebouncedValue(value.text, PREVIEW_DEBOUNCE_MS);
  const preview = useQuery({
    queryKey: ["web-prompt-preview", backend, value.mode, text],
    queryFn: () => api.previewWebPrompt({ ai_instructions: { mode: value.mode, text } }),
    // Keeps the last prompt on screen while the next one loads, rather than a skeleton per pause in typing.
    placeholderData: keepPreviousData,
  });

  if (preview.isError) {
    return <LoadError message="Couldn't load the preview." onRetry={() => void preview.refetch()} />;
  }
  if (preview.isPending) return <Skeleton className="h-4 w-full" />;
  // The server drops that line when guidance replaces the default: a row's own text, or the server-wide text.
  const serverText = settings.data ? settingString(settings.data, "llm_web.instructions").trim() : "";
  const sendsRecency =
    preview.data.backend === "native" && settings.isSuccess && value.mode !== "own" && serverText === "";
  return (
    <div className="space-y-2">
      <pre className={PROMPT_CLASS}>{preview.data.system}</pre>
      <p className="text-sm text-muted-foreground">
        {PREVIEW_FOOTNOTES[preview.data.backend] ?? PREVIEW_FOOTNOTES.native}
        {sendsRecency && <span>{RECENCY_LINE}</span>}
      </p>
    </div>
  );
}

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3">
      <p className="text-sm text-destructive-text">{message}</p>
      <Button type="button" variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
