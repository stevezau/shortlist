import { useMutation } from "@tanstack/react-query";
import { PlugZap } from "lucide-react";
import { type ReactNode, useId, useState } from "react";

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
import { api, apiErrorMessage } from "@/lib/api";
import { settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings, TestableService } from "@/lib/types";
import { cn } from "@/lib/utils";

const REDACTED = "•••••";

/** One editable field on a connection card. `showIf` hides it based on the other fields' values. */
export type ConnectionField =
  | {
      key: string;
      label: string;
      kind: "text" | "password";
      placeholder?: string;
      showIf?: (values: Record<string, string>) => boolean;
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

  // Status dot on the logo tile: green = last test passed, red = failed, amber = configured but
  // untested, grey = nothing set. A quick scan across the cards shows what's wired up.
  const dot = test.isSuccess
    ? test.data.ok
      ? "bg-success"
      : "bg-destructive"
    : test.isError
      ? "bg-destructive"
      : configured
        ? "bg-warning"
        : "bg-muted-foreground/40";

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
      if (field.kind === "password" && (value === REDACTED || value === "")) {
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
              return (
                <div key={field.key} className="space-y-1.5">
                  <Label htmlFor={id}>{field.label}</Label>
                  {field.kind === "select" ? (
                    <Segmented
                      ariaLabel={field.label}
                      value={values[field.key] ?? ""}
                      options={field.options}
                      onChange={(v) =>
                        setValues((prev) => ({ ...prev, [field.key]: v }))
                      }
                    />
                  ) : (
                    <Input
                      id={id}
                      type={field.kind === "password" ? "password" : "text"}
                      placeholder={field.placeholder}
                      value={values[field.key] ?? ""}
                      onFocus={(e) => {
                        // Clear the redacted placeholder on focus so the owner types a fresh secret.
                        if (
                          field.kind === "password" &&
                          e.target.value === REDACTED
                        ) {
                          setValues((prev) => ({ ...prev, [field.key]: "" }));
                        }
                      }}
                      onBlur={() => {
                        // Left blank without typing? Put the placeholder back so it's clear the saved
                        // secret is still there (and Save will leave it untouched).
                        if (
                          field.kind === "password" &&
                          (values[field.key] ?? "") === "" &&
                          settingString(settings, field.key) === REDACTED
                        ) {
                          setValues((prev) => ({
                            ...prev,
                            [field.key]: REDACTED,
                          }));
                        }
                      }}
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
