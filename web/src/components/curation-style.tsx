import { useQuery } from "@tanstack/react-query";
import { useId } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";

export interface CurationStyleValue {
  tone: string;
  guidance: string;
  template: string;
}

/** Where this editor sits in the override chain — drives the priority note it shows. */
export type CurationScope = "global" | "row" | "user";

const SCOPE_NOTE: Record<CurationScope, string> = {
  global:
    "This is the global default, used for every row and person unless overridden. Priority: global → row → person, and the most specific one wins.",
  row: "This row’s prompt overrides the global default. A person can still override it just for themselves. Priority: global → row → person.",
  user: "This overrides the row’s prompt and the global default — for this person only. Priority: global → row → person.",
};

/**
 * The curation prompt, edited directly. One box holds the whole prompt (stored as `template`), started
 * from the built-in default; `$k`/`$username`/`$max_reason_len` are filled in per run, and the safety
 * contract is appended by the engine so it can't be edited away. `tone`/`guidance` are legacy fields —
 * always sent as the neutral "balanced"/"" now, folded into the box once so nothing an owner set is lost.
 */
export function CurationStyleFields({
  value,
  onChange,
  allowInherit = false,
  shared = false,
  scope,
}: {
  value: CurationStyleValue;
  onChange: (next: CurationStyleValue) => void;
  /** Overrides (row/person): a blank box means "inherit the level above". */
  allowInherit?: boolean;
  /** Shared rows preview the group-row skeleton, personal rows the per-user one. */
  shared?: boolean;
  scope?: CurationScope;
}) {
  const fieldId = useId();
  const builtIn = useQuery({
    queryKey: ["prompt-default", shared],
    queryFn: () => api.getPromptDefault(shared),
    staleTime: Infinity,
  });
  const defaultTemplate = builtIn.data?.template ?? "";

  // An override starts blank (inherit); the global starts from the built-in. A legacy `guidance` note
  // is folded onto the base once so it survives the switch to editing the whole prompt.
  const base = allowInherit ? "" : defaultTemplate;
  const shown =
    value.template ||
    [base, value.guidance.trim()].filter(Boolean).join(" ").trim();
  const isCustomized = Boolean(
    value.template ||
    value.guidance.trim() ||
    (value.tone && value.tone !== "balanced"),
  );

  return (
    <div className="space-y-2">
      {scope && (
        <p className="text-sm text-muted-foreground">{SCOPE_NOTE[scope]}</p>
      )}
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={fieldId}>The prompt the AI receives</Label>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!isCustomized}
          onClick={() =>
            onChange({ tone: "balanced", guidance: "", template: "" })
          }
        >
          {allowInherit ? "Clear override" : "Reset to default"}
        </Button>
      </div>
      <Textarea
        id={fieldId}
        value={shown}
        placeholder={
          allowInherit
            ? `Blank = inherit. Start from:\n${defaultTemplate}`
            : defaultTemplate
        }
        onChange={(event) =>
          onChange({
            tone: "balanced",
            guidance: "",
            template: event.target.value,
          })
        }
        className="min-h-[14rem] font-mono text-xs leading-relaxed"
      />
      <p className="text-xs text-muted-foreground">
        Edit the prompt directly. Variables filled in per run:{" "}
        <span className="font-mono">$k</span> (row size),{" "}
        <span className="font-mono">$username</span> (the person),{" "}
        <span className="font-mono">$max_reason_len</span> (max reason length).{" "}
        {allowInherit ? "Leave blank to inherit the level above. " : ""}The
        safety rules — only suggest titles already in your library, never invent
        — are always added automatically.
      </p>
    </div>
  );
}
