import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecommendationsSection } from "@/components/settings/recommendations-section";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: { putSettings, testConnection: vi.fn() },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RecommendationsSection settings={settings} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RecommendationsSection", () => {
  beforeEach(() => putSettings.mockClear());

  // The model is "intent + inline fix": a source's toggle is never disabled; when it's on but its
  // dependency is missing, the card shows exactly how to satisfy it right there.

  it("shows an inline Trakt key field when the Trakt source is on without a key", () => {
    renderSection({ "candidates.sources": ["trakt"] });
    expect(screen.getByLabelText(/Trakt API key/i)).toBeInTheDocument();
  });

  it("AI web search: enabling it reveals the backend picker", () => {
    renderSection({
      "curator.provider": "anthropic",
      "candidates.sources": ["llm_web"],
    });
    expect(screen.getByRole("button", { name: /^Exa$/i })).toBeInTheDocument();
  });

  it("AI web search: choosing Exa with no key prompts for the Exa key INLINE (no dead-end)", () => {
    renderSection({
      "curator.provider": "anthropic",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "exa",
    });
    // The fix is right here — not a "go to Connections" message.
    expect(screen.getByLabelText(/Exa API key/i)).toBeInTheDocument();
  });

  it("AI web search: no inline Exa key needed on a native-capable curator using Auto", () => {
    renderSection({
      "curator.provider": "anthropic",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "auto",
    });
    expect(screen.queryByLabelText(/Exa API key/i)).toBeNull();
  });

  it("AI web search: Exa key present → no inline prompt even in Exa mode", () => {
    renderSection({
      "curator.provider": "ollama",
      "exa.apikey": "•••••",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "exa",
    });
    expect(screen.queryByLabelText(/Exa API key/i)).toBeNull();
  });

  it("AI web search: with no curator, prompts to set one up (every backend needs a model)", () => {
    renderSection({
      "curator.provider": "none",
      "exa.apikey": "•••••",
      "candidates.sources": ["llm_web"],
    });
    expect(
      screen.getByText(/needs an AI provider to choose titles/i),
    ).toBeInTheDocument();
  });

  it("AI web search: 'AI provider's own' on a provider that can't self-search (Ollama) warns loudly", () => {
    // Regression: this cell used to show the toggle ON with no prompt while the engine did nothing.
    renderSection({
      "curator.provider": "ollama",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "native",
    });
    expect(
      screen.getByText(/can’t search the web on its own/i),
    ).toBeInTheDocument();
  });

  it("persists an enabled source even when its dependency isn't met yet (intent, not stripped)", async () => {
    renderSection({ "candidates.sources": ["tmdb_similar"] }); // no Trakt key configured
    fireEvent.click(screen.getByLabelText(/Trakt — related titles/i)); // needs a Trakt key
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const sources = putSettings.mock.calls.at(-1)?.[0]?.[
      "candidates.sources"
    ] as string[];
    expect(sources).toContain("trakt"); // kept as intent, NOT stripped for the missing key
  });

  it("saves the backend choice to llm_web.search_provider", async () => {
    renderSection({
      "curator.provider": "anthropic",
      "candidates.sources": ["llm_web"],
    });
    fireEvent.click(screen.getByRole("button", { name: /^Exa$/i }));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["llm_web.search_provider"],
    ).toBe("exa");
  });

  it("persists the owner's intent — enabling a source saves it in candidates.sources", async () => {
    renderSection({ "candidates.sources": ["tmdb_similar"] });
    fireEvent.click(screen.getByLabelText(/TMDB — discover by taste/i));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const sources = putSettings.mock.calls.at(-1)?.[0]?.[
      "candidates.sources"
    ] as string[];
    expect(sources).toContain("tmdb_discover");
  });

  it("auto-saves a change to the watched cap and carries the sources set too", async () => {
    renderSection({ "recommendations.watched_pct": 0.5 });
    const slider = screen.getByRole("slider", { name: /already-watched/i });
    expect(slider).toHaveValue("50");
    fireEvent.change(slider, { target: { value: "55" } });
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const body = putSettings.mock.calls.at(-1)?.[0];
    expect(body?.["recommendations.watched_pct"]).toBe(0.55);
    expect(body).toHaveProperty("candidates.sources");
  });
});
