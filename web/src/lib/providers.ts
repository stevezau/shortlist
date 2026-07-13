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
    id: "ollama",
    label: "Ollama",
    glyph: "ollama",
    defaultModel: "llama3.3",
    needsKey: false,
    needsUrl: true,
    cost: "Free and fully local — no key, just a URL to your Ollama server.",
  },
  {
    id: "none",
    label: "None",
    glyph: null,
    defaultModel: "",
    needsKey: false,
    needsUrl: false,
    cost: "Free. Heuristic mode: frequency × rating × recency, with template reasons. Fully functional.",
  },
];

/** Look a provider up by its stored `curator.provider` id. */
export function findProvider(id: string): CuratorProviderInfo | undefined {
  return CURATOR_PROVIDERS.find((provider) => provider.id === id);
}
