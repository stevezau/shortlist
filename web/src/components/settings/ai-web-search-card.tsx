import { Segmented } from "@/components/segmented";
import { InlineKeyField } from "@/components/settings/inline-key-field";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { hasCurator, hasExa, hasNativeWebSearch } from "@/lib/sources";
import type { Settings } from "@/lib/types";

const BACKENDS = [
  { value: "auto", label: "Auto" },
  { value: "native", label: "Curator’s own" },
  { value: "exa", label: "Exa" },
] as const;

/** Whether the chosen backend needs an Exa key (exa, or auto with no native-capable curator). */
function needsExa(backend: string, settings: Settings): boolean {
  const usesExa =
    backend === "exa" || (backend === "auto" && !hasNativeWebSearch(settings));
  return usesExa && !hasExa(settings);
}

function backendNote(backend: string, settings: Settings): string {
  if (backend === "native")
    return "Uses your AI curator’s own web search (Claude, GPT, or Gemini). A local Ollama model can’t — pick Exa for that.";
  if (backend === "exa")
    return "Uses the Exa search API — works for every provider, and the only option for a local Ollama curator.";
  const via = hasNativeWebSearch(settings)
    ? "your curator’s own web search"
    : hasExa(settings)
      ? "your Exa key"
      : "—";
  return `Uses the curator’s own tool where it has one (Claude/GPT/Gemini), otherwise Exa. Right now: ${via}.`;
}

/**
 * "AI — web search for what to watch next" as its own card: enable, choose the search backend, and —
 * the point — enter whatever that backend needs RIGHT HERE. No dead-end "add it in Connections". The
 * toggle reflects intent and is never disabled; if a dependency is missing, the card shows exactly how
 * to satisfy it (an inline Exa key, or a curator prompt), so it can never read "on" while unexplained.
 */
export function AiWebSearchCard({
  settings,
  enabled,
  onToggle,
  backend,
  onBackendChange,
}: {
  settings: Settings;
  enabled: boolean;
  onToggle: () => void;
  backend: string;
  onBackendChange: (v: string) => void;
}) {
  // Prompts, prioritised so exactly one shows: no curator at all → set one up (nothing else can help);
  // else the "native" backend on a curator that can't self-search (Ollama) → tell them to switch; else
  // an Exa-using backend with no key → enter it inline. This mirrors the engine's own capability gate,
  // so the card is loud in EVERY state where the source would produce nothing.
  const curatorMissing = !hasCurator(settings);
  const nativeUnusable =
    !curatorMissing && backend === "native" && !hasNativeWebSearch(settings);
  const exaMissing = !curatorMissing && needsExa(backend, settings);

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-0.5">
            <p className="text-sm font-medium">
              AI — web search for what to watch next
            </p>
            <p className="text-sm text-muted-foreground">
              Searches the live web for current, well-reviewed titles to watch
              next, then keeps only what’s in your library. Choose how it
              searches below.
            </p>
          </div>
          <Switch
            checked={enabled}
            onCheckedChange={onToggle}
            aria-label="Enable AI web search"
          />
        </div>

        {enabled && (
          <div className="space-y-3 border-t pt-4">
            <div className="space-y-2">
              <Label>Search backend</Label>
              <Segmented<string>
                value={backend}
                ariaLabel="Web search backend"
                options={BACKENDS.map((b) => ({
                  value: b.value,
                  label: b.label,
                }))}
                onChange={onBackendChange}
              />
              <p className="text-xs text-muted-foreground">
                {backendNote(backend, settings)}
              </p>
            </div>

            {curatorMissing && (
              <p className="text-sm text-warning">
                Also needs an AI curator to choose titles from the results —{" "}
                <a href="#connections" className="font-medium underline">
                  set one up in Connections
                </a>
                .
              </p>
            )}

            {nativeUnusable && (
              <p className="text-sm text-warning">
                Your AI curator can’t search the web on its own — switch the
                backend to <strong>Auto</strong> or <strong>Exa</strong>, or use
                a Claude, GPT, or Gemini curator.
              </p>
            )}

            {exaMissing && (
              <InlineKeyField
                settingKey="exa.apikey"
                service="exa"
                label="Exa API key"
                placeholder="exa-…"
                hint="This backend searches via Exa. Paste your key from exa.ai to switch it on — no trip to Connections."
                settings={settings}
              />
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
