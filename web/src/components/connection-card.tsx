import { useMutation } from "@tanstack/react-query";
import { ExternalLink, PlugZap } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Segmented } from "@/components/segmented";
import { TestResult } from "@/components/test-result";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  isSecretUnchanged,
  REDACTED,
  SecretInput,
} from "@/components/ui/secret-input";
import { api, apiErrorMessage } from "@/lib/api";
import { settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings, TestableService } from "@/lib/types";
import { cn } from "@/lib/utils";

/** One editable field on a connection card. `showIf` hides it based on the other fields' values. */
export type ConnectionField =
  | {
      key: string;
      label: string;
      kind: "text" | "password";
      placeholder?: string;
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
      options: { value: string; label: string }[];
      showIf?: (values: Record<string, string>) => boolean;
    };

/**
 * A connection to an external service: shows its status at a glance, tests it in place, and — the
 * part the wizard used to own exclusively — lets the owner edit, add, or clear it right here.
 */
export function ConnectionCard({
  service,
  title,
  purpose,
  glyph,
  settings,
  fields,
  summary,
}: {
  service: TestableService;
  title: string;
  /** Plain-English, non-technical explanation of what this connection is for. */
  purpose: string;
  /** The service's brand mark, shown in the logo tile. */
  glyph: ReactNode;
  settings: Settings;
  fields: ConnectionField[];
  /** One-line description of the saved config when idle (e.g. the URL, or "API key saved"). */
  summary: string;
}) {
  const test = useMutation({ mutationFn: () => api.testConnection(service) });
  const save = useSaveSettings();
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<Record<string, string>>(() =>
    initialValues(settings, fields),
  );
  const fieldId = useId();
  const configured = Boolean(summary);

  // Auto-test a configured connection once when the page opens, so the dot shows real green/red
  // without the owner clicking Test on every card. Only configured services probe (nothing to test
  // otherwise); the ref fires it a single time per mount (or the first time setup completes).
  const autoTested = useRef(false);
  useEffect(() => {
    if (configured && !autoTested.current && !editing) {
      autoTested.current = true;
      test.mutate();
    }
  }, [configured, editing, test]);

  // Status dot on the logo tile: green = last test passed, red = failed, amber = configured but
  // untested, grey = nothing set. A quick scan across the cards shows what's wired up. The dot is
  // colour-only and aria-hidden, so the same state is spelled out for screen readers alongside it.
  const dot = test.isSuccess
    ? test.data.ok
      ? "bg-success"
      : "bg-destructive"
    : test.isError
      ? "bg-destructive"
      : configured
        ? "bg-warning"
        : "bg-muted-foreground/40";
  const status =
    test.isSuccess && test.data.ok
      ? "Connection OK"
      : test.isSuccess || test.isError
        ? "Connection failed"
        : configured
          ? "Configured, untested"
          : "Not set up";

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
    save.mutate(payload, { onSuccess: () => setEditing(false) });
  };

  const clear = () => {
    // Blank every field, save, and close — the server stores empty, which clears a secret too.
    const payload: Settings = {};
    for (const field of fields) payload[field.key] = "";
    save.mutate(payload, {
      onSuccess: () => {
        setEditing(false);
        test.reset();
      },
    });
  };

  return (
    <Card data-testid={`connection-${service}`}>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between">
          <span className="flex items-center gap-2.5">
            <span className="relative">
              <span className="grid h-9 w-9 place-items-center rounded-lg border bg-elevated [&>svg]:h-5 [&>svg]:w-5">
                {glyph}
              </span>
              <span
                aria-hidden="true"
                className={cn(
                  "absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full ring-2 ring-card",
                  dot,
                )}
              />
              <span className="sr-only">{status}</span>
            </span>
            {title}
          </span>
          {!editing && (
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" onClick={openEditor}>
                {configured ? "Edit" : "Set up"}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => test.mutate()}
                loading={test.isPending}
                disabled={!configured}
              >
                {!test.isPending && <PlugZap aria-hidden="true" />}
                Test
              </Button>
            </div>
          )}
        </CardTitle>
        <CardDescription>{purpose}</CardDescription>
      </CardHeader>
      <CardContent>
        {editing ? (
          <div className="space-y-3">
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
                    <Segmented
                      ariaLabel={field.label}
                      value={values[field.key] ?? ""}
                      options={field.options}
                      onChange={(v) =>
                        setValues((prev) => ({ ...prev, [field.key]: v }))
                      }
                    />
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
                </div>
              );
            })}
            {save.isError && (
              <p role="alert" className="text-sm text-destructive">
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
                  className="ml-auto text-destructive hover:text-destructive"
                  onClick={clear}
                  disabled={save.isPending}
                >
                  Clear
                </Button>
              )}
            </div>
          </div>
        ) : test.isSuccess ? (
          <TestResult result={test.data} />
        ) : test.isError ? (
          <TestResult error={test.error} />
        ) : (
          <p className="text-sm text-muted-foreground">
            {summary || "Not set up yet — choose Set up to connect."}
          </p>
        )}
      </CardContent>
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
