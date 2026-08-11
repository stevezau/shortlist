import { RECENT_COUNT_LABEL } from "@/components/recent-count-field";
import { Segmented } from "@/components/segmented";
import { InlineKeyField } from "@/components/settings/inline-key-field";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  hasCurator,
  hasExa,
  hasNativeWebSearch,
  hasSearxng,
} from "@/lib/sources";
import type { Settings } from "@/lib/types";

const BACKENDS = [
  { value: "auto", label: "Auto" },
  { value: "native", label: "AI provider’s own" },
  { value: "exa", label: "Exa" },
  { value: "searxng", label: "SearXNG" },
] as const;

/**
 * Which external backend this configuration actually searches through, or null for none.
 *
 * Mirrors `make_search_client` on the server, including its tie-break: under Auto with both set up,
 * SearXNG wins because it is free and local — choosing "Auto" must never be the reason a bill
 * appears. Naming a backend explicitly never falls back to the other one.
 */
function resolvedExternal(
  backend: string,
  settings: Settings,
): "exa" | "searxng" | null {
  if (backend === "native") return null;
  if (backend === "exa") return hasExa(settings) ? "exa" : null;
  if (backend === "searxng") return hasSearxng(settings) ? "searxng" : null;
  if (hasSearxng(settings)) return "searxng";
  return hasExa(settings) ? "exa" : null;
}

function backendNote(backend: string, settings: Settings): string {
  if (backend === "native")
    return "Uses your AI provider’s own web search (Claude, GPT, or Gemini). A local Ollama model can’t — pick Exa or SearXNG for that.";
  if (backend === "exa")
    return "Uses the Exa search API — works for every provider, including a local Ollama model.";
  if (backend === "searxng")
    return "Uses your own SearXNG instance — no account, no key, no per-search bill, and it works with any AI provider. SearXNG is a proxy rather than its own index: it forwards each search to real engines (Google, Brave, DuckDuckGo) and merges what they send back.";
  const native = hasNativeWebSearch(settings);
  const external = resolvedExternal(backend, settings);
  if (native && external === "searxng")
    return "Uses your AI provider’s own search and your SearXNG instance together. They find mostly different titles, so you get the widest pool.";
  if (native && external === "exa")
    return "Uses your AI provider’s own search and Exa together. They find mostly different titles, so you get the widest pool — at the cost of two searches per run.";
  if (native)
    return "Uses your AI provider’s own web search. Add Exa or a SearXNG address below to search with both (they find different titles).";
  if (external === "searxng")
    return "Uses your SearXNG instance. A Claude, GPT, or Gemini provider would add its own web search alongside it.";
  if (external === "exa")
    return "Uses your Exa key. A Claude, GPT, or Gemini provider would add its own web search alongside it.";
  return "Set up a search backend below — an Exa key or your own SearXNG — or pick a Claude/GPT/Gemini provider that can search on its own.";
}

/**
 * "AI — web search for what to watch next" as its own card: enable, choose the search backend, and —
 * the point — enter whatever that backend needs RIGHT HERE. No dead-end "add it in Connections". The
 * toggle reflects intent and is never disabled; if a dependency is missing, the card shows exactly how
 * to satisfy it, so it can never read "on" while unexplained.
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
  // Prompts, prioritised so the card is loud in EVERY state where the source would produce nothing:
  // no curator at all → set one up (nothing else can help); else "native" on a curator that can't
  // self-search (Ollama) → tell them to switch; else an external backend with nothing configured →
  // enter it inline. This mirrors the engine's own capability gate.
  const curatorMissing = !hasCurator(settings);
  const nativeUnusable =
    !curatorMissing && backend === "native" && !hasNativeWebSearch(settings);
  const external = resolvedExternal(backend, settings);
  // Which inline field(s) to offer. Naming a backend asks for exactly that one; Auto with nothing
  // configured offers both, because either would switch the source on and neither is the "right"
  // answer for every owner. Under Auto a native-capable provider needs NOTHING — it already searches
  // on its own, so asking would be a prompt for a dependency that isn't one.
  const wantsExternal =
    !curatorMissing &&
    backend !== "native" &&
    !external &&
    !(backend === "auto" && hasNativeWebSearch(settings));
  const askExa = wantsExternal && backend !== "searxng";
  const askSearxng = wantsExternal && backend !== "exa";

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
              {/* SearXNG's one prerequisite, stated wherever SearXNG is in play and kept visible
                  even after the address is saved. A stock instance serves HTML only and refuses us
                  with a 403, and the source then finds nothing with no other clue as to why. */}
              {(backend === "searxng" || external === "searxng") && (
                <p className="text-xs text-muted-foreground">
                  SearXNG must be serving JSON: add{" "}
                  <code className="font-mono">json</code> to{" "}
                  <code className="font-mono">search.formats</code> in its{" "}
                  <code className="font-mono">settings.yml</code> and restart,
                  or it will refuse Shortlist with a 403.
                </p>
              )}
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
                source, either switch the backend to <strong>Auto</strong>,{" "}
                <strong>Exa</strong> or <strong>SearXNG</strong>, or pick a
                Claude, GPT, or Gemini provider.
              </p>
            )}

            {askExa && (
              <InlineKeyField
                settingKey="exa.apikey"
                service="exa"
                label="Exa API key"
                placeholder="exa-…"
                hint="Hosted search, no setup beyond the key. Paste it from exa.ai to switch this on."
                helpUrl="https://dashboard.exa.ai/api-keys"
                settings={settings}
              />
            )}

            {askSearxng && (
              <InlineKeyField
                settingKey="searxng.url"
                service="searxng"
                label="SearXNG address"
                placeholder="http://your-host:8080"
                hint="Your own SearXNG instance, and it works with any AI provider. SearXNG forwards each search on to real engines (Google, Brave, DuckDuckGo) — what stays yours is that there is no account, key or bill."
                helpUrl="https://docs.searxng.org/admin/installation-docker.html"
                helpLabel="Setup guide"
                secret={false}
                settings={settings}
              />
            )}

            {/* Usage & limits, so the cost of turning this on is never a surprise (the MDBList card
                does the same for its lookups). The last two lines are backend-specific. */}
            <div className="space-y-1.5 rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">
                How much it searches
              </p>
              <p>
                On a row&rsquo;s refresh night it runs one search per recent
                watch, as many as{" "}
                <a href="#recent-count" className="font-medium underline">
                  {RECENT_COUNT_LABEL}
                </a>{" "}
                allows (default 10). Results are cached for two weeks and shared
                across everyone, so a popular title is searched once for the
                whole server — not once per person.
              </p>
              {external === "exa" && (
                <p>
                  Exa&rsquo;s free tier covers roughly 1,000 searches a month —
                  plenty for a small server. A large server, or a high recent
                  count, may need a paid Exa plan.
                </p>
              )}
              {external === "searxng" && (
                <p>
                  SearXNG searches cost nothing, so the only limit is whatever
                  your own instance imposes.
                </p>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
