import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { InlineKeyField } from "@/components/settings/inline-key-field";
import type { Settings } from "@/lib/types";

const { putSettings, testConnection } = vi.hoisted(() => ({
  putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
  testConnection: vi.fn(() => Promise.resolve({ ok: true, message: "works" })),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, f: string) => f,
  api: { putSettings, testConnection },
}));

function renderField(settings: Settings, helpUrl?: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <InlineKeyField
        settingKey="exa.apikey"
        service="exa"
        label="Exa API key"
        settings={settings}
        helpUrl={helpUrl}
      />
    </QueryClientProvider>,
  );
}

describe("InlineKeyField", () => {
  beforeEach(() => {
    putSettings.mockClear();
    testConnection.mockClear();
  });

  it("saves the typed key under its setting key", async () => {
    renderField({});
    fireEvent.change(screen.getByLabelText(/Exa API key/i), {
      target: { value: "exa-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(putSettings.mock.calls.at(-1)?.[0]).toEqual({
      "exa.apikey": "exa-secret",
    });
  });

  it("won't save (or wipe) when nothing was typed — Save is disabled", () => {
    renderField({});
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
  });

  it("a saved key shows as the redacted sentinel; Test is enabled, Save is a no-op until retyped", () => {
    renderField({ "exa.apikey": "•••••" });
    expect(screen.getByLabelText(/Exa API key/i)).toHaveValue("•••••");
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled(); // untouched → no wipe
    expect(screen.getByRole("button", { name: /test/i })).not.toBeDisabled();
  });

  it("Test probes the service's connection", async () => {
    renderField({ "exa.apikey": "•••••" });
    fireEvent.click(screen.getByRole("button", { name: /test/i }));
    await waitFor(() => expect(testConnection).toHaveBeenCalledWith("exa"));
  });

  it("shows a 'Get a key' link to the provider when helpUrl is given", () => {
    renderField({}, "https://dashboard.exa.ai/api-keys");
    const link = screen.getByRole("link", { name: /get a key/i });
    expect(link).toHaveAttribute("href", "https://dashboard.exa.ai/api-keys");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("omits the 'Get a key' link when no helpUrl is provided", () => {
    renderField({});
    expect(
      screen.queryByRole("link", { name: /get a key/i }),
    ).not.toBeInTheDocument();
  });
});
