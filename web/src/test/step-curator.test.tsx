import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Settings } from "@/lib/types";
import type { CuratorProvider } from "@/lib/wizard";
import { StepCurator } from "@/pages/setup/step-curator";

const { getSettings, putSettings, testConnection, getCuratorModels } =
  vi.hoisted(() => ({
    getSettings: vi.fn(() => Promise.resolve({} as Settings)),
    putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
    testConnection: vi.fn((_service: string) =>
      Promise.resolve({ ok: true, message: "ok" }),
    ),
    getCuratorModels: vi.fn((_body: unknown) =>
      Promise.resolve({ provider: "openai_compatible", models: [] }),
    ),
  }));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, fallback: string) => fallback,
  api: {
    getSettings: () => getSettings(),
    putSettings: (v: Settings) => putSettings(v),
    testConnection: (service: string) => testConnection(service),
    getCuratorModels: (body: unknown) => getCuratorModels(body),
  },
}));

function renderStep(provider: CuratorProvider) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const update = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <StepCurator
        data={{ curator_provider: provider }}
        update={update}
        next={vi.fn()}
        complete={vi.fn()}
      />
    </QueryClientProvider>,
  );
  return { update };
}

describe("StepCurator", () => {
  beforeEach(() => {
    getSettings.mockClear();
    putSettings.mockClear();
    testConnection.mockClear();
  });

  it("offers an API key field for a local/OpenAI-compatible server", async () => {
    // Issue #88: the wizard hid the key entirely, so a hosted gateway that speaks the same API
    // (ollama.com cloud, OpenRouter) could not be configured here at all — and since step 3's Next
    // gate needs a PASSING test, its inevitable 401 stranded the owner on this step.
    renderStep("openai_compatible");

    const key = await screen.findByLabelText(/API key \(optional\)/i);
    expect(key).toHaveAttribute("type", "password");
    expect(screen.getByLabelText(/Server URL/i)).toBeInTheDocument();
  });

  it("saves the key a hosted gateway needs alongside the server URL", async () => {
    renderStep("openai_compatible");

    const url = await screen.findByLabelText(/Server URL/i);
    await userEvent.clear(url);
    await userEvent.type(url, "https://ollama.com");
    await userEvent.type(
      screen.getByLabelText(/API key \(optional\)/i),
      "sk-cloud-key",
    );
    await userEvent.click(screen.getByRole("button", { name: /Save & test/i }));

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(putSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        "curator.provider": "openai_compatible",
        "curator.openai_base_url": "https://ollama.com",
        "curator.api_key": "sk-cloud-key",
      }),
    );
  });

  it("still lets a keyless local server save with the key box left blank", async () => {
    renderStep("openai_compatible");

    // The gate is `needsKey`, NOT "the field is on screen" — a box nobody has to fill must not
    // disable the only button on the step for every Ollama user.
    const save = await screen.findByRole("button", { name: /Save & test/i });
    expect(save).toBeEnabled();
    await userEvent.click(save);

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(putSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        "curator.provider": "openai_compatible",
        "curator.api_key": "",
      }),
    );
  });

  it("keeps a required key required: Anthropic cannot save with an empty key", async () => {
    renderStep("anthropic");

    expect(
      await screen.findByRole("button", { name: /Save & test/i }),
    ).toBeDisabled();
    expect(screen.getByLabelText(/Claude API key/i)).toBeInTheDocument();
  });

  it("seeds the server URL from the setting this step actually saves", async () => {
    // The step writes `curator.openai_base_url` but used to re-read `curator.ollama_url`, so a
    // Back/Next round trip silently reset a saved URL to the localhost placeholder.
    getSettings.mockResolvedValueOnce({
      "curator.provider": "openai_compatible",
      "curator.openai_base_url": "http://gpu-box:8080/v1",
    } as Settings);
    renderStep("openai_compatible");

    await waitFor(() =>
      expect(screen.getByLabelText(/Server URL/i)).toHaveValue(
        "http://gpu-box:8080/v1",
      ),
    );
  });
});
