import { useMutation } from "@tanstack/react-query";
import { ExternalLink, PlugZap, Trash2, TriangleAlert } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { TestResult } from "@/components/test-result";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { ModelField } from "@/components/model-field";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  isSecretUnchanged,
  REDACTED,
  SecretInput,
} from "@/components/ui/secret-input";
import { api, apiErrorMessage } from "@/lib/api";
import { settingString } from "@/lib/format";
import { useCuratorModels, useSaveSettings } from "@/lib/queries";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import type { Settings, TestableService } from "@/lib/types";
import { cn } from "@/lib/utils";

type PillTone = "ok" | "bad" | "warn" | "neutral";

const PILL_TONES: Record<PillTone, string> = {
  ok: "border-success/40 bg-success/10 text-success",
  bad: "border-destructive/40 bg-destructive/10 text-destructive-text",
  warn: "border-warning/40 bg-warning/10 text-warning",
  neutral: "border-border-strong bg-elevated text-muted-foreground",
};

function StatusPill({ tone, children }: { tone: PillTone; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-px text-[11px] font-medium leading-4",
        PILL_TONES[tone],
      )}
    >
      {tone === "ok" && <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-success" />}
      {children}
    </span>
  );
}

/** One editable field on a connection card. `showIf` hides it based on the other fields' values. */
export type ConnectionField =
  | {
      key: string;
      label: string;
      // "model" renders a native <select> dropdown of the AI provider's available models plus a
      // "Custom…" option that reveals a free-text override. Needs a sibling `curator.provider` field
      // on the same card so it knows whose models to list.
      kind: "text" | "password" | "model";
      placeholder?: string;
      /** One line under the input, for what the field is for or how to undo it. */
      hint?: string;
      showIf?: (values: Record<string, string>) => boolean;
      /** Optional "Get a key ↗" link shown by the field. A function receives the current field
          values so a provider-specific URL can be chosen (e.g. the AI curator's key link). */
      helpUrl?:
        string | ((values: Record<string, string>) => string | undefined);
    }
  | {
      key: string;
      label: string;
      kind: "select";
      /** Static list, or a function of the current values when one choice narrows another — the
          providers offered for "my AI's own search" are not the ones offered for SearXNG, because
          local models have no search tool. A disabled option stays VISIBLE with its reason: hiding
          it makes people hunt for a control that was never there. */
      options:
        | { value: string; label: string }[]
        | ((values: Record<string, string>) => {
            value: string;
            label: string;
            disabled?: boolean;
            reason?: string;
          }[]);
      showIf?: (values: Record<string, string>) => boolean;
      /** One line under the buttons explaining the current choice. The buttons sit in a row, so a
          label long enough to carry the trade-off makes the row unreadable — this is where that
          detail goes instead. */
      hint?: (values: Record<string, string>) => string | undefined;
      /** Other field keys to clear when this one changes — e.g. switching AI provider clears the
          now-wrong saved model and key so the new provider's are entered fresh. */
      resets?: string[];
    };

/**
 * A connection to an external service: shows its status at a glance, tests it in place, and — the
 * part the wizard used to own exclusively — lets the owner edit, add, or clear it right here.
 */
export function ConnectionCard({
  service,
  testId,
  autoTest = true,
  title,
  need = "optional",
  requires,
  purpose,
  next,
  glyph,
  settings,
  fields,
  summary,
  footnote,
  testLabel = "Test",
  unsetLabel,
}: {
  service: TestableService;
  /** False for a service whose probe is too expensive to run unasked. The dot then stays amber
   *  (configured, untested) until Test is pressed. The provider's own web search is the case: its
   *  probe performs a REAL search, so auto-running it would bill a page load. */
  autoTest?: boolean;
  /** Stable identity for the card. Defaults to the service, which is right for every card whose
   *  service is fixed; the Web search card's service follows the chosen backend, so it passes its
   *  own — it is one card either way, and tests should not have to know which backend is selected. */
  testId?: string;
  title: string;
  /** Whether Shortlist works without this. Shown as a badge, so it is answerable at a glance
      instead of being the first clause of a paragraph on every card. */
  need?: "required" | "optional";
  /** A cost or precondition worth seeing BEFORE going to get a key — "Needs paid Trakt VIP". Given
      its own warning-toned line, because buried mid-sentence is exactly how someone ends up hunting
      for a key they cannot create (issue #73). */
  requires?: string;
  /** What the service is and what Shortlist does with it. One or two sentences. */
  purpose: string;
  /** What to do once it is connected, or when it matters — kept out of `purpose` so the card reads
      as "what is this" then "what do I do", rather than one undifferentiated block. */
  next?: string;
  /** The service's brand mark, shown in the logo tile. */
  glyph: ReactNode;
  settings: Settings;
  fields: ConnectionField[];
  /** One-line description of the saved config when idle (e.g. the URL, or "API key saved"). */
  summary: string;
  /** Optional extra line shown under the card when idle — e.g. recent usage the owner should see. */
  footnote?: ReactNode;
  /** Wording for the test button, where the test is not a silent ping: the webhook's posts a message. */
  testLabel?: string;
  /** What the pill says while an optional service is not set up. "Optional" by default; a service
   *  that another feature waits on (a request app, the webhook) says "Not set up" instead. */
  unsetLabel?: "Optional" | "Not set up";
}) {
  const test = useMutation({ mutationFn: () => api.testConnection(service) });
  const save = useSaveSettings();
  const [editing, setEditing] = useState(false);
  const [testRequested, setTestRequested] = useState(false);
  // Idle-card "Remove" is a two-tap confirm: removing a connection is destructive (it wipes the
  // saved URL/key), so the first tap asks and the second commits — no accidental one-click wipe.
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [values, setValues] = useState<Record<string, string>>(() =>
    initialValues(settings, fields),
  );
  const fieldId = useId();
  const configured = Boolean(summary);

  // A "model" field shows the AI provider's available models in a real dropdown (plus a "Custom…"
  // escape hatch). The list is fetched for the provider + key CURRENTLY in the form (a redacted key
  // means "use the saved one"), so switching provider or typing a new key re-queries the right
  // models. The whole (provider, key, url) generation is debounced TOGETHER: the fetch fires only
  // once the form settles (the debounced snapshot matches what's on screen), so a provider switch can
  // never pair the old key with the new provider, and typing a key never refetches per keystroke.
  const provider = values["curator.provider"] ?? "";
  const enteredKey = values["curator.api_key"] ?? "";
  // A local/self-hosted server's "credential" is its URL, not a key. `curator.ollama_url` is the
  // pre-merge key, read as a fallback so an instance configured before the merge still lists models.
  const enteredUrl =
    values["curator.openai_base_url"] ?? values["curator.ollama_url"] ?? "";
  const hasModelField = fields.some((f) => f.kind === "model");
  const formGeneration = `${provider} ${enteredKey} ${enteredUrl}`;
  const settled = useDebouncedValue(formGeneration, 500) === formGeneration;
  const credential = ["openai_compatible", "ollama"].includes(provider)
    ? enteredUrl
    : enteredKey;
  const models = useCuratorModels(
    { provider, apiKey: enteredKey, ollamaUrl: enteredUrl },
    editing &&
      hasModelField &&
      Boolean(provider) &&
      provider !== "none" &&
      Boolean(credential) &&
      settled,
  );
  const modelOptions = models.data?.models ?? [];

  // Auto-test a configured connection once when the page opens, so the dot shows real green/red
  // without the owner clicking Test on every card. Only configured services probe (nothing to test
  // otherwise); it fires a single time per mount (or the first time setup completes).
  //
  // Tracks WHICH service it tested, not merely that it did. The Web search card's `service` follows
  // the chosen backend, and a save fires the probe before the settings query has re-rendered the
  // card — so a one-shot boolean left the result of testing the PREVIOUS backend on screen, showing
  // a green "Connection OK" for an instance that was never contacted.
  const autoTested = useRef<string | null>(null);
  useEffect(() => {
    if (autoTest && configured && autoTested.current !== service && !editing) {
      autoTested.current = service;
      test.mutate();
    }
  }, [autoTest, configured, editing, service, test]);

  // One pill says the state in words: whether it works when it is set up, and whether Shortlist
  // needs it when it isn't. "Connected" only ever comes from a test that passed.
  const pill: { label: string; tone: PillTone } =
    test.isSuccess && test.data.ok
      ? { label: "Connected", tone: "ok" }
      : test.isSuccess || test.isError
        ? { label: "Connection failed", tone: "bad" }
        : configured
          ? { label: test.isPending ? "Checking…" : "Not tested yet", tone: "neutral" }
          : need === "required"
            ? { label: "Not set up", tone: "warn" }
            : { label: unsetLabel ?? "Optional", tone: "neutral" };

  const openEditor = () => {
    setValues(initialValues(settings, fields));
    setEditing(true);
    save.reset();
  };

  const commit = () => {
    const payload: Settings = {};
    for (const field of fields) {
      const value = values[field.key] ?? "";
      // A password left as the redacted placeholder OR left blank means "no change" — never wipe a
      // saved secret on Save. (Focusing a field clears its dots; saving without retyping must be a
      // no-op, not a delete.) Clearing a secret is done deliberately via the Clear button.
      if (field.kind === "password" && isSecretUnchanged(value)) {
        continue;
      }
      payload[field.key] = value;
    }
    save.mutate(payload, {
      onSuccess: () => {
        setEditing(false);
        // Test what was just saved. The auto-test above fires once per mount, so it covers a
        // FIRST-time setup but not a REPLACED key: the card was already configured when the page
        // opened, so the dot kept showing the old key's green while the new one went untried. That
        // is the case that matters most — someone re-entering a key precisely because the old one
        // stopped working (a lapsed Trakt VIP, a rotated token) got no signal that it still doesn't.
        //
        // Clearing the marker rather than probing HERE, because a save can change which service this
        // card even talks to (the Web search card's backend). Calling `test.mutate()` inline would
        // use the `service` from this render — the one being replaced — and leave its verdict on
        // screen for a backend that was never contacted. The effect re-runs once the settings query
        // has refreshed and probes whatever is actually configured now.
        autoTested.current = null;
      },
    });
  };

  const clear = () => {
    // Blank every field, save, and close — the server stores empty, which clears a secret too.
    const payload: Settings = {};
    for (const field of fields) payload[field.key] = "";
    save.mutate(payload, {
      onSuccess: () => {
        setEditing(false);
        setConfirmRemove(false);
        test.reset();
      },
    });
  };

  return (
    <Card
      data-testid={testId ?? `connection-${service}`}
      id={testId ?? `connection-${service}`}
      className="scroll-mt-32 rounded-none border-0 bg-transparent shadow-none md:scroll-mt-8"
    >
      <CardHeader className="px-4 py-4 sm:px-5">
        {/* Wraps, and the name side may shrink: the glyph, the service name and the buttons together
            held the row open past a 320px screen. */}
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <div className="flex min-w-0 flex-1 basis-60 items-start gap-3">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border bg-elevated [&>svg]:h-4 [&>svg]:w-4">
              {glyph}
            </span>
            <div className="min-w-0 space-y-1">
              <h3 className="flex flex-wrap items-center gap-2 text-sm font-semibold leading-6">
                {title}
                <StatusPill tone={pill.tone}>{pill.label}</StatusPill>
                {/* "Optional" says whether it is needed, not whether it is set up — say that too. */}
                {!configured && need !== "required" && <span className="sr-only">Not set up</span>}
              </h3>
              <p className="max-w-prose text-sm text-muted-foreground">{purpose}</p>
              {next && <p className="max-w-prose text-sm text-muted-foreground">{next}</p>}
              {configured && !editing && <p className="break-words text-sm text-foreground/80">{summary}</p>}
              {test.isSuccess && test.data.ok && !testRequested && !editing && (
                <TestResult result={test.data} className="text-sm [&>svg]:h-3.5 [&>svg]:w-3.5" />
              )}
            </div>
          </div>
          {!editing &&
            (confirmRemove ? (
              // Inline confirm on the idle row — the destructive tap and its "keep it" escape sit
              // right where Remove was, so it never wipes a connection on a single click.
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted-foreground">Remove?</span>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={clear}
                    loading={save.isPending}
                  >
                    Remove
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setConfirmRemove(false)}
                    disabled={save.isPending}
                  >
                    Keep
                  </Button>
                </div>
                {save.isError && (
                  <p className="text-sm text-destructive-text">
                    {apiErrorMessage(save.error, "Remove failed.")}
                  </p>
                )}
              </div>
            ) : configured ? (
              <div className="flex shrink-0 items-center gap-1.5">
                <Button variant="ghost" size="sm" onClick={openEditor}>
                  Edit
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => { setTestRequested(true); test.mutate(); }}
                  loading={test.isPending}
                >
                  {!test.isPending && <PlugZap aria-hidden="true" />}
                  {testLabel}
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove ${title} connection`}
                  className="text-muted-foreground hover:text-destructive-text"
                  onClick={() => setConfirmRemove(true)}
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </div>
            ) : (
              // Nothing to test until something is on file, so the only action is the one that
              // gets it there.
              <Button variant="outline" size="sm" className="shrink-0" onClick={openEditor}>
                Set up
              </Button>
            ))}
        </div>
      </CardHeader>
      {(editing || testRequested || test.isError || (test.isSuccess && !test.data.ok) || footnote) && <CardContent className="px-4 pb-4 pt-0 sm:pl-16 sm:pr-5">
        {editing ? (
          <div className="space-y-3">
            {/* Seen when going to get a key, which is when it matters (issue #73) — not on the idle
                row, where it read as a warning about something not even set up. */}
            {requires && (
              <Badge variant="warning">
                <TriangleAlert aria-hidden className="h-3 w-3" />
                {requires}
              </Badge>
            )}
            {fields.map((field, i) => {
              if (field.showIf && !field.showIf(values)) return null;
              const id = `${fieldId}-${i}`;
              const helpUrl =
                field.kind === "select"
                  ? undefined
                  : typeof field.helpUrl === "function"
                    ? field.helpUrl(values)
                    : field.helpUrl;
              return (
                <div key={field.key} className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <Label htmlFor={id}>{field.label}</Label>
                    {helpUrl && (
                      <a
                        href={helpUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-0.5 text-xs font-medium text-primary underline-offset-2 hover:underline"
                      >
                        Get a key
                        <ExternalLink className="h-3 w-3" aria-hidden="true" />
                      </a>
                    )}
                  </div>
                  {field.kind === "select" ? (
                    <>
                      <Segmented
                        ariaLabel={field.label}
                        value={values[field.key] ?? ""}
                        options={
                          typeof field.options === "function"
                            ? field.options(values)
                            : field.options
                        }
                        onChange={(v) =>
                          setValues((prev) => {
                            const next = { ...prev, [field.key]: v };
                            // Clear now-stale sibling fields (e.g. the previous provider's model + key).
                            for (const k of field.resets ?? []) next[k] = "";
                            return next;
                          })
                        }
                      />
                      {field.hint?.(values) && (
                        <p className="text-sm text-muted-foreground">
                          {field.hint(values)}
                        </p>
                      )}
                    </>
                  ) : field.kind === "password" ? (
                    <SecretInput
                      id={id}
                      placeholder={field.placeholder}
                      value={values[field.key] ?? ""}
                      saved={settingString(settings, field.key) === REDACTED}
                      onChange={(v) =>
                        setValues((prev) => ({ ...prev, [field.key]: v }))
                      }
                    />
                  ) : field.kind === "model" ? (
                    <ModelField
                      // Remount on provider switch so a left-open "Custom…" box resets to the dropdown.
                      key={provider}
                      id={id}
                      value={values[field.key] ?? ""}
                      placeholder={field.placeholder}
                      models={modelOptions}
                      loading={models.isLoading}
                      error={models.isError}
                      onChange={(v) =>
                        setValues((prev) => ({ ...prev, [field.key]: v }))
                      }
                    />
                  ) : (
                    <Input
                      id={id}
                      type="text"
                      placeholder={field.placeholder}
                      value={values[field.key] ?? ""}
                      onChange={(e) =>
                        setValues((prev) => ({
                          ...prev,
                          [field.key]: e.target.value,
                        }))
                      }
                    />
                  )}
                  {field.kind !== "select" && field.hint && (
                    <p className="text-sm text-muted-foreground">{field.hint}</p>
                  )}
                </div>
              );
            })}
            {save.isError && (
              <p role="alert" className="text-sm text-destructive-text">
                {apiErrorMessage(
                  save.error,
                  "Saving failed. Check the server log and try again.",
                )}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <Button size="sm" onClick={commit} loading={save.isPending}>
                Save
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setEditing(false)}
              >
                Cancel
              </Button>
              {configured && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto text-destructive-text hover:text-destructive-text"
                  onClick={clear}
                  disabled={save.isPending}
                >
                  Clear
                </Button>
              )}
            </div>
          </div>
        ) : test.isSuccess ? (
          // A passing background check is already shown, small, under the summary above; only the
          // footnote opened this section, so don't say it twice.
          test.data.ok && !testRequested ? null : <TestResult result={test.data} />
        ) : test.isError ? (
          <TestResult error={test.error} />
        ) : null}
        {footnote && !editing && (
          <p className="mt-2 text-xs text-muted-foreground">{footnote}</p>
        )}
      </CardContent>}
    </Card>
  );
}

function initialValues(
  settings: Settings,
  fields: ConnectionField[],
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of fields)
    out[field.key] = settingString(settings, field.key);
  return out;
}
