import { useMutation } from "@tanstack/react-query";
import { Check, Loader2 } from "lucide-react";
import { useId, useState } from "react";

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
import { api, ApiError } from "@/lib/api";
import type { CuratorProvider } from "@/lib/wizard";
import { cn } from "@/lib/utils";

import type { StepProps } from "./step-props";

interface ProviderCard {
  id: CuratorProvider;
  name: string;
  cost: string;
  needsKey: boolean;
  needsUrl: boolean;
  defaultModel: string;
  keyUrl?: string;
}

const PROVIDERS: readonly ProviderCard[] = [
  {
    id: "anthropic",
    name: "Anthropic",
    cost: "Pennies per night on the cheap tier — bring your own API key.",
    needsKey: true,
    needsUrl: false,
    defaultModel: "claude-haiku-4-5-20251001",
    keyUrl: "https://console.anthropic.com/settings/keys",
  },
  {
    id: "openai",
    name: "OpenAI",
    cost: "Pennies per night on the mini tier — bring your own API key.",
    needsKey: true,
    needsUrl: false,
    defaultModel: "gpt-5-mini",
    keyUrl: "https://platform.openai.com/api-keys",
  },
  {
    id: "google",
    name: "Google",
    cost: "Pennies per night on the Flash tier — bring your own API key.",
    needsKey: true,
    needsUrl: false,
    defaultModel: "gemini-2.5-flash",
    keyUrl: "https://aistudio.google.com/apikey",
  },
  {
    id: "ollama",
    name: "Ollama",
    cost: "Free and fully local — no key, just a URL to your Ollama server.",
    needsKey: false,
    needsUrl: true,
    defaultModel: "llama3.3",
  },
  {
    id: "none",
    name: "None",
    cost: "Free. Heuristic mode: frequency × rating × recency, with template reasons. Fully functional.",
    needsKey: false,
    needsUrl: false,
    defaultModel: "",
  },
];

/**
 * Step 3 — five provider cards (design doc §3 step 3). Keys are BYO, stored
 * encrypted at rest server-side, and redacted in the UI after save. "None"
 * is a proudly first-class choice, not a degraded mode.
 */
export function StepCurator({ data, update }: StepProps) {
  const selected = PROVIDERS.find((p) => p.id === data.curator_provider);
  const [apiKey, setApiKey] = useState("");
  const [ollamaUrl, setOllamaUrl] = useState("http://localhost:11434");
  const [model, setModel] = useState(selected?.defaultModel ?? "");
  const keyId = useId();
  const modelId = useId();
  const ollamaId = useId();

  const saveAndTest = useMutation({
    mutationFn: async (provider: ProviderCard) => {
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

  const choose = (provider: ProviderCard) => {
    update({ curator_provider: provider.id });
    setModel(provider.defaultModel);
    saveAndTest.reset();
    if (provider.id === "none") saveAndTest.mutate(provider);
  };

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-2">
        {PROVIDERS.map((provider) => (
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
                  {provider.name}
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
                <Label htmlFor={keyId}>{selected.name} API key</Label>
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

      {saveAndTest.isSuccess &&
        (saveAndTest.data.ok ? (
          <p className="inline-flex items-center gap-2 text-sm text-success">
            <Check className="h-4 w-4" aria-hidden="true" />
            {saveAndTest.data.message}
          </p>
        ) : (
          <p role="alert" className="text-sm text-destructive">
            {saveAndTest.data.message}
          </p>
        ))}
      {saveAndTest.isError && (
        <p role="alert" className="text-sm text-destructive">
          {saveAndTest.error instanceof ApiError
            ? saveAndTest.error.message
            : "The test call failed. Check the key and try again."}
        </p>
      )}
    </div>
  );
}
