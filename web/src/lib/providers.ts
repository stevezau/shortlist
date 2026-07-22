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
    defaultModel: "claude-haiku-4-5-20251001",
    needsKey: true,
    needsUrl: false,
    keyUrl: "https://console.anthropic.com/settings/keys",
    cost: "Pennies per night on the cheap tier — bring your own API key.",
  },
  {
    id: "openai",
    label: "OpenAI",
    glyph: "openai",
    defaultModel: "gpt-5-mini",
    needsKey: true,
    needsUrl: false,
    keyUrl: "https://platform.openai.com/api-keys",
    cost: "Pennies per night on the mini tier — bring your own API key.",
  },
  {
    id: "google",
    label: "Gemini",
    glyph: "google",
    defaultModel: "gemini-2.5-flash",
    needsKey: true,
    needsUrl: false,
    keyUrl: "https://aistudio.google.com/apikey",
    cost: "Pennies per night on the Flash tier — bring your own API key.",
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
