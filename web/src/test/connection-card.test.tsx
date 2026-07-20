import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ConnectionCard,
  type ConnectionField,
} from "@/components/connection-card";
import type { Settings } from "@/lib/types";

const { putSettings, testConnection, getCuratorModels } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
  testConnection: vi.fn(() => Promise.resolve({ ok: true, message: "ok" })),
  getCuratorModels: vi.fn(() =>
    Promise.resolve({ provider: "anthropic", models: [] as string[] }),
  ),
}));

vi.mock("@/lib/api", () => {
  class ApiError extends Error {}
  return {
    ApiError,
    api: {
      putSettings: (values: Settings) => putSettings(values),
      testConnection: () => testConnection(),
      getCuratorModels: () => getCuratorModels(),
    },
  };
});

function renderCard(settings: Settings, fields: ConnectionField[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConnectionCard
        service="tmdb"
        title="TMDB"
        purpose="Finds similar titles."
        glyph={<span>logo</span>}
        settings={settings}
        summary={settings["tmdb.apikey"] ? "API key saved" : ""}
        fields={fields}
      />
    </QueryClientProvider>,
  );
}

/** The AI curator card, whose "model" field drives the datalist model fetch. */
function renderCuratorCard(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ConnectionCard
        service="llm"
        title="AI curator"
        purpose="Picks each row's titles."
        glyph={<span>logo</span>}
        settings={settings}
        summary="Claude"
        fields={[
          {
            key: "curator.provider",
            label: "Provider",
            kind: "select",
            options: [
              { value: "anthropic", label: "Claude" },
              { value: "none", label: "None" },
            ],
          },
          {
            key: "curator.model",
            label: "Model",
            kind: "model",
            showIf: (v) => v["curator.provider"] !== "none",
          },
        ]}
      />
    </QueryClientProvider>,
  );
}

describe("ConnectionCard", () => {
  beforeEach(() => {
    putSettings.mockClear();
    testConnection.mockClear();
    getCuratorModels.mockClear();
  });

  it("auto-tests a configured connection on mount so its status shows without a click", async () => {
    renderCard({ "tmdb.apikey": "•••••" }, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await waitFor(() => expect(testConnection).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Connection OK")).toBeInTheDocument();
  });

  it("does not auto-test a connection that isn't set up", async () => {
    renderCard({}, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await new Promise((r) => setTimeout(r, 0));
    expect(testConnection).not.toHaveBeenCalled();
  });

  it("offers 'Set up' when nothing is configured, and saves the typed value", async () => {
    renderCard({}, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await userEvent.click(screen.getByRole("button", { name: /Set up/i }));
    await userEvent.type(screen.getByLabelText("API key"), "abc123");
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    expect(putSettings).toHaveBeenCalledTimes(1);
    expect(putSettings.mock.calls[0]?.[0]).toEqual({ "tmdb.apikey": "abc123" });
  });

  it("skips an unchanged redacted secret so a save doesn't overwrite it", async () => {
    renderCard({ "tmdb.apikey": "•••••" }, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));
    // Leave the redacted placeholder untouched and save.
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    // The redacted sentinel is dropped from the payload → the server keeps the existing key.
    expect(putSettings.mock.calls[0]?.[0]).toEqual({});
  });

  it("does not wipe a saved secret when the field is focused but not retyped", async () => {
    // Regression: focusing a password clears its dots; saving without typing must be a no-op, never
    // an empty-string overwrite of the live secret.
    renderCard({ "tmdb.apikey": "•••••" }, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));
    await userEvent.click(screen.getByLabelText("API key")); // focus → clears the placeholder
    await userEvent.tab(); // blur without typing
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    expect(putSettings.mock.calls[0]?.[0]).toEqual({}); // the secret key is left untouched
  });

  it("shows a provider-specific 'Get a key' link that follows the selected option", async () => {
    // Mirrors the AI curator card: the key field's helpUrl is a function of the current values, so
    // the link points at whichever provider is picked.
    renderCard({}, [
      {
        key: "provider",
        label: "Provider",
        kind: "select",
        options: [
          { value: "anthropic", label: "Claude" },
          { value: "openai", label: "OpenAI" },
        ],
      },
      {
        key: "api_key",
        label: "API key",
        kind: "password",
        helpUrl: (v) =>
          v.provider === "openai"
            ? "https://platform.openai.com/api-keys"
            : "https://console.anthropic.com/settings/keys",
      },
    ]);
    await userEvent.click(screen.getByRole("button", { name: /Set up/i }));

    // Defaults to the first provider's key page…
    expect(screen.getByRole("link", { name: /get a key/i })).toHaveAttribute(
      "href",
      "https://console.anthropic.com/settings/keys",
    );

    // …and follows the switch to the other provider.
    await userEvent.click(screen.getByRole("button", { name: "OpenAI" }));
    expect(screen.getByRole("link", { name: /get a key/i })).toHaveAttribute(
      "href",
      "https://platform.openai.com/api-keys",
    );
  });

  it("lists the provider's models in a dropdown yet still saves a typed override", async () => {
    // The AI curator "model" field: a datalist gives the provider's known models as suggestions,
    // but it's a free-text input so any id the list doesn't offer still saves.
    getCuratorModels.mockResolvedValue({
      provider: "anthropic",
      models: ["claude-haiku-4-5", "claude-sonnet-5"],
    });
    const { container } = renderCuratorCard({
      "curator.provider": "anthropic",
      "curator.api_key": "•••••",
    });

    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));

    // The provider's models populate the datalist (the dropdown suggestions).
    await waitFor(() =>
      expect(container.querySelectorAll("datalist option")).toHaveLength(2),
    );
    expect(
      Array.from(container.querySelectorAll("datalist option")).map(
        (o) => (o as HTMLOptionElement).value,
      ),
    ).toEqual(["claude-haiku-4-5", "claude-sonnet-5"]);

    // A custom id the list never offered still saves — the override the user asked for.
    await userEvent.type(screen.getByLabelText("Model"), "claude-opus-4-8");
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    expect(putSettings.mock.calls[0]?.[0]).toMatchObject({
      "curator.model": "claude-opus-4-8",
    });
  });

  it("does not fetch curator models for a card without a model field", async () => {
    // Every connection card mounts ConnectionCard; only the AI curator has a model field. The
    // models query must stay dormant for the others (no wasted request, no key probe).
    renderCard({ "tmdb.apikey": "•••••" }, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));
    await new Promise((r) => setTimeout(r, 0));
    expect(getCuratorModels).not.toHaveBeenCalled();
  });

  it("does not fetch curator models when the provider is 'none'", async () => {
    // 'none' is heuristic mode — no provider to list models from. The `provider !== "none"` clause
    // is load-bearing ("none" is a truthy string), so guard against a regression that drops it.
    renderCuratorCard({
      "curator.provider": "none",
      "curator.api_key": "•••••",
    });
    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));
    await new Promise((r) => setTimeout(r, 0));
    expect(getCuratorModels).not.toHaveBeenCalled();
  });

  it("does not fetch curator models until a key is saved", async () => {
    // Mirrors the setup wizard: the endpoint reads the saved key server-side, so listing before one
    // is on file just wastes a request the server can't answer. Free-text entry still works.
    renderCuratorCard({ "curator.provider": "anthropic" });
    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));
    await new Promise((r) => setTimeout(r, 0));
    expect(getCuratorModels).not.toHaveBeenCalled();
  });

  it("clears a configured connection", async () => {
    renderCard({ "tmdb.apikey": "•••••" }, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));
    await userEvent.click(screen.getByRole("button", { name: /Clear/i }));

    expect(putSettings.mock.calls[0]?.[0]).toEqual({ "tmdb.apikey": "" });
  });
});
