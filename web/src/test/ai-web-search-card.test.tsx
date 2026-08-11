import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AiWebSearchCard } from "@/components/settings/ai-web-search-card";
import type { Settings } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, f: string) => f,
  api: {
    putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
    testConnection: vi.fn(() => Promise.resolve({ ok: true, message: "ok" })),
  },
}));

function renderCard(settings: Record<string, string>, backend: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AiWebSearchCard
        settings={settings as unknown as Settings}
        enabled
        onToggle={() => {}}
        backend={backend}
        onBackendChange={() => {}}
      />
    </QueryClientProvider>,
  );
}

const OLLAMA = { "curator.provider": "ollama" };
const CLAUDE = { "curator.provider": "anthropic" };
const SEARX = { "searxng.url": "http://searx.local:8080" };
const EXA = { "exa.apikey": "•••••" };

describe("AiWebSearchCard", () => {
  it("offers SearXNG alongside the other backends", () => {
    renderCard(OLLAMA, "auto");
    expect(screen.getByRole("button", { name: "SearXNG" })).toBeInTheDocument();
  });

  it("asks for the address inline when SearXNG is chosen but unset", () => {
    // No dead-ending at "set it up in Connections" — the card takes the address itself.
    renderCard(OLLAMA, "searxng");
    expect(screen.getByLabelText(/SearXNG address/i)).toBeInTheDocument();
  });

  it("states the JSON prerequisite before an address is entered", () => {
    // A stock SearXNG serves HTML only and answers our request with a 403. This sentence is the
    // difference between "it works" and an owner filing a bug — it must be on screen at setup time.
    renderCard(OLLAMA, "searxng");
    expect(screen.getByText(/search\.formats/)).toBeInTheDocument();
  });

  it("keeps stating it AFTER the address is saved", () => {
    // The failure is silent: a saved address plus an HTML-only instance means the source quietly
    // finds nothing. If the warning vanished once the field did, there'd be no clue left on screen.
    renderCard({ ...OLLAMA, ...SEARX }, "searxng");
    expect(screen.getByText(/search\.formats/)).toBeInTheDocument();
  });

  it("states it when Auto resolved to SearXNG, not just when it was picked by name", () => {
    renderCard({ ...OLLAMA, ...SEARX }, "auto");
    expect(screen.getByText(/search\.formats/)).toBeInTheDocument();
  });

  it("does not nag about JSON when the backend isn't SearXNG", () => {
    renderCard({ ...OLLAMA, ...EXA }, "exa");
    expect(screen.queryByText(/search\.formats/)).not.toBeInTheDocument();
  });

  it("offers both external backends when auto has neither", () => {
    renderCard(OLLAMA, "auto");
    expect(screen.getByLabelText(/Exa API key/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/SearXNG address/i)).toBeInTheDocument();
  });

  it("stops asking once one external backend is configured", () => {
    renderCard({ ...OLLAMA, ...SEARX }, "auto");
    expect(screen.queryByLabelText(/Exa API key/i)).not.toBeInTheDocument();
  });

  it("asks a native-capable provider on Auto for nothing at all", () => {
    // Claude already searches on its own, so under Auto there is no missing dependency — prompting
    // for one would invent a setup step that isn't needed.
    renderCard(CLAUDE, "auto");
    expect(screen.queryByLabelText(/Exa API key/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/SearXNG address/i)).not.toBeInTheDocument();
  });

  it("says auto picked SearXNG when both are configured", () => {
    // Mirrors make_search_client on the server: auto never mints an Exa bill on its own.
    renderCard({ ...CLAUDE, ...SEARX, ...EXA }, "auto");
    expect(
      screen.getByText(/own search and your SearXNG instance together/i),
    ).toBeInTheDocument();
  });

  it("shows Exa's billing note only when Exa is the resolved backend", () => {
    renderCard({ ...OLLAMA, ...EXA }, "exa");
    expect(screen.getByText(/free tier/i)).toBeInTheDocument();
    renderCard({ ...OLLAMA, ...SEARX }, "searxng");
    expect(screen.queryAllByText(/free tier/i)).toHaveLength(1); // still only the first render's
  });

  it("tells an Ollama owner how to escape the native-only dead end", () => {
    renderCard(OLLAMA, "native");
    // The warning must name SearXNG as a way out, not just Exa — that dead end IS issue #78.
    const warning = screen.getByText(/can’t search the web on its own/i);
    expect(warning.textContent).toMatch(/SearXNG/);
  });
});
