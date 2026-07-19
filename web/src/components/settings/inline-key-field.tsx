import { useMutation } from "@tanstack/react-query";
import { ExternalLink, PlugZap } from "lucide-react";
import { useId, useState } from "react";

import { TestResult } from "@/components/test-result";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  isSecretUnchanged,
  REDACTED,
  SecretInput,
} from "@/components/ui/secret-input";
import { api } from "@/lib/api";
import { settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings, TestableService } from "@/lib/types";

/**
 * Enter (and test) an API key RIGHT WHERE a feature needs it — so turning something on never dead-ends
 * at "…add it in Connections first". A compact single-secret field: password input + Save + Test, with
 * the same redacted-sentinel handling as the Connections cards (a saved key shows as dots; saving
 * without retyping is a no-op, never a wipe). Connections stays the central list; this is the shortcut.
 */
export function InlineKeyField({
  settingKey,
  label,
  service,
  settings,
  placeholder,
  hint,
  helpUrl,
}: {
  settingKey: string;
  label: string;
  /** Which test-connection probe to run; also the key's home service. */
  service: TestableService;
  settings: Settings;
  placeholder?: string;
  hint?: string;
  /** Optional "Get a key ↗" link to the provider's key page. */
  helpUrl?: string;
}) {
  const save = useSaveSettings();
  const test = useMutation({ mutationFn: () => api.testConnection(service) });
  const saved = settingString(settings, settingKey) !== "";
  const [value, setValue] = useState(saved ? REDACTED : "");
  const id = useId();

  const untouched = isSecretUnchanged(value);
  const commit = () => {
    if (untouched) return; // nothing typed / still the placeholder → no change, never wipe the key
    save.mutate(
      { [settingKey]: value },
      { onSuccess: () => setValue(REDACTED) },
    );
  };

  return (
    <div className="space-y-2 rounded-lg border border-dashed border-primary/40 bg-primary/5 p-3">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id}>{label}</Label>
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
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <SecretInput
          id={id}
          placeholder={placeholder}
          className="max-w-xs"
          value={value}
          saved={saved}
          onChange={setValue}
        />
        <Button
          size="sm"
          onClick={commit}
          loading={save.isPending}
          disabled={untouched}
        >
          Save
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => test.mutate()}
          loading={test.isPending}
          disabled={!saved && !save.isSuccess}
        >
          {!test.isPending && <PlugZap aria-hidden="true" />}
          Test
        </Button>
      </div>
      {save.isError && (
        <p role="alert" className="text-sm text-destructive">
          Couldn’t save. Try again.
        </p>
      )}
      {test.isSuccess && <TestResult result={test.data} />}
      {test.isError && <TestResult error={test.error} />}
    </div>
  );
}
