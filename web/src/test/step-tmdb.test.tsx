import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { StepTmdb } from "@/pages/setup/step-tmdb";

const { getSettings, putSettings, testConnection } = vi.hoisted(() => ({
  getSettings: vi.fn(),
  putSettings: vi.fn((_values: Record<string, unknown>) => Promise.resolve({})),
  testConnection: vi.fn((_service: string) =>
    Promise.resolve({ ok: true, message: "Connected" }),
  ),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    getSettings: () => getSettings(),
    putSettings: (values: Record<string, unknown>) => putSettings(values),
    testConnection: (service: string) => testConnection(service),
  },
}));

const SAVED = {
  "tautulli.url": "http://taut:8181",
  "tautulli.apikey": "•••••", // secrets come back redacted from GET /api/settings
  "tmdb.apikey": "•••••",
};

function renderStep(
  data: Record<string, unknown> = {},
  update: (patch: Record<string, unknown>) => void = vi.fn(),
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <StepTmdb
        data={data}
        update={update}
        next={vi.fn()}
        complete={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

describe("StepTmdb settings persistence", () => {
  beforeEach(() => {
    getSettings.mockReset();
    putSettings.mockClear();
  });

  it("seeds the fields from saved settings so they survive Back/Next (key shown masked)", async () => {
    getSettings.mockResolvedValue(SAVED);
    renderStep();
    await screen.findByDisplayValue("http://taut:8181"); // wait for the async seed to land
    expect(screen.getByLabelText("Tautulli URL")).toHaveValue(
      "http://taut:8181",
    );
    expect(screen.getByLabelText("Tautulli API key")).toHaveValue("•••••");
    expect(
      screen.getByLabelText("TMDB API key"),
    ).toHaveValue("•••••");
  });

  it("does not clobber typing when nothing is saved yet", async () => {
    getSettings.mockResolvedValue({});
    renderStep();
    const key = await screen.findByLabelText("Tautulli API key");
    expect(key).toHaveValue("");
    await userEvent.type(key, "mysecret");
    expect(key).toHaveValue("mysecret");
  });

  it("clears the TMDB works-flag when the tested key is invalid, so Next stays blocked", async () => {
    getSettings.mockResolvedValue({});
    testConnection.mockResolvedValueOnce({
      ok: false,
      message: "TMDB rejected the key",
    });
    const update = vi.fn();
    renderStep({}, update);
    await userEvent.type(
      await screen.findByLabelText(
        "TMDB API key",
      ),
      "wrong-key",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Test key" }),
    );
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ tmdb_set: false }),
    );
  });

  it("un-verifies a previously-valid TMDB key the moment it's edited", async () => {
    getSettings.mockResolvedValue({});
    const update = vi.fn();
    renderStep({ tmdb_set: true }, update); // already validated on a prior visit
    await userEvent.type(
      await screen.findByLabelText(
        "TMDB API key",
      ),
      "x",
    );
    expect(update).toHaveBeenCalledWith({ tmdb_set: false });
  });

  it("re-sends the redacted mask unchanged on save (backend treats it as no-change)", async () => {
    getSettings.mockResolvedValue(SAVED);
    renderStep();
    await screen.findByDisplayValue("http://taut:8181"); // wait for the seed to land
    await userEvent.click(screen.getByRole("button", { name: "Save & test" }));
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith({
        "tautulli.url": "http://taut:8181",
        "tautulli.apikey": "•••••",
      }),
    );
  });
});

describe("StepTmdb TMDB walkthrough", () => {
  it("links to TMDB signup and offers no Tautulli skip button", async () => {
    getSettings.mockResolvedValue({});
    renderStep();
    const link = await screen.findByRole("link", { name: /themoviedb\.org\/signup/ });
    expect(link).toHaveAttribute("href", "https://www.themoviedb.org/signup");
    expect(screen.getByText(/not the long Read Access Token/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Skip/ })).not.toBeInTheDocument();
  });
});
