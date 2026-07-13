import { Sparkles } from "lucide-react";

import {
  PlexGlyph,
  ProviderGlyph,
  TautulliGlyph,
  TmdbGlyph,
} from "@/components/brand-glyphs";
import { ConnectionCard } from "@/components/connection-card";
import { settingString } from "@/lib/format";
import { CURATOR_PROVIDERS } from "@/lib/providers";
import type { Settings } from "@/lib/types";

const PROVIDER_OPTIONS = CURATOR_PROVIDERS.map((provider) => ({
  value: provider.id,
  label: provider.label,
}));

/** Connections: Plex, Tautulli, TMDB, and the AI curator — each editable and testable in place. */
export function ConnectionsSection({ settings }: { settings: Settings }) {
  return (
    <section aria-labelledby="connections-heading" className="space-y-3">
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
          summary={settingString(settings, "curator.provider")}
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
              placeholder: "e.g. claude-sonnet-4-5",
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
      </div>
      {/* Required by the TMDB API terms of use whenever their data is displayed. */}
      <p className="text-xs text-muted-foreground">
        This product uses the TMDB API but is not endorsed or certified by TMDB.
      </p>
    </section>
  );
}
