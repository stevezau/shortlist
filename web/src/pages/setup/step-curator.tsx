import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

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
import { api } from "@/lib/api";
import { settingString } from "@/lib/format";
import { CURATOR_PROVIDERS, type CuratorProviderInfo } from "@/lib/providers";
import { queryKeys, useCuratorModels, useSettings } from "@/lib/queries";
import { cn } from "@/lib/utils";

import type { StepProps } from "./step-props";

/**
 * Step 3 — five provider cards (design doc §3 step 3). Keys are BYO, stored
 * encrypted at rest server-side, and redacted in the UI after save. "None"
 * is a proudly first-class choice, not a degraded mode.
 */
export function StepCurator({ data, update }: StepProps) {
  const selected = CURATOR_PROVIDERS.find(
    (p) => p.id === data.curator_provider,
  );
  const [apiKey, setApiKey] = useState("");
  const [ollamaUrl, setOllamaUrl] = useState("http://localhost:11434");
  const [model, setModel] = useState(selected?.defaultModel ?? "");
  const keyId = useId();
  const modelId = useId();
  const modelListId = useId();
  const ollamaId = useId();
  const queryClient = useQueryClient();

  // Coming back to this step (Back/Next) remounts it — seed the fields from what's already saved so
  // the key (redacted "•••••"), model, and Ollama URL survive the round trip. Only overrides the
  // defaults above when a value is actually on file; a re-sent "•••••" is a no-op on the backend.
  const settings = useSettings();
  const seeded = useRef(false);
  useEffect(() => {
    const saved = settings.data;
    if (seeded.current || !saved) return;
    seeded.current = true;
    const savedKey = settingString(saved, "curator.api_key");
    if (savedKey) setApiKey(savedKey);
    const savedUrl = settingString(saved, "curator.ollama_url");
    if (savedUrl) setOllamaUrl(savedUrl);
    const savedModel = settingString(saved, "curator.model");
    if (savedModel) setModel(savedModel);
  }, [settings.data]);

  // Fetch the provider's models once a key is on file (Ollama lists from its URL, no key). The backend
  // reads the SAVED key server-side, so the list reflects the last Save & test; an empty list (no key
  // yet, or a provider that can't list) just leaves the free-text field.
  const hasSavedKey = !!(
    settings.data && settingString(settings.data, "curator.api_key")
  );
  const models = useCuratorModels(
    selected?.id ?? "none",
    Boolean(
      selected && selected.id !== "none" && (selected.needsUrl || hasSavedKey),
    ),
  );
  const modelOptions = models.data?.models ?? [];

  const saveAndTest = useMutation({
    mutationFn: async (provider: CuratorProviderInfo) => {
      await api.putSettings({
        "curator.provider": provider.id,
        ...(provider.needsKey ? { "curator.api_key": apiKey } : {}),
        // The backend keeps a dedicated key for the Ollama endpoint URL.
        ...(provider.needsUrl ? { "curator.ollama_url": ollamaUrl } : {}),
        ...(provider.defaultModel ? { "curator.model": model } : {}),
      });
      if (provider.id === "none")
        return {
          ok: true,
          message: "Built-in picker ready — no AI, no keys, no cloud.",
        };
      return api.testConnection("llm");
    },
    onSuccess: (result, provider) => {
      // Only a passing test opens the Next gate. testConnection resolves even when the key is
      // wrong (ok: false) — so gate on result.ok, not merely on the call succeeding.
      if (provider.id !== "none") update({ curator_ready: result.ok === true });
      // The key is now on file, so refetch this provider's model list to populate the picker.
      queryClient.invalidateQueries({
        queryKey: queryKeys.curatorModels(provider.id),
      });
    },
  });

  const choose = (provider: CuratorProviderInfo) => {
    // Switching to a different provider must drop any seeded "•••••": that mask belongs to the
    // previously-saved provider's key, and leaving it would imply this provider already has one
    // (and re-save it against the wrong key).
    if (provider.id !== data.curator_provider) setApiKey("");
    // "none" is ready immediately; a key/URL provider isn't ready until Save & test passes, so the
    // gate closes on selection and only reopens on a successful test.
    update({
      curator_provider: provider.id,
      curator_ready: provider.id === "none",
    });
    setModel(provider.defaultModel);
    saveAndTest.reset();
    if (provider.id === "none") saveAndTest.mutate(provider);
  };

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-2">
        {CURATOR_PROVIDERS.map((provider) => (
          <button
            key={provider.id}
            type="button"
            onClick={() => choose(provider)}
            aria-pressed={data.curator_provider === provider.id}
            className={cn(
              "rounded-lg text-left",
              data.curator_provider === provider.id && "ring-2 ring-primary",
            )}
          >
            <Card className="h-full">
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center justify-between text-base">
                  {provider.label}
                  {data.curator_provider === provider.id && (
                    <Check
                      className="h-4 w-4 text-primary"
                      aria-hidden="true"
                    />
                  )}
                </CardTitle>
                <CardDescription>{provider.cost}</CardDescription>
              </CardHeader>
            </Card>
          </button>
        ))}
      </div>

      {selected && selected.id !== "none" && (
        <Card>
          <CardContent className="space-y-4 pt-6">
            {selected.needsKey && (
              <div className="space-y-2">
                <Label htmlFor={keyId}>{selected.label} API key</Label>
                <Input
                  id={keyId}
                  type="password"
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  autoComplete="off"
                />
                {selected.keyUrl && (
                  <p className="text-sm text-muted-foreground">
                    Get a key at{" "}
                    <a
                      href={selected.keyUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="text-primary underline-offset-4 hover:underline"
                    >
                      {new URL(selected.keyUrl).host}
                    </a>
                    . Stored encrypted, never logged, redacted after save.
                  </p>
                )}
              </div>
            )}
            {selected.needsUrl && (
              <div className="space-y-2">
                <Label htmlFor={ollamaId}>Ollama URL</Label>
                <Input
                  id={ollamaId}
                  value={ollamaUrl}
                  onChange={(event) => setOllamaUrl(event.target.value)}
                  autoComplete="off"
                />
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor={modelId}>Model</Label>
              <Input
                id={modelId}
                value={model}
                onChange={(event) => setModel(event.target.value)}
                autoComplete="off"
                list={modelOptions.length > 0 ? modelListId : undefined}
                placeholder={selected.defaultModel}
              />
              {modelOptions.length > 0 && (
                <datalist id={modelListId}>
                  {modelOptions.map((id) => (
                    <option key={id} value={id} />
                  ))}
                </datalist>
              )}
              <p className="text-sm text-muted-foreground">
                {models.isFetching
                  ? "Loading available models…"
                  : modelOptions.length > 0
                    ? `${modelOptions.length} models available — start typing to choose, or keep the recommended ${selected.defaultModel}.`
                    : "The cheap tier is plenty — the curator only re-ranks ~40 titles you already own."}
              </p>
            </div>
            <Button
              onClick={() => saveAndTest.mutate(selected)}
              disabled={
                saveAndTest.isPending ||
                (selected.needsKey && apiKey.trim().length === 0)
              }
            >
              {saveAndTest.isPending && (
                <Loader2 className="animate-spin" aria-hidden="true" />
              )}
              Save & test
            </Button>
          </CardContent>
        </Card>
      )}

      {saveAndTest.isSuccess && <TestResult result={saveAndTest.data} />}
      {saveAndTest.isError && (
        <TestResult
          error={saveAndTest.error}
          errorFallback="The test call failed. Check the key and try again."
        />
      )}
    </div>
  );
}
