import { useMutation } from "@tanstack/react-query";
import { ExternalLink, PlugZap } from "lucide-react";
import { useId, useState } from "react";

import { TestResult } from "@/components/test-result";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  helpLabel = "Get a key",
  secret = true,
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
  /** Wording for that link. Override where there is no key to get — a self-hosted backend's link
   *  points at install docs, and calling that "Get a key" invents a step that doesn't exist. */
  helpLabel?: string;
  /**
   * False for a setting that is an ADDRESS rather than a credential (a SearXNG URL). Redacting one
   * would hide the very value the owner needs to read back to spot a typo, and the sentinel logic
   * that protects a key from being wiped has nothing to protect here.
   */
  secret?: boolean;
}) {
  const save = useSaveSettings();
  const test = useMutation({ mutationFn: () => api.testConnection(service) });
  const stored = settingString(settings, settingKey);
  const saved = stored !== "";
  const [value, setValue] = useState(secret ? (saved ? REDACTED : "") : stored);
  const id = useId();

  const untouched = secret ? isSecretUnchanged(value) : value.trim() === stored;
  const commit = () => {
    if (untouched) return; // nothing typed / still the placeholder → no change, never wipe the key
    const next = secret ? value : value.trim();
    save.mutate(
      { [settingKey]: next },
      { onSuccess: () => secret && setValue(REDACTED) },
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
            {helpLabel}
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>
        )}
      </div>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {secret ? (
          <SecretInput
            id={id}
            placeholder={placeholder}
            className="max-w-xs"
            value={value}
            saved={saved}
            onChange={setValue}
          />
        ) : (
          <Input
            id={id}
            placeholder={placeholder}
            className="max-w-xs"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        )}
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
        <p role="alert" className="text-sm text-destructive-text">
          Couldn’t save. Try again.
        </p>
      )}
      {test.isSuccess && <TestResult result={test.data} />}
      {test.isError && <TestResult error={test.error} />}
    </div>
  );
}
