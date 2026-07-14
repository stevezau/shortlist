import { Compass, Film, Sparkles, Tv } from "lucide-react";

import {
  PlexGlyph,
  ProviderGlyph,
  TautulliGlyph,
  TmdbGlyph,
} from "@/components/brand-glyphs";
import { ConnectionCard } from "@/components/connection-card";
import { settingString } from "@/lib/format";
import { CURATOR_PROVIDERS, findProvider } from "@/lib/providers";
import type { Settings } from "@/lib/types";

const PROVIDER_OPTIONS = CURATOR_PROVIDERS.map((provider) => ({
  value: provider.id,
  label: provider.label,
}));

/** Connections: Plex, Tautulli, TMDB, and the AI curator — each editable and testable in place. */
export function ConnectionsSection({ settings }: { settings: Settings }) {
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
          purpose="Your media server — where the personalized rows appear."
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
          purpose="Optional. A richer source of who-watched-what history."
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
          purpose="Finds similar titles to suggest. Needs a free key."
          settings={settings}
          summary={
            settingString(settings, "tmdb.apikey") ? "API key saved" : ""
          }
          glyph={<TmdbGlyph />}
          fields={[{ key: "tmdb.apikey", label: "API key", kind: "password" }]}
        />
        <ConnectionCard
          service="llm"
          title="AI curator"
          purpose="Writes each row and its “why we picked this”. Optional — a no-AI mode works too."
          settings={settings}
          summary={
            // "none" is the default and is a real choice, not a configured connection — show the
            // provider's friendly label only when an AI provider is actually set, else "" (not set).
            settingString(settings, "curator.provider") &&
            settingString(settings, "curator.provider") !== "none"
              ? (findProvider(settingString(settings, "curator.provider"))
                  ?.label ?? settingString(settings, "curator.provider"))
              : ""
          }
          glyph={
            <ProviderGlyph
              provider={settingString(settings, "curator.provider")}
              fallback={<Sparkles aria-hidden className="text-primary" />}
            />
          }
          fields={[
            {
              key: "curator.provider",
              label: "Provider",
              kind: "select",
              options: PROVIDER_OPTIONS,
            },
            {
              key: "curator.model",
              label: "Model (blank = a sensible default)",
              kind: "text",
              placeholder: "e.g. claude-haiku-4-5",
              showIf: (v) => v["curator.provider"] !== "none",
            },
            {
              key: "curator.api_key",
              label: "API key",
              kind: "password",
              showIf: (v) =>
                !["none", "ollama"].includes(v["curator.provider"] ?? ""),
            },
            {
              key: "curator.ollama_url",
              label: "Ollama URL",
              kind: "text",
              placeholder: "http://localhost:11434",
              showIf: (v) => v["curator.provider"] === "ollama",
            },
          ]}
        />
        <ConnectionCard
          service="radarr"
          title="Radarr"
          purpose="Optional. Lets Shortlist request missing movies so they get downloaded."
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
          purpose="Optional. Lets Shortlist request missing TV shows so they get downloaded."
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
          purpose="Optional. A recommendation source — Trakt's 'related titles' can surface picks TMDB misses."
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
      </div>
      {/* Required by the TMDB API terms of use whenever their data is displayed. */}
      <p className="text-xs text-muted-foreground">
        This product uses the TMDB API but is not endorsed or certified by TMDB.
      </p>
    </section>
  );
}
