/** The four Settings tabs, in display order. Each is its own address: `/settings/<value>`. */
export const SETTINGS_TABS = [
  { value: "connections", label: "Connections" },
  { value: "defaults", label: "Defaults" },
  { value: "requests", label: "Requests" },
  { value: "system", label: "System" },
] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number]["value"];

export function isSettingsTab(value: string | undefined): value is SettingsTab {
  return SETTINGS_TABS.some((tab) => tab.value === value);
}

/** The Defaults tab's sections, in page order — the jump list beside them reads from this. */
export const DEFAULTS_SECTIONS = [
  { id: "sources", label: "Title sources" },
  { id: "refresh", label: "Refresh & variety" },
  { id: "row-defaults", label: "Row defaults" },
  { id: "placement", label: "Row placement" },
] as const;

/**
 * Where a known anchor lives now, for the addresses that were in use before Settings was split
 * into tabs. They are in bookmarks, the docs, other screens' links (`/settings#defaults` from a
 * row's "Global row defaults"), and the `action_url` of notifications already stored.
 *
 * An id renamed by the split maps to its new one (`recommendations` was the "Finding titles"
 * section, which is now Title sources + Refresh & variety); every other entry keeps its id.
 */
const ANCHORS: Record<string, { tab: SettingsTab; anchor?: string }> = {
  connections: { tab: "connections" },
  "connections-heading": { tab: "connections", anchor: "connections" },
  // The webhook's switch and events sit with its connection now, so there is one Webhook.
  notifications: { tab: "connections" },
  recommendations: { tab: "defaults", anchor: "sources" },
  "recs-heading": { tab: "defaults", anchor: "sources" },
  sources: { tab: "defaults" },
  refresh: { tab: "defaults" },
  defaults: { tab: "defaults", anchor: "row-defaults" },
  "defaults-heading": { tab: "defaults", anchor: "row-defaults" },
  "row-defaults": { tab: "defaults" },
  placement: { tab: "defaults" },
  "placement-heading": { tab: "defaults", anchor: "placement" },
  requests: { tab: "requests" },
  "requests-heading": { tab: "requests", anchor: "requests" },
  advanced: { tab: "system" },
  "advanced-heading": { tab: "system", anchor: "advanced" },
  "api-access": { tab: "system" },
  "api-access-heading": { tab: "system", anchor: "api-access" },
  "assistant-access": { tab: "system" },
  "assistant-access-heading": { tab: "system", anchor: "assistant-access" },
  danger: { tab: "system" },
  "danger-heading": { tab: "system", anchor: "danger" },
};

/** Controls that other screens deep-link to, all on the Defaults tab (most inside "More
 *  recommendation controls", which opens itself when one of them is the target). */
const DEFAULTS_FIELDS = [
  "watched-pct",
  "refresh-days",
  "idle-hold-days",
  "recency",
  "max-seeds",
  "recent-count",
  "use-plex-ratings",
  "dislike-threshold",
  "min-history",
  "cold-start",
  "rating-source",
];

/** System's controls, each with an id so search can land on it. */
const SYSTEM_FIELDS = ["runs-retention", "events-retention", "log-level", "run-concurrency", "plex-timeout", "pause-all"];

/**
 * The tab and element an anchor names, or `null` when it is not one Settings knows by name (a
 * caller may still find it in the page). `connection-<service>` is each service's row.
 */
export function resolveSettingsAnchor(id: string): { tab: SettingsTab; anchor: string } | null {
  const known = ANCHORS[id];
  if (known) return { tab: known.tab, anchor: known.anchor ?? id };
  if (id.startsWith("connection-")) return { tab: "connections", anchor: id };
  if (DEFAULTS_FIELDS.includes(id)) return { tab: "defaults", anchor: id };
  if (SYSTEM_FIELDS.includes(id)) return { tab: "system", anchor: id };
  return null;
}

/** The tab an old `/settings#…` address opens on. Anything unrecognised opens Connections, which
 *  is where `/settings` alone always landed. */
export function settingsTabForHash(hash: string): SettingsTab {
  return resolveSettingsAnchor(hash.replace(/^#/, ""))?.tab ?? "connections";
}

/** One thing the Settings search can find. `to` is the full address it jumps to. */
export type SettingsSearchEntry = {
  label: string;
  /** Extra words people might type for it: what it is, the services it names. */
  keywords: string;
  to: string;
  /** Where it is, shown beside the result. */
  where: string;
  /** Set when the setting no longer lives in Settings, so the result says so instead of failing. */
  moved?: boolean;
};

const at = (tab: SettingsTab, anchor: string) => `/settings/${tab}#${anchor}`;

export const SETTINGS_SEARCH_INDEX: SettingsSearchEntry[] = [
  { label: "AI assistants", keywords: "mcp chatgpt claude codex connect connection assistant access permissions", to: "/assistant-access", where: "Connections & permissions" },
  { label: "Plex", keywords: "server address token pms", to: at("connections", "connection-plex"), where: "Connections" },
  { label: "TMDB", keywords: "api key catalogue posters", to: at("connections", "connection-tmdb"), where: "Connections" },
  {
    label: "AI & web search",
    keywords: "ai provider model anthropic claude openai gpt gemini google ollama local exa searxng curator key",
    to: at("connections", "connection-llm"),
    where: "Connections",
  },
  { label: "Tautulli", keywords: "names display friendly", to: at("connections", "connection-tautulli"), where: "Connections" },
  { label: "Trakt", keywords: "related titles vip client id", to: at("connections", "connection-trakt"), where: "Connections" },
  { label: "MDBList", keywords: "imdb rotten tomatoes metacritic scores ratings", to: at("connections", "connection-mdblist"), where: "Connections" },
  { label: "Overseerr / Jellyseerr", keywords: "seerr requests", to: at("connections", "connection-overseerr"), where: "Connections" },
  { label: "Radarr", keywords: "movies requests", to: at("connections", "connection-radarr"), where: "Connections" },
  { label: "Sonarr", keywords: "shows tv requests", to: at("connections", "connection-sonarr"), where: "Connections" },
  { label: "Webhook", keywords: "discord slack ntfy gotify home assistant n8n alerts header", to: at("connections", "connection-notify"), where: "Connections" },
  { label: "Send alerts to a webhook", keywords: "notifications what to send events failed run", to: at("connections", "notifications"), where: "Connections" },
  { label: "Title sources", keywords: "tmdb similar discover trakt related candidates", to: at("defaults", "sources"), where: "Defaults" },
  { label: "Web search", keywords: "ai web search exa searxng what to watch next", to: at("defaults", "sources"), where: "Defaults" },
  { label: "Titles refresh every", keywords: "cadence nightly weekly monthly never days refresh", to: at("defaults", "refresh-days"), where: "Defaults" },
  { label: "Already-watched titles", keywords: "watched familiar rewatch percent", to: at("defaults", "watched-pct"), where: "Defaults" },
  { label: "Recent releases", keywords: "recency newer year preference", to: at("defaults", "recency"), where: "Defaults" },
  { label: "Hold rows for inactive viewers", keywords: "idle inactive hold more recommendation controls", to: at("defaults", "idle-hold-days"), where: "Defaults" },
  { label: "How many recent watches to match", keywords: "seeds history taste", to: at("defaults", "max-seeds"), where: "Defaults" },
  { label: "Watches the AI web search looks up", keywords: "recent count searches", to: at("defaults", "recent-count"), where: "Defaults" },
  { label: "Respect Plex ratings", keywords: "ratings dislike thumbs down stars", to: at("defaults", "use-plex-ratings"), where: "Defaults" },
  { label: "Enough watch history", keywords: "minimum history threshold", to: at("defaults", "min-history"), where: "Defaults" },
  { label: "When someone hasn’t watched enough", keywords: "cold start new user fallback", to: at("defaults", "cold-start"), where: "Defaults" },
  { label: "Rate titles using", keywords: "rating source highest rated imdb tmdb", to: at("defaults", "rating-source"), where: "Defaults" },
  { label: "Row name template", keywords: "row name library name user top seed", to: at("defaults", "row-defaults"), where: "Defaults" },
  { label: "How many titles", keywords: "row size length", to: at("defaults", "row-defaults"), where: "Defaults" },
  { label: "Let Shortlist order the Recommended shelf", keywords: "shelf order placement kometa agregarr", to: at("defaults", "placement"), where: "Defaults" },
  { label: "Fill in the gaps automatically", keywords: "requests radarr sonarr overseerr missing", to: at("requests", "requests"), where: "Defaults" },
  { label: "Runs kept", keywords: "history retention months", to: at("system", "runs-retention"), where: "System" },
  { label: "Change log kept", keywords: "events audit retention", to: at("system", "events-retention"), where: "System" },
  { label: "Console log detail", keywords: "log level debug trace docker logs", to: at("system", "log-level"), where: "System" },
  { label: "Run concurrency", keywords: "people at once parallel speed", to: at("system", "run-concurrency"), where: "System" },
  { label: "Plex request timeout", keywords: "timeout seconds slow", to: at("system", "plex-timeout"), where: "System" },
  { label: "What Shortlist has on your Plex", keywords: "audit leftovers collections check plex", to: at("system", "advanced"), where: "System" },
  { label: "API access", keywords: "token api scripts", to: at("system", "api-access"), where: "System" },
  { label: "Pause all users", keywords: "pause resume stop", to: at("system", "pause-all"), where: "System" },
  { label: "Full uninstall", keywords: "uninstall remove restore danger", to: at("system", "danger"), where: "System" },
  {
    label: "Disabled users see nothing",
    keywords: "privacy disabled hide shared rows",
    to: "/privacy",
    where: "Moved to Privacy",
    moved: true,
  },
];

/**
 * The entries matching `query`: every word must appear in the label or its keywords. Label matches
 * come first, so typing a setting's own name finds it at the top.
 */
export function searchSettings(query: string, index: SettingsSearchEntry[] = SETTINGS_SEARCH_INDEX): SettingsSearchEntry[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const scored = index
    .map((entry) => {
      const label = entry.label.toLowerCase();
      const haystack = `${label} ${entry.keywords.toLowerCase()}`;
      if (!words.every((word) => haystack.includes(word))) return null;
      const score = label.startsWith(words.join(" ")) ? 0 : words.every((word) => label.includes(word)) ? 1 : 2;
      return { entry, score };
    })
    .filter((hit): hit is { entry: SettingsSearchEntry; score: number } => hit !== null);
  return scored.sort((a, b) => a.score - b.score).map((hit) => hit.entry);
}
