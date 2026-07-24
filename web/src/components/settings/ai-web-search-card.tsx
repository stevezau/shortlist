import { Segmented } from "@/components/segmented";
import { InlineKeyField } from "@/components/settings/inline-key-field";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { hasCurator, hasExa, hasNativeWebSearch } from "@/lib/sources";
import type { Settings } from "@/lib/types";

const BACKENDS = [
  { value: "auto", label: "Auto" },
  { value: "native", label: "AI provider’s own" },
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
    return "Uses your AI provider’s own web search (Claude, GPT, or Gemini). A local Ollama model can’t — pick Exa for that.";
  if (backend === "exa")
    return "Uses the Exa search API — works for every provider, and the only option for a local Ollama model.";
  if (hasNativeWebSearch(settings) && hasExa(settings))
    return "Uses your AI provider’s own search and Exa together. They find mostly different titles, so you get the widest pool — at the cost of two searches per run.";
  if (hasNativeWebSearch(settings))
    return "Uses your AI provider’s own web search. Add an Exa key below to search with both (they find different titles).";
  if (hasExa(settings))
    return "Uses your Exa key. A Claude, GPT, or Gemini provider would add its own web search alongside it.";
  return "Set up a search backend below — an Exa key, or a Claude/GPT/Gemini provider that can search on its own.";
}

/**
 * "AI — web search for what to watch next" as its own card: enable, choose the search backend, and —
 * the point — enter whatever that backend needs RIGHT HERE. No dead-end "add it in Connections". The
 * toggle reflects intent and is never disabled; if a dependency is missing, the card shows exactly how
 * to satisfy it (an inline Exa key, or an AI-provider prompt), so it can never read "on" while unexplained.
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
  // Whether this backend actually hits Exa (so we only surface Exa's usage/limits when relevant):
  // the "exa" backend always, and "auto" when the curator can't self-search.
  const usesExa =
    backend === "exa" || (backend === "auto" && !hasNativeWebSearch(settings));

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
                Also needs an AI provider to choose titles from the results —{" "}
                <a href="#connections" className="font-medium underline">
                  set one up in Connections
                </a>
                .
              </p>
            )}

            {nativeUnusable && (
              <p className="text-sm text-warning">
                Your AI provider can’t search the web on its own. To use this
                source, either switch the backend to <strong>Auto</strong> or{" "}
                <strong>Exa</strong>, or pick a Claude, GPT, or Gemini provider.
              </p>
            )}

            {exaMissing && (
              <InlineKeyField
                settingKey="exa.apikey"
                service="exa"
                label="Exa API key"
                placeholder="exa-…"
                hint="This backend searches via Exa. Paste your key from exa.ai to switch it on — no trip to Connections."
                helpUrl="https://dashboard.exa.ai/api-keys"
                settings={settings}
              />
            )}

            {/* Usage & limits, so the cost of turning this on is never a surprise (the MDBList card
                does the same for its lookups). Only the Exa free-tier line is Exa-specific. */}
            <div className="space-y-1.5 rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">
                How much it searches
              </p>
              <p>
                On a row&rsquo;s refresh night it runs one search per recent
                watch, up to the{" "}
                <a href="#recent-count" className="font-medium underline">
                  Recent watches to search
                </a>{" "}
                count (default 10). Results are cached for two weeks and shared
                across everyone, so a popular title is searched once for the
                whole server — not once per person.
              </p>
              {usesExa && (
                <p>
                  Exa&rsquo;s free tier covers roughly 1,000 searches a month —
                  plenty for a small server. A large server, or a high recent
                  count, may need a paid Exa plan.
                </p>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
