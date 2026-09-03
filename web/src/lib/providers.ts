import type { CuratorProvider } from "@/lib/wizard";

/**
 * One canonical description of an AI-curator provider — the single source the settings screen, the
 * setup wizard, and the brand glyphs all read from, so a provider's name, default model, and what
 * it needs (a key, a URL, neither) can never drift between screens.
 */
export interface CuratorProviderInfo {
  id: CuratorProvider;
  /** Brand name shown to the owner. "Claude"/"Gemini" are the recognizable marks, not the vendor. */
  label: string;
  /** Which brand glyph renders for this provider, or null for the no-AI option. */
  glyph: "anthropic" | "openai" | "google" | "ollama" | null;
  defaultModel: string;
  needsKey: boolean;
  /**
   * Offer a key field without requiring one. A local runtime needs no key, but the same
   * OpenAI-compatible API is what hosted gateways (ollama.com cloud, OpenRouter) speak, and those
   * reject every call without one — so "needs a key" and "has somewhere to type a key" are two
   * different questions (issue #88).
   */
  optionalKey?: boolean;
  /** Extra line under the key field, for when it isn't obvious whether one is needed. */
  keyHint?: string;
  needsUrl: boolean;
  /** Which setting the URL is stored under — each URL-taking provider has its own. */
  urlKey?: "curator.ollama_url" | "curator.openai_base_url";
  /** What to call that URL field, and an example of the shape it wants. */
  urlLabel?: string;
  urlPlaceholder?: string;
  /** Where the owner gets an API key (the wizard links to its host). */
  keyUrl?: string;
  /** One-line cost/what-it-is blurb for the wizard's provider cards. */
  cost: string;
}

export const CURATOR_PROVIDERS: readonly CuratorProviderInfo[] = [
  {
    id: "anthropic",
    label: "Claude",
    glyph: "anthropic",
    // Undated alias, matching the backend's DEFAULT_MODEL. A dated id rots on the provider's
    // retirement schedule — which is exactly how `gemini-2.5-flash` below started 404ing.
    defaultModel: "claude-haiku-4-5",
    needsKey: true,
    needsUrl: false,
    keyUrl: "https://console.anthropic.com/settings/keys",
    cost: "Great quality. Costs more than OpenAI/Google — bring your own API key.",
  },
  {
    id: "openai",
    label: "OpenAI",
    glyph: "openai",
    // Must match DEFAULT_MODEL in shortlist/engine/curator/openai.py — the wizard WRITES this into
    // `curator.model`, so a disagreement means two different defaults depending on how you set up.
    // Measured 8x cheaper and 17x faster than gpt-5-mini for the same result; see that file.
    defaultModel: "gpt-4o-mini",
    needsKey: true,
    needsUrl: false,
    keyUrl: "https://platform.openai.com/api-keys",
    cost: "Among the cheaper hosted options. Bring your own API key.",
  },
  {
    id: "google",
    label: "Gemini",
    glyph: "google",
    // An alias, matching the backend's DEFAULT_MODEL. `gemini-2.5-flash` was here until Google
    // retired it for new users, which 404'd every fresh Google setup on its first run.
    defaultModel: "gemini-flash-latest",
    needsKey: true,
    needsUrl: false,
    keyUrl: "https://aistudio.google.com/apikey",
    cost: "Among the cheaper hosted options. Bring your own API key.",
  },
  {
    // ONE entry for every self-hosted runtime: Ollama, llama.cpp, LM Studio, vLLM, LocalAI — and
    // hosted gateways like OpenRouter. They all speak the same OpenAI-compatible API, so a card
    // per runtime was one capability wearing several hats (issue #7).
    id: "openai_compatible",
    label: "Local / OpenAI-compatible",
    glyph: "ollama",
    defaultModel: "",
    needsKey: false,
    optionalKey: true,
    keyHint:
      "Only needed for a hosted gateway like ollama.com or OpenRouter. Leave blank for a server on your own network.",
    needsUrl: true,
    urlKey: "curator.openai_base_url",
    urlLabel: "Server URL",
    urlPlaceholder: "http://localhost:11434",
    cost: "Free and fully local — Ollama, llama.cpp, LM Studio, vLLM or LocalAI. Just the URL of your server. (Also works with any OpenAI-compatible gateway, e.g. OpenRouter.)",
  },
  {
    id: "none",
    label: "None",
    glyph: null,
    defaultModel: "",
    needsKey: false,
    needsUrl: false,
    cost: "Free. Built-in picker: frequency × rating × recency, with template reasons. Fully functional.",
  },
];

/** Look a provider up by its stored `curator.provider` id. */
export function findProvider(id: string): CuratorProviderInfo | undefined {
  return CURATOR_PROVIDERS.find((provider) => provider.id === id);
}
