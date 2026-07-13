import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ConnectionCard,
  type ConnectionField,
} from "@/components/connection-card";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", () => {
  class ApiError extends Error {}
  return {
    ApiError,
    api: {
      putSettings: (values: Settings) => putSettings(values),
      testConnection: () => Promise.resolve({ ok: true, message: "ok" }),
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

describe("ConnectionCard", () => {
  beforeEach(() => putSettings.mockClear());

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

  it("clears a configured connection", async () => {
    renderCard({ "tmdb.apikey": "•••••" }, [
      { key: "tmdb.apikey", label: "API key", kind: "password" },
    ]);
    await userEvent.click(screen.getByRole("button", { name: /Edit/i }));
    await userEvent.click(screen.getByRole("button", { name: /Clear/i }));

    expect(putSettings.mock.calls[0]?.[0]).toEqual({ "tmdb.apikey": "" });
  });
});
