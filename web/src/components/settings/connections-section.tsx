import { Compass, Film, Globe, Inbox, Server, Tv, Webhook } from "lucide-react";
import type { ReactNode } from "react";

import {
  MdblistGlyph,
  TautulliGlyph,
  TmdbGlyph,
} from "@/components/brand-glyphs";
import { ConnectionCard } from "@/components/connection-card";
import { NotificationsSection, WebhookAlertsSwitch } from "@/components/settings/notifications-section";
import { useWebhookAlerts } from "@/components/settings/webhook-alerts";
import { settingBool, settingString } from "@/lib/format";
import { CURATOR_PROVIDERS, findProvider } from "@/lib/providers";
import { latestRunChain } from "@/lib/dashboard-status";
import { useRuns } from "@/lib/queries";
import { hasExa, hasExternalSearch, hasSearxng } from "@/lib/sources";
import type { Run, Settings, TestableService } from "@/lib/types";

/** The one "Search with" choice, matching the picker on the AI web search card so the two can never
 *  disagree — they are the same setting, shown where each is useful. */
const SEARCH_BACKENDS = [
  { value: "exa", label: "Exa" },
  { value: "native", label: "AI Search" },
  { value: "searxng", label: "SearXNG" },
] as const;

/** Providers whose own model can search the web. Mirrors NATIVE_WEB_SEARCH_PROVIDERS in sources.ts
 *  and `supports_native_web_search` on each curator. Local/OpenAI-compatible runtimes are absent
 *  because there is no tool to switch on — they speak `/v1/chat/completions` and nothing else. */
const CAN_SEARCH_ALONE = ["anthropic", "openai", "google"];

/** The AI providers this backend can actually use, with a reason on any it can't.
 *
 *  Three different answers, which is why one flat list was never going to work:
 *  - `exa`     — every provider, plus None: Exa extracts titles itself, so an AI is optional here.
 *  - `native`  — only providers whose model can search. A local model has no search tool at all.
 *  - `searxng` — every provider EXCEPT None: raw snippets need something to read them. */
function providerOptionsFor(values: Record<string, string>) {
  const backend = values["llm_web.search_provider"] || "native";
  return CURATOR_PROVIDERS.map((provider) => {
    const isNone = provider.id === "none";
    if (backend === "native" && !CAN_SEARCH_ALONE.includes(provider.id))
      return {
        value: provider.id,
        label: provider.label,
        disabled: true,
        reason: isNone
          ? "“AI Search” means your AI does the searching, so it needs one. Pick Exa above instead — it searches on its own and needs no AI at all."
          : `${provider.label} models have no web search built in — there is no tool to switch on. Pick Exa or SearXNG above and ${provider.label} can still choose the titles.`,
      };
    if (backend === "searxng" && isNone)
      return {
        value: provider.id,
        label: provider.label,
        disabled: true,
        reason:
          "SearXNG returns raw web pages, so something has to read them to find the titles. Pick any provider — a local one is fine — or switch to Exa above, which reads its own results.",
      };
    return { value: provider.id, label: provider.label };
  });
}

/** The line under the provider buttons: whether one is needed here, and the Gemini caveat.
 *
 *  Gemini is NOT disabled. Re-measured 2026-09-03 under the year-anchored prompt: it still issues no
 *  search queries, but returned 12 of 12 titles from 2024 or later, overlapping what a searching
 *  control found. Its training data is recent enough. The real cost is that it cannot refresh itself
 *  as its cutoff recedes — a caveat, not a defect, so it says so rather than blocking the choice. */
function providerHint(values: Record<string, string>): string | undefined {
  const backend = values["llm_web.search_provider"] || "native";
  const provider = values["curator.provider"] || "none";
  // If the SELECTED provider is one this backend can't use, that is the only thing worth saying —
  // and it has to be said in text: a disabled button carries `pointer-events-none`, so its tooltip
  // is unreachable by both mouse and keyboard.
  const blocked = providerOptionsFor(values).find(
    (o) => o.value === provider && o.disabled,
  );
  if (blocked?.reason) return blocked.reason;
  if (backend === "native" && provider === "google")
    return "Gemini answers from its own knowledge rather than searching. Its picks are current today, but unlike Claude and GPT it won’t refresh them — they age with the model.";
  if (backend === "exa")
    return provider === "none"
      ? "Not needed here. Add one and it picks which of Exa’s titles suit each person — and on OpenAI or Gemini, makes row poster art."
      : "Optional here. It picks which of Exa’s titles suit each person, and on OpenAI or Gemini it also makes row poster art.";
  if (backend === "searxng")
    return "Required: something has to read SearXNG’s raw snippets. Any provider will do, a local one included.";
  return "Required: with this backend, your AI is what does the searching.";
}

/** How hard Exa works per search, cheapest first.
 *
 *  The buttons carry the name only; the trade-off goes in `hint`, shown one line at a time for
 *  whichever depth is selected. Labels used to carry price and caveat inline, which made a row of
 *  six buttons unreadable.
 *
 *  The cheap modes are called erratic because they measurably are: on the same two searches
 *  "Balanced" found 13 and 8 usable titles where "Thorough" found 47 and 36, and once returned
 *  nothing at all. Values must match EXA_SEARCH_TYPES on the server. */
const EXA_SEARCH_TYPES = [
  {
    value: "instant",
    label: "Instant",
    hint: "Cheapest and fastest, but unreliable: measured at 11 titles one run and none the next. Fine with an AI provider, which can fall back to reading the raw articles — risky without one.",
  },
  {
    value: "auto",
    label: "Auto",
    hint: "Exa chooses the strategy per search, and it is their own recommended setting. Measured the weakest here — 2 picks where Instant gave 7, at the same price.",
  },
  {
    value: "deep-lite",
    label: "Deep lite",
    hint: "Recommended, and the only mode that produced titles on every single test. Found three to four times more than the cheap modes, for about half a cent more.",
  },
  {
    value: "deep",
    label: "Deep",
    hint: "Faster than Deep lite and steadier, but finds fewer titles. Same price.",
  },
] as const;

/** The line under the backend buttons: whether this backend needs an AI provider, and what it does
 *  without one. The answer differs per backend and none of it was visible anywhere:
 *
 *  - native  — the AI IS the search, so an AI is required by definition (and Gemini declines to).
 *  - exa     — Exa extracts titles itself via `outputSchema`, so it works with NO AI at all.
 *  - searxng — returns raw snippets, so something must read them: an AI is required.
 *
 *  Worth stating plainly because the failure was silent and expensive: Exa with the provider set to
 *  None used to pay for every search and discard the titles. */
function backendHint(chosen: string | undefined): string | undefined {
  const backend = chosen || "native";
  if (backend === "exa")
    return "Recommended. Finds the most titles, and reads its own results — so it works with no AI provider at all.";
  if (backend === "searxng")
    return "Free and self-hosted. Returns raw web snippets, so it needs an AI provider below to read them.";
  // native — which provider does the searching, and its caveats, belong to the provider field below.
  return "Your AI searches the web itself, as part of one call per person. Billed by your provider.";
}

/** Whether the card should show `backend`'s fields for the pending choice. Exactly one backend runs,
 *  so exactly one backend's fields are ever asked for. */
function offers(
  values: Record<string, string>,
  backend: "exa" | "searxng",
): boolean {
  return (values["llm_web.search_provider"] || "native") === backend;
}

/** What the one Test button probes.
 *
 *  The external backend when one is set up — a bad Exa key is the failure people actually hit — and
 *  the AI provider otherwise, which is also what gives heuristic mode ("none") a real answer instead
 *  of erroring on a search that cannot run.
 *
 *  Deliberately never `native_search`: that probe fires a REAL billable web search, and this card
 *  auto-tests on every visit to Settings. The old standalone card could afford it by suppressing
 *  autoTest for exactly that case; one merged card cannot without losing the auto-probe for the
 *  external backends, which is the more useful of the two. The cost is that "your plan may not
 *  allow the web-search tool" is no longer caught until a run — see the design doc. */
function testableSearchService(settings: Settings): TestableService {
  const chosen = settingString(settings, "llm_web.search_provider") || "native";
  if (chosen === "exa" && hasExa(settings)) return "exa";
  if (chosen === "searxng" && hasSearxng(settings)) return "searxng";
  // No external backend on file: the useful probe is the AI provider itself, which also gives
  // heuristic mode ("none") a real answer instead of erroring on a search that cannot run.
  return "llm";
}

/** The collapsed card's one-line state. Covers BOTH halves the merged card owns, because either
 *  alone is a real configuration: with `exa` chosen but no key yet, a perfectly good Claude key was
 *  rendering as "Not set up" with a disabled Test button, because `configured` is `Boolean(summary)`. */
function searchSummary(settings: Settings): string {
  const chosen = settingString(settings, "llm_web.search_provider") || "native";
  const providerId = settingString(settings, "curator.provider");
  // "None" (heuristic mode) COUNTS as configured — it is a deliberate choice with a real answer
  // ("Built-in picker — no AI, nothing to test"), and `configured` is `Boolean(summary)`, so
  // excluding it disabled the Test button that e2e asserts on. Only a genuinely unset provider is
  // blank.
  const ai = providerId ? (findProvider(providerId)?.label ?? providerId) : "";
  const where =
    chosen === "searxng"
      ? hasSearxng(settings)
        ? `SearXNG · ${settingString(settings, "searxng.url")}`
        : ""
      : chosen === "exa"
        ? hasExa(settings)
          ? "Exa"
          : ""
        : // "its own web search" only where there IS one — never for None or a local model.
          providerId && providerId !== "none"
          ? "its own web search"
          : "";
  // Either half alone counts as configured: Exa runs with no AI, and a saved AI key is real even
  // before its backend has one. Empty only when genuinely nothing is set.
  if (ai && where) return `${ai} · ${where}`;
  return where || ai;
}

/** How long a web search is reused before it is paid for again — the copy half of
 *  `WEB_SEARCH_CACHE_TTL_S` in shortlist/engine/candidates.py. Kept in step by
 *  `test_cache_prune.py::test_cache_ttl_matches_the_ui`, which reads this file. */
const WEB_SEARCH_CACHE_DAYS = 7;

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
  // The cache is why this number is far lower than "people x titles" and why it swings run to run,
  // so state it here rather than leaving the count looking broken. It is also the only place the
  // owner is told their picks can lag a new release by up to a week.
  // Mirrors WEB_SEARCH_CACHE_TTL_S (shortlist/engine/candidates.py); test_cache_ttl_matches_the_ui
  // fails if the two drift apart.
  return `Last run: ${lastSearches.toLocaleString()} web search${lastSearches === 1 ? "" : "es"} · results are shared by everyone and reused for ${WEB_SEARCH_CACHE_DAYS} days`;
}

/** Searches in the latest finished run plus the scheduled runs chained with it. Two cron groups run
 *  back to back and the second often searches nothing, so its 0 must not stand in for the night. */
function lastNightsSearches(runs: Run[] | undefined): number | undefined {
  const chain = latestRunChain(runs)?.runs ?? [runs?.find((r) => r.finished_at)];
  const counts = chain.map((run) => run?.stats?.exa_searches);
  return counts.some((n) => n != null) ? counts.reduce<number>((sum, n) => sum + (n ?? 0), 0) : undefined;
}

/** "2 of 4 set up": how many of a group's services have something on file. Set up, not "connected" —
 *  whether each one actually answers is its own pill, from a real test. */
function setUpCount(summaries: string[]): string {
  const done = summaries.filter(Boolean).length;
  return done === 0 ? "None set up" : `${done} of ${summaries.length} set up`;
}

/** One group of services: a heading, a line on why the group matters, and one row per service. */
function ConnectionGroup({
  id,
  title,
  description,
  count,
  children,
}: {
  id: string;
  title: string;
  description: string;
  count?: string;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className="scroll-mt-32 overflow-hidden rounded-lg border bg-card md:scroll-mt-8"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 border-b px-4 py-3.5 sm:px-5">
        <div className="min-w-0 space-y-0.5">
          <h2 id={`${id}-title`} className="text-sm font-semibold">
            {title}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">{description}</p>
        </div>
        {count && <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{count}</span>}
      </div>
      <div className="divide-y">{children}</div>
    </section>
  );
}

/** Connections: every service Shortlist talks to, one row each, editable and testable in place. */
export function ConnectionsSection({ settings }: { settings: Settings }) {
  const runs = useRuns();
  const lastSearches = lastNightsSearches(runs.data);
  // "No AI" is a choice, not a connection: rows use the built-in picker and there is nothing to test.
  const searchBuiltInOnly = settingString(settings, "curator.provider") === "none" && !hasExternalSearch(settings);
  const alerts = useWebhookAlerts(settings);
  const summaries = {
    plex: settingString(settings, "plex.url"),
    tmdb: settingString(settings, "tmdb.apikey") ? "API key saved" : "",
    search: searchSummary(settings),
    tautulli: settingString(settings, "tautulli.url"),
    trakt: settingString(settings, "trakt.client_id") ? "API key saved" : "",
    mdblist: settingString(settings, "requests.mdblist.apikey") ? "API key saved" : "",
    overseerr: settingString(settings, "requests.overseerr.url"),
    radarr: settingString(settings, "requests.radarr.url"),
    sonarr: settingString(settings, "requests.sonarr.url"),
    webhook: settingString(settings, "notify.webhook.url")
      ? settingString(settings, "notify.webhook.auth_header_value") &&
        settingString(settings, "notify.webhook.auth_header_name")
        ? "Address and auth header saved"
        : "Address saved"
      : "",
  };
  return (
    <div id="connections" className="scroll-mt-32 space-y-6 md:scroll-mt-8">

        <ConnectionGroup
          id="connections-essential"
          title="Essential"
          description="Shortlist can’t build rows without these."
          count={setUpCount([summaries.plex, summaries.tmdb])}
        >
<ConnectionCard
          service="plex"
          title="Plex"
          need="required"
          purpose="Where Shortlist reads watch history, and where it builds each person’s row."
          settings={settings}
          summary={summaries.plex}
          glyph={<Server aria-hidden className="text-muted-foreground" />}
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
          service="tmdb"
          title="TMDB"
          need="required"
          purpose="The free catalogue Shortlist looks titles up in. A key is free."
          settings={settings}
          summary={summaries.tmdb}
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
        </ConnectionGroup>
        <ConnectionGroup
          id="connections-discovery"
          title="Discovery & watch history"
          description="Each one widens where picks come from, or how people are named. Rows build fine without them."
          count={setUpCount([searchBuiltInOnly ? "" : summaries.search, summaries.tautulli, summaries.trakt, summaries.mdblist])}
        >
<ConnectionCard
          service={testableSearchService(settings)}
          testId="connection-llm"
          title="AI & Web search"
          need="optional"
          purpose="Finds what critics and “what to watch next” articles are recommending right now, and keeps only the titles you already own. Optional — without it, rows are built from your library alone."
          settings={settings}
          summary={summaries.search}
          builtInOnly={searchBuiltInOnly}
          glyph={<Globe aria-hidden className="text-primary" />}
          footnote={searchFootnote(
            settings,
            lastSearches,
          )}
          fields={[
            {
              key: "llm_web.search_provider",
              label: "Where to search",
              kind: "select",
              options: [...SEARCH_BACKENDS],
              hint: (v) => backendHint(v["llm_web.search_provider"]),
            },
            {
              key: "exa.apikey",
              label: "Exa API key",
              kind: "password",
              helpUrl: "https://dashboard.exa.ai/api-keys",
              showIf: (v) => offers(v, "exa"),
            },
            {
              key: "exa.search_type",
              label: "Search depth",
              kind: "select",
              // `label` alone — the option's `hint` is rendered under the row, not on the button.
              options: EXA_SEARCH_TYPES.map(({ value, label }) => ({
                value,
                label,
              })),
              hint: (v) =>
                EXA_SEARCH_TYPES.find(
                  (t) => t.value === (v["exa.search_type"] || "deep-lite"),
                )?.hint,
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
            {
              key: "curator.provider",
              label: "AI provider",
              kind: "select",
              // The list narrows to what the chosen backend can actually use. Disabled options stay
              // visible with the reason attached, so "why can't I pick this?" is answered in place.
              options: providerOptionsFor,
              hint: providerHint,
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
          service="tautulli"
          title="Tautulli"
          purpose="Supplies the friendlier names your users go by, so rows say “Sarah” and not an email address."
          settings={settings}
          summary={summaries.tautulli}
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
          service="trakt"
          title="Trakt"
          // Trakt made API keys VIP-only, so people followed our instructions, found no way to
          // create a key, and reported it as a Shortlist bug (issue #73). The badge says it before
          // they go looking, rather than burying it mid-paragraph where it was skimmed past.
          requires="Needs paid Trakt VIP"
          purpose="Its “related titles” often catch suggestions TMDB misses. Switch the source on under Defaults → Title sources once the key is saved."
          settings={settings}
          summary={summaries.trakt}
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
          // "off TMDB" read two ways — scores taken FROM TMDB, or a row switched AWAY from TMDB —
          // and it means the second. Say which score is being swapped for which instead.
          purpose="IMDb, Rotten Tomatoes, Metacritic and Trakt scores in one lookup. Only needed if you want a “Highest rated” row ranked by one of those instead of by TMDB’s score, or requests judged by them."
          settings={settings}
          summary={summaries.mdblist}
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
        </ConnectionGroup>
        <ConnectionGroup
          id="connections-requests"
          title="Requests"
          description="Where picks that aren’t in your library get sent, once requests are turned on in the Requests tab."
          count={setUpCount([summaries.overseerr, summaries.radarr, summaries.sonarr])}
        >
<ConnectionCard
          service="overseerr"
          title="Overseerr / Jellyseerr"
          purpose="An alternative to connecting Radarr and Sonarr directly: Shortlist files a request here and it fetches the title, using its own quality settings and approval rules. Works with Overseerr, Jellyseerr and Seerr — they share one API."
          settings={settings}
          summary={summaries.overseerr}
          unsetLabel="Not set up"
          glyph={<Inbox aria-hidden className="text-primary" />}
          fields={[
            {
              key: "requests.overseerr.url",
              label: "Address",
              kind: "text",
              placeholder: "http://your-host:5055",
            },
            {
              key: "requests.overseerr.apikey",
              label: "API key",
              kind: "password",
            },
          ]}
        />
<ConnectionCard
          service="radarr"
          title="Radarr"
          purpose="Fetches films Shortlist wanted to recommend but couldn’t find on your server."
          settings={settings}
          summary={summaries.radarr}
          unsetLabel="Not set up"
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
          purpose="Fetches shows Shortlist wanted to recommend but couldn’t find on your server."
          settings={settings}
          summary={summaries.sonarr}
          unsetLabel="Not set up"
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
        </ConnectionGroup>
        <ConnectionGroup
          id="notifications"
          title="Notifications"
          description="Shortlist runs while you’re asleep. This is how it tells you what happened."
        >
<ConnectionCard
          service="notify"
          title="Webhook"
          purpose="Where Shortlist sends its alerts: a Discord or Slack channel, ntfy, Gotify, Home Assistant, n8n, or anything else that accepts a webhook."
          settings={settings}
          footnote={settingString(settings, "notify.webhook.url") ? <WebhookNextStep settings={settings} /> : undefined}
          headerExtra={<WebhookAlertsSwitch alerts={alerts} />}
          summary={summaries.webhook}
          unsetLabel="Not set up"
          glyph={<Webhook aria-hidden className="text-primary" />}
          // Its test posts a real message into the owner's channel, so it never runs by itself.
          autoTest={false}
          testLabel="Send a test"
          fields={[
            {
              // A secret: a Discord or Slack webhook address works as a password for that channel.
              key: "notify.webhook.url",
              label: "Address",
              kind: "password",
              placeholder: "https://discord.com/api/webhooks/…",
            },
            {
              key: "notify.webhook.auth_header_name",
              label: "Header name",
              kind: "text",
              placeholder: "Authorization",
              hint: "Only if your receiver needs a key, as ntfy, Gotify and n8n can. Clear the name to stop sending a header.",
            },
            {
              key: "notify.webhook.auth_header_value",
              label: "Header value",
              kind: "password",
              placeholder: "Bearer …",
              hint: "Sent with every message, the test included. Shown as dots once saved.",
            },
          ]}
        />
          {/* The switch and the events sit with the webhook they send to: one Webhook, one place. */}
          <NotificationsSection settings={settings} alerts={alerts} />
        </ConnectionGroup>
      {/* Required by the TMDB API terms of use whenever their data is displayed. */}
      <p className="text-xs text-muted-foreground">
        This product uses the TMDB API but is not endorsed or certified by TMDB.
      </p>
    </div>
  );
}

/**
 * Saving the address sends nothing by itself: the switch and the events are just below it, under
 * Notifications. Once an address is saved, say which of those states it is in and link there.
 */
function WebhookNextStep({ settings }: { settings: Settings }) {
  if (!settingString(settings, "notify.webhook.url")) return null;
  const events = settings["notify.webhook.events"];
  const count = Array.isArray(events) ? events.length : 0;
  const lead = !settingBool(settings, "notify.webhook.enabled")
    ? "Not sending yet — turn it on and choose what to send in"
    : count === 0
      ? "Switched on, but nothing is ticked — choose what to send in"
      : `Sends ${count} ${count === 1 ? "kind" : "kinds"} of alert — change them in`;
  return (
    <>
      {lead}{" "}
      <a
        href="#notifications"
        className="font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Notifications →
      </a>
    </>
  );
}
