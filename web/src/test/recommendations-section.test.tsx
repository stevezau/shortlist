import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RecommendationsSection } from "@/components/settings/recommendations-section";
import type { Settings } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: { putSettings: (values: Settings) => Promise.resolve(values) },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RecommendationsSection settings={settings} />
    </QueryClientProvider>,
  );
}

describe("RecommendationsSection", () => {
  it("blocks the AI-from-library source until a curator is configured", () => {
    renderSection({});
    expect(screen.getByLabelText(/suggests from your library/i)).toBeDisabled();
    // Both AI sources (library + web search) show the curator-needed hint.
    expect(screen.getAllByText(/Needs an AI curator/i).length).toBeGreaterThan(
      0,
    );
  });

  it("allows the AI-from-library source once a curator is set", () => {
    renderSection({ "curator.provider": "anthropic" });
    expect(
      screen.getByLabelText(/suggests from your library/i),
    ).not.toBeDisabled();
    expect(screen.queryByText(/Needs an AI curator/i)).toBeNull();
  });
});
