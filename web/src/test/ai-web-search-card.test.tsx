import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AiWebSearchCard } from "@/components/settings/ai-web-search-card";
import type { Settings } from "@/lib/types";

function renderCard(settings: Record<string, string>, enabled = true) {
  render(
    <AiWebSearchCard
      settings={settings as unknown as Settings}
      enabled={enabled}
      onToggle={() => {}}
    />,
  );
}

const OLLAMA = { "curator.provider": "ollama" };
const CLAUDE = { "curator.provider": "anthropic" };
const SEARX = { "searxng.url": "http://searx.local:8080" };
const EXA = { "exa.apikey": "•••••" };

describe("AiWebSearchCard", () => {
  it("does not duplicate the backend picker that lives in Connections", () => {
    // The same control rendered in two places with no way to tell which was authoritative. This
    // card owns whether the source runs; Connections owns which service it runs through.
    renderCard({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" });
    expect(
      screen.queryByRole("button", { name: "SearXNG" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/SearXNG address/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Exa API key/i)).not.toBeInTheDocument();
  });

  it("still names the backend it will search with, and links to where to change it", () => {
    // Not duplicating the control must not mean hiding what it is — "on" has to be readable.
    renderCard({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" });
    expect(screen.getByText(/your SearXNG instance/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /change it in Connections/i }),
    ).toHaveAttribute("href", "#connections");
  });

  it("names the provider's own search when that is the backend", () => {
    renderCard({ ...CLAUDE, "llm_web.search_provider": "native" });
    expect(
      screen.getByText(/your AI provider’s own search/),
    ).toBeInTheDocument();
  });

  it("says so when the chosen backend has not been set up", () => {
    renderCard({ ...OLLAMA, "llm_web.search_provider": "searxng" });
    expect(
      screen.getByText(/No SearXNG address is saved yet/),
    ).toBeInTheDocument();
  });

  it("tells an Ollama owner why the provider's own search cannot work", () => {
    renderCard({ ...OLLAMA, "llm_web.search_provider": "native" });
    expect(
      screen.getByText(/can’t search the web on its own/),
    ).toBeInTheDocument();
  });

  it("reports a missing AI provider ahead of any backend detail", () => {
    renderCard({ ...SEARX, "llm_web.search_provider": "searxng" });
    expect(screen.getByText(/needs an AI provider/)).toBeInTheDocument();
  });

  it("is silent about problems once the source can actually run", () => {
    renderCard({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" });
    expect(screen.queryByText(/saved yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/needs an AI provider/)).not.toBeInTheDocument();
  });

  it("keeps stating the JSON prerequisite while SearXNG is the backend", () => {
    // A stock SearXNG serves HTML only and refuses us with a 403; the failure is otherwise silent.
    renderCard({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" });
    expect(screen.getByText(/search\.formats/)).toBeInTheDocument();
  });

  it("does not nag about JSON when the backend isn't SearXNG", () => {
    renderCard({ ...OLLAMA, ...EXA, "llm_web.search_provider": "exa" });
    expect(screen.queryByText(/search\.formats/)).not.toBeInTheDocument();
  });

  it("shows Exa's billing note only for Exa", () => {
    renderCard({ ...OLLAMA, ...EXA, "llm_web.search_provider": "exa" });
    expect(screen.getByText(/free tier/i)).toBeInTheDocument();
  });

  it("says SearXNG searches are free", () => {
    renderCard({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" });
    expect(screen.getByText(/cost nothing/i)).toBeInTheDocument();
  });

  it("shows none of the detail while the source is switched off", () => {
    renderCard(
      { ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" },
      false,
    );
    expect(screen.queryByText(/Searching with/)).not.toBeInTheDocument();
    expect(screen.queryByText(/How much it searches/)).not.toBeInTheDocument();
  });

  it("treats a stored 'auto' as the default rather than showing a retired backend", () => {
    // 0063 pins every install off `auto`, but the UI must not render a dead value if one survives.
    renderCard({ ...CLAUDE, "llm_web.search_provider": "auto" });
    expect(
      screen.getByText(/your AI provider’s own search/),
    ).toBeInTheDocument();
  });
});
