import {
  Compass,
  Film,
  Globe,
  Sparkles,
  TriangleAlert,
  Tv,
} from "lucide-react";

import {
  MdblistGlyph,
  PlexGlyph,
  ProviderGlyph,
  TautulliGlyph,
  TmdbGlyph,
} from "@/components/brand-glyphs";
import { ConnectionCard } from "@/components/connection-card";
import { settingString } from "@/lib/format";
import { CURATOR_PROVIDERS, findProvider } from "@/lib/providers";
import { useRuns } from "@/lib/queries";
import { hasExa, hasExternalSearch, hasSearxng } from "@/lib/sources";
import type { Settings, TestableService } from "@/lib/types";

const PROVIDER_OPTIONS = CURATOR_PROVIDERS.map((provider) => ({
  value: provider.id,
  label: provider.label,
}));

/** The one "Search with" choice, matching the picker on the AI web search card so the two can never
 *  disagree — they are the same setting, shown where each is useful. */
const SEARCH_BACKENDS = [
  { value: "native", label: "AI provider’s own" },
  { value: "exa", label: "Exa" },
  { value: "searxng", label: "SearXNG (self-hosted)" },
] as const;

/** Whether the card should show `backend`'s fields for the pending choice. Exactly one backend runs,
 *  so exactly one backend's fields are ever asked for. */
function offers(
  values: Record<string, string>,
  backend: "exa" | "searxng",
): boolean {
  return (values["llm_web.search_provider"] || "native") === backend;
}

/** Which backend the Test button probes — the one the owner chose. `native` has no external service
 *  of its own, so it probes the AI provider's web-search tool instead (see the `native_search` case
 *  in the settings API): inferring "Claude can search" from the provider name is not the same as
 *  that account actually being allowed to. */
function testableSearchService(settings: Settings): TestableService {
  const chosen = settingString(settings, "llm_web.search_provider") || "native";
  return chosen === "exa" || chosen === "searxng" ? chosen : "native_search";
}

/** The collapsed card's one-line state: which backend is in play and how it's configured. */
function searchSummary(settings: Settings): string {
  const chosen = settingString(settings, "llm_web.search_provider") || "native";
  if (chosen === "searxng")
    return hasSearxng(settings)
      ? `SearXNG · ${settingString(settings, "searxng.url")}`
      : "";
  if (chosen === "exa") return hasExa(settings) ? "Exa · API key saved" : "";
  return "Your AI provider’s own web search";
}

/** "Last run: 46 web searches" — a spend proxy, since neither backend exposes a live quota, so the
 *  most recent finished run's count is the closest thing to "usage today".
 *
 *  Deliberately does NOT say "billed": the counter records searches by WHICHEVER backend that run
 *  used, while the backend shown here is whatever is configured NOW. Someone who has just switched
 *  SearXNG → Exa would otherwise see a bill claimed for searches that were free. */
function searchFootnote(
  settings: Settings,
  lastSearches: number | undefined,
): string | undefined {
  // Only where an external backend is actually set up. `run_persistence` always writes
  // `exa_searches`, so without this a native-only (Claude) server reads "Last run: 0 web searches"
  // while its provider searched all night, and a server that removed its backend keeps showing the
  // count from before.
  if (lastSearches == null || !hasExternalSearch(settings)) return undefined;
  return `Last run: ${lastSearches.toLocaleString()} web search${lastSearches === 1 ? "" : "es"}`;
}

/** Connections: Plex, Tautulli, TMDB, and the AI provider — each editable and testable in place. */
export function ConnectionsSection({ settings }: { settings: Settings }) {
  const runs = useRuns();
  const lastFinishedRun = runs.data?.find((r) => r.finished_at);
  // Warn when Ollama/compatible is selected but NEITHER external backend is configured — those
  // providers have no native web search, so llm_web (the proven-valuable feature) can't run without
  // one. Either backend clears it: a self-hoster satisfying this with SearXNG must not still be
  // nagged about Exa, which is the whole point of #78.
  const curatorProvider = settingString(settings, "curator.provider");
  const needsExaWarning =
    ["ollama", "openai_compatible"].includes(curatorProvider ?? "") &&
    !hasExternalSearch(settings);
  return (
    <section
      id="connections"
      aria-labelledby="connections-heading"
      className="scroll-mt-6 space-y-3"
    >
      <h2 id="connections-heading" className="text-lg font-semibold">
        Connections
      </h2>
      <div className="grid gap-4 lg:grid-cols-2">
        <ConnectionCard
          service="plex"
          title="Plex"
          need="required"
          purpose="Your media server. Shortlist reads each person’s watch history from it, and builds their row back into it."
          settings={settings}
          summary={settingString(settings, "plex.url")}
          glyph={<PlexGlyph />}
          fields={[
            {
              key: "plex.url",
              label: "Server address",
              kind: "text",
              placeholder: "http://your-host:32400",
            },
            { key: "plex.token", label: "Plex token", kind: "password" },
          ]}
        />
        <ConnectionCard
          service="tautulli"
          title="Tautulli"
          purpose="Tautulli is the monitoring dashboard many people run alongside Plex."
          next="Shortlist reads nothing but the friendlier names it knows your users by, so rows say “Sarah” rather than an email address."
          settings={settings}
          summary={settingString(settings, "tautulli.url")}
          glyph={<TautulliGlyph />}
          fields={[
            {
              key: "tautulli.url",
              label: "Address",
              kind: "text",
              placeholder: "http://your-host:8181",
            },
            { key: "tautulli.apikey", label: "API key", kind: "password" },
          ]}
        />
        <ConnectionCard
          service="tmdb"
          title="TMDB"
          need="required"
          purpose="TMDB (The Movie Database) is the free catalogue Shortlist looks titles up in. It is where “people who watched this also watched…” comes from."
          next="A key is free — sign up, then paste it here."
          settings={settings}
          summary={
            settingString(settings, "tmdb.apikey") ? "API key saved" : ""
          }
          glyph={<TmdbGlyph />}
          fields={[
            {
              key: "tmdb.apikey",
              label: "API key",
              kind: "password",
              helpUrl: "https://www.themoviedb.org/settings/api",
            },
          ]}
        />
        <ConnectionCard
          service="llm"
          title="AI provider"
          purpose="An AI provider lets Shortlist search the web for titles that match someone’s taste, and explain why it picked each one."
          next="Shortlist works fully without it. This only adds the web-search source."
          settings={settings}
          summary={
            // Show the provider's friendly label ("Claude", "None"), never the raw id or a
            // machine-id-looking string. "None" (heuristic mode) is a real, testable choice, so it
            // stays a configured state — its Test button must keep working, not vanish.
            findProvider(settingString(settings, "curator.provider"))?.label ??
            settingString(settings, "curator.provider")
          }
          glyph={
            <ProviderGlyph
              provider={settingString(settings, "curator.provider")}
              fallback={<Sparkles aria-hidden className="text-primary" />}
            />
          }
          footnote={
            needsExaWarning && (
              <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-sm">
                <TriangleAlert
                  className="mt-0.5 h-4 w-4 shrink-0"
                  aria-hidden="true"
                />
                <span>
                  This provider has no web search of its own, so it can&rsquo;t
                  find new titles on its own. Add an Exa key or a SearXNG
                  address below, or switch to Anthropic, OpenAI, or Google to
                  search the web directly.
                </span>
              </div>
            )
          }
          fields={[
            {
              key: "curator.provider",
              label: "Provider",
              kind: "select",
              options: PROVIDER_OPTIONS,
              // Switching provider invalidates the old provider's model + key — clear both so the new
              // provider's models load fresh once its key is entered.
              resets: ["curator.model", "curator.api_key"],
            },
            {
              key: "curator.model",
              label: "Model (blank = a sensible default)",
              kind: "model",
              placeholder: "e.g. claude-haiku-4-5",
              showIf: (v) => v["curator.provider"] !== "none",
            },
            {
              key: "curator.api_key",
              label: "API key",
              kind: "password",
              showIf: (v) =>
                // A local server needs no key, but a hosted gateway (OpenRouter) does — so the
                // field stays and the backend substitutes a placeholder when it's left blank.
                !["none", "ollama"].includes(v["curator.provider"] ?? ""),
              // Link straight to the selected provider's key page (Anthropic/OpenAI/Google console).
              helpUrl: (v) => findProvider(v["curator.provider"] ?? "")?.keyUrl,
            },
            {
              // One field for every self-hosted runtime. `/v1` is appended server-side when the URL
              // is a bare host, so the address people know their server by just works.
              key: "curator.openai_base_url",
              label: "Server URL",
              kind: "text",
              placeholder: "http://localhost:11434",
              showIf: (v) =>
                ["openai_compatible", "ollama"].includes(
                  v["curator.provider"] ?? "",
                ),
            },
          ]}
        />
        <ConnectionCard
          service="radarr"
          title="Radarr"
          purpose="Radarr is the app that fetches films for your library."
          next="Connect it and Shortlist can ask for a film it wanted to recommend but couldn’t find on your server."
          settings={settings}
          summary={settingString(settings, "requests.radarr.url")}
          glyph={<Film aria-hidden className="text-primary" />}
          fields={[
            {
              key: "requests.radarr.url",
              label: "Address",
              kind: "text",
              placeholder: "http://your-host:7878",
            },
            {
              key: "requests.radarr.apikey",
              label: "API key",
              kind: "password",
            },
          ]}
        />
        <ConnectionCard
          service="sonarr"
          title="Sonarr"
          purpose="Sonarr is Radarr’s equivalent for TV."
          next="Connect it and Shortlist can ask for a show it wanted to recommend but couldn’t find on your server."
          settings={settings}
          summary={settingString(settings, "requests.sonarr.url")}
          glyph={<Tv aria-hidden className="text-primary" />}
          fields={[
            {
              key: "requests.sonarr.url",
              label: "Address",
              kind: "text",
              placeholder: "http://your-host:8989",
            },
            {
              key: "requests.sonarr.apikey",
              label: "API key",
              kind: "password",
            },
          ]}
        />
        <ConnectionCard
          service="trakt"
          title="Trakt"
          // Trakt made API keys VIP-only, so people followed our instructions, found no way to
          // create a key, and reported it as a Shortlist bug (issue #73). The badge says it before
          // they go looking, rather than burying it mid-paragraph where it was skimmed past.
          requires="Needs paid Trakt VIP"
          purpose="Trakt is a site where people log what they watch. Its “related titles” often catch suggestions TMDB misses."
          next="Creating an API key needs a paid Trakt VIP subscription. Once the key is saved, switch the Trakt source on under Finding titles."
          settings={settings}
          summary={
            settingString(settings, "trakt.client_id") ? "API key saved" : ""
          }
          glyph={<Compass aria-hidden className="text-primary" />}
          fields={[
            {
              key: "trakt.client_id",
              label: "API key (Trakt app client ID)",
              kind: "password",
            },
          ]}
        />
        <ConnectionCard
          service="mdblist"
          title="MDBList"
          // Two consumers, not one: `requests.rating_source` gates what gets requested, and
          // `recommendations.rating_source` orders any row set to "Highest rated". Naming only
          // Requests left the row-ordering setting looking like it needed nothing.
          purpose="MDBList fetches a title’s IMDb, Rotten Tomatoes, Metacritic and Trakt scores in one lookup. The key is free."
          next="Needed only if you sort a row by “Highest rated” on anything but TMDB, or judge requests by those scores."
          settings={settings}
          summary={
            settingString(settings, "requests.mdblist.apikey")
              ? "API key saved"
              : ""
          }
          glyph={<MdblistGlyph />}
          fields={[
            {
              key: "requests.mdblist.apikey",
              label: "API key",
              kind: "password",
              placeholder: "Free key from mdblist.com",
              helpUrl: "https://mdblist.com/preferences/",
            },
          ]}
        />
        <ConnectionCard
          service={testableSearchService(settings)}
          testId="connection-websearch"
          // The native probe runs a real web search, so it is never fired unasked.
          autoTest={testableSearchService(settings) !== "native_search"}
          title="Web search"
          purpose="The optional web-search source looks up what to watch next, then keeps only titles already in your library. Claude, GPT and Gemini can search on their own; any other provider — including a local Ollama — searches through Exa or your own SearXNG."
          next="You only need ONE of them. Exa is hosted and takes a free-tier key; SearXNG runs on your own hardware and costs nothing."
          settings={settings}
          summary={searchSummary(settings)}
          glyph={<Globe aria-hidden className="text-primary" />}
          fields={[
            {
              key: "llm_web.search_provider",
              label: "Search backend",
              kind: "select",
              options: [...SEARCH_BACKENDS],
            },
            {
              key: "exa.apikey",
              label: "Exa API key",
              kind: "password",
              helpUrl: "https://dashboard.exa.ai/api-keys",
              showIf: (v) => offers(v, "exa"),
            },
            {
              key: "searxng.url",
              label: "SearXNG address",
              kind: "text",
              placeholder: "http://your-host:8080",
              showIf: (v) => offers(v, "searxng"),
            },
            {
              key: "searxng.username",
              label: "SearXNG username (only if behind a login)",
              kind: "text",
              showIf: (v) => offers(v, "searxng"),
            },
            {
              key: "searxng.password",
              label: "SearXNG password",
              kind: "password",
              showIf: (v) => offers(v, "searxng"),
            },
          ]}
          footnote={searchFootnote(settings, lastFinishedRun?.stats?.exa_searches)}
        />
      </div>
      {/* Required by the TMDB API terms of use whenever their data is displayed. */}
      <p className="text-xs text-muted-foreground">
        This product uses the TMDB API but is not endorsed or certified by TMDB.
      </p>
    </section>
  );
}
