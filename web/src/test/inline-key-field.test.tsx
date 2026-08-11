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

  describe("secret={false} — a setting that is an address, not a credential", () => {
    function renderUrlField(settings: Settings) {
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
        <QueryClientProvider client={client}>
          <InlineKeyField
            settingKey="searxng.url"
            service="searxng"
            label="SearXNG address"
            settings={settings}
            secret={false}
          />
        </QueryClientProvider>,
      );
    }

    it("shows a saved address in the clear — dots would hide the thing you need to check", () => {
      renderUrlField({ "searxng.url": "http://searx.local:8080" });
      expect(screen.getByLabelText(/SearXNG address/i)).toHaveValue(
        "http://searx.local:8080",
      );
    });

    it("saves an edited address, since there is no sentinel to guard against wiping", () => {
      renderUrlField({ "searxng.url": "http://old:8080" });
      fireEvent.change(screen.getByLabelText(/SearXNG address/i), {
        target: { value: "http://new:8080" },
      });
      fireEvent.click(screen.getByRole("button", { name: /save/i }));
      return waitFor(() =>
        expect(putSettings.mock.calls.at(-1)?.[0]).toEqual({
          "searxng.url": "http://new:8080",
        }),
      );
    });

    it("can Test an address that is already saved", () => {
      renderUrlField({ "searxng.url": "http://searx.local:8080" });
      fireEvent.click(screen.getByRole("button", { name: /test/i }));
      return waitFor(() =>
        expect(testConnection).toHaveBeenCalledWith("searxng"),
      );
    });
  });
});
