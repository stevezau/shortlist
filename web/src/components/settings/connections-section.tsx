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
import type { Settings } from "@/lib/types";

const PROVIDER_OPTIONS = CURATOR_PROVIDERS.map((provider) => ({
  value: provider.id,
  label: provider.label,
}));

/** "Last run: 46 Exa searches" — a spend proxy for the Exa card (Exa has no live-quota endpoint,
 * so the most recent finished run's search count is the closest thing to "usage today"). */
function exaUsageNote(lastExa: number | undefined): string | undefined {
  if (lastExa == null) return undefined;
  return `Last run: ${lastExa.toLocaleString()} Exa search${lastExa === 1 ? "" : "es"} · billed per search`;
}

/** Connections: Plex, Tautulli, TMDB, and the AI provider — each editable and testable in place. */
export function ConnectionsSection({ settings }: { settings: Settings }) {
  const runs = useRuns();
  const lastFinishedRun = runs.data?.find((r) => r.finished_at);
  const exaConfigured = Boolean(settingString(settings, "exa.apikey"));
  const exaFootnote = exaConfigured
    ? exaUsageNote(lastFinishedRun?.stats?.exa_searches)
    : undefined;

  // Warn when Ollama/compatible is selected but no Exa key is configured — those providers have no
  // native web search, so llm_web (the proven-valuable feature) won't work without Exa.
  const curatorProvider = settingString(settings, "curator.provider");
  const needsExaWarning =
    ["ollama", "openai_compatible"].includes(curatorProvider ?? "") &&
    !exaConfigured;
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
          purpose="Your media server — the source of each person's watch history, and where the personalized rows appear."
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
          purpose="Optional. Tautulli is the monitoring dashboard many people run alongside Plex. Shortlist only reads the friendlier names it knows your users by, so rows say “Sarah” rather than an email address."
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
          purpose="Required. TMDB (The Movie Database) is the free film and TV catalogue Shortlist looks titles up in — it is where “people who watched this also watched…” comes from. Signing up for a key is free."
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
          purpose="Optional. Finds new titles by searching the web for your tastes. Shortlist works fully with no AI at all — this only adds the web-search source."
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
                  This provider has no web search of its own, so it can't find
                  new titles without Exa. Add an Exa key below, or switch to
                  Anthropic, OpenAI, or Google to search the web directly.
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
          purpose="Optional. Radarr is the app that fetches films for your library. Connect it and Shortlist can ask it for a film it wanted to recommend but couldn’t find on your server."
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
          purpose="Optional. Sonarr is Radarr’s equivalent for TV. Connect it and Shortlist can ask it for a show it wanted to recommend but couldn’t find on your server."
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
          // Trakt made API keys VIP-only, so people were following our instructions, finding no way
          // to create a key, and reporting it as a Shortlist bug (issue #73). Say it before they go.
          purpose="Optional, and Trakt now charges for this. Trakt is a site where people log what they watch, and its “related titles” are a second place to look for suggestions that often catches things TMDB misses. Creating an API key needs a paid Trakt VIP subscription — everything in Shortlist works without it. Switch the Trakt source on under Finding titles once the key is saved."
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
          purpose="Optional. MDBList fetches a title’s IMDb, Rotten Tomatoes, Metacritic and Trakt scores in one lookup. Needed if you sort a row by “Highest rated” on anything but TMDB, or judge request candidates by those scores. Free key."
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
          service="exa"
          title="Exa (web search)"
          purpose="Optional. Exa is a web search built for AI to read. Shortlist searches it for each of a person’s recent watches, then hands the results to your AI provider to pick from. Works with any provider — and it is the only way a local model like Ollama can search the web at all."
          settings={settings}
          summary={settingString(settings, "exa.apikey") ? "API key saved" : ""}
          glyph={<Globe aria-hidden className="text-primary" />}
          fields={[{ key: "exa.apikey", label: "API key", kind: "password" }]}
          footnote={exaFootnote}
        />
      </div>
      {/* Required by the TMDB API terms of use whenever their data is displayed. */}
      <p className="text-xs text-muted-foreground">
        This product uses the TMDB API but is not endorsed or certified by TMDB.
      </p>
    </section>
  );
}
