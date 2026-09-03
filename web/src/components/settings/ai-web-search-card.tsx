import { RECENT_COUNT_LABEL } from "@/components/recent-count-field";
import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import {
  hasCurator,
  hasExa,
  hasNativeWebSearch,
  hasSearxng,
  webSearchProvider,
} from "@/lib/sources";
import type { Settings } from "@/lib/types";

/** How the chosen backend reads on screen. */
const BACKEND_LABELS: Record<string, string> = {
  native: "your AI provider’s own search",
  exa: "Exa",
  searxng: "your SearXNG instance",
};

/** Whether the chosen backend is actually set up, mirroring `make_search_client` on the server. */
function backendReady(backend: string, settings: Settings): boolean {
  if (backend === "exa") return hasExa(settings);
  if (backend === "searxng") return hasSearxng(settings);
  return hasNativeWebSearch(settings);
}

/** What's missing, phrased as the thing to go and do. Null when the source can actually run.
 *
 *  Whether an AI provider is needed depends on the backend, and used to be asked as one blanket
 *  question. Exa extracts the titles itself, so it runs with no AI at all; SearXNG returns raw
 *  snippets that only a model can read; native search IS the model. Asking blankly told Exa owners
 *  to go and buy a key they did not need, and — worse — read as the reason the source was idle when
 *  it was actually running and being billed for. */
function missing(backend: string, settings: Settings): string | null {
  if (!hasCurator(settings) && backend !== "exa")
    return backend === "searxng"
      ? "This also needs an AI provider: SearXNG returns raw web snippets, and something has to read them to find the titles."
      : "This also needs an AI provider — the search runs inside it.";
  if (backendReady(backend, settings)) return null;
  if (backend === "exa") return "No Exa API key is saved yet.";
  if (backend === "searxng") return "No SearXNG address is saved yet.";
  return "Your AI provider can’t search the web on its own — only Claude, GPT and Gemini can. Switch the search backend to Exa or SearXNG, or change provider.";
}

/**
 * "Web search for what to watch next": the on/off switch for the source, and what turning it on
 * will cost.
 *
 * WHICH backend it searches with, and that backend's credentials, deliberately live on the
 * Connections → "AI & Web search" card and nowhere else. They were briefly in both places, which meant the
 * same control rendered twice with no way to tell which one was authoritative. Connections owns the
 * services Shortlist talks to (the AI provider works the same way); this card owns whether the
 * source runs and how hard. It still NAMES the backend and says plainly when one isn't usable, so
 * "on" can never read as working when it isn't — it just links out rather than duplicating the form.
 */
export function AiWebSearchCard({
  settings,
  enabled,
  onToggle,
}: {
  settings: Settings;
  enabled: boolean;
  onToggle: () => void;
}) {
  const backend = webSearchProvider(settings);
  const problem = missing(backend, settings);

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-0.5">
            {/* Not "AI — web search": the AI is optional here now that Exa extracts titles itself,
                and a title claiming otherwise is what sent Exa owners looking for a key. */}
            <p className="text-sm font-medium">
              Web search for what to watch next
            </p>
            <p className="text-sm text-muted-foreground">
              Searches the live web for current, well-reviewed titles to watch
              next, then keeps only what’s in your library.
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
            <p className="text-sm text-muted-foreground">
              Searching with{" "}
              <strong className="text-foreground">
                {BACKEND_LABELS[backend] ?? backend}
              </strong>{" "}
              —{" "}
              <a href="#connections" className="font-medium underline">
                change it in Connections
              </a>
              .
            </p>

            {problem && (
              <p className="text-sm text-warning">
                {problem}{" "}
                <a href="#connections" className="font-medium underline">
                  Set it up in Connections
                </a>
                .
              </p>
            )}

            {backend === "searxng" && (
              <p className="text-xs text-muted-foreground">
                SearXNG must be serving JSON: add{" "}
                <code className="font-mono">json</code> to{" "}
                <code className="font-mono">search.formats</code> in its{" "}
                <code className="font-mono">settings.yml</code> and restart, or
                it will refuse Shortlist with a 403.
              </p>
            )}

            {/* Usage & limits, so the cost of turning this on is never a surprise (the MDBList card
                does the same for its lookups). The last line is backend-specific. */}
            <div className="space-y-1.5 rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">
                How much it searches
              </p>
              {backend === "native" ? (
                <p>
                  Your AI provider searches as part of one call per person, so
                  there is no separate search to count, cap or cache — the cost
                  shows up as AI tokens rather than as searches.
                </p>
              ) : (
                <p>
                  On a row&rsquo;s refresh night it runs one search per recent
                  watch, as many as{" "}
                  <a href="#recent-count" className="font-medium underline">
                    {RECENT_COUNT_LABEL}
                  </a>{" "}
                  allows (default 10). Results are cached for two weeks and
                  shared across everyone, so a popular title is searched once
                  for the whole server — not once per person.
                </p>
              )}
              {backend === "exa" && (
                <p>
                  Exa&rsquo;s free tier covers roughly 1,000 searches a month —
                  plenty for a small server. A large server, or a high recent
                  count, may need a paid Exa plan.
                </p>
              )}
              {backend === "searxng" && (
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
