import { useMutation } from "@tanstack/react-query";
import { Check, Loader2 } from "lucide-react";
import { useId, useState } from "react";

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
import { CURATOR_PROVIDERS, type CuratorProviderInfo } from "@/lib/providers";
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
  const ollamaId = useId();

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
          message: "Heuristic mode is ready — no AI, no keys, no cloud.",
        };
      return api.testConnection("llm");
    },
  });

  const choose = (provider: CuratorProviderInfo) => {
    update({ curator_provider: provider.id });
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
              />
              <p className="text-sm text-muted-foreground">
                The cheap tier is plenty — the curator only re-ranks ~40 titles
                you already own.
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
