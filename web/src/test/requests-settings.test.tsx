import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RequestsSettings } from "@/components/requests-settings";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return {
    ApiError,
    apiErrorMessage: (error: unknown, fallback: string) =>
      error instanceof ApiError ? error.message : fallback,
    api: {
      putSettings: (values: Settings) => putSettings(values),
      testConnection: () => Promise.resolve({ ok: true, message: "Connected" }),
      getArrOptions: () =>
        Promise.resolve({ quality_profiles: [], root_folders: [] }),
    },
  };
});

/** Requests on, judging by IMDb, with an MDBList key already saved (the key lives in Connections now). */
const WITH_SAVED_MDBLIST_KEY: Settings = {
  "requests.enabled": true,
  "requests.rating_source": "imdb",
  "requests.mdblist.apikey": "•••••", // a saved secret always reads back redacted
};

function renderPanel(settings: Settings = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RequestsSettings settings={settings} />
    </QueryClientProvider>,
  );
}

describe("RequestsSettings", () => {
  beforeEach(() => putSettings.mockClear());

  it("keeps the config hidden until requests are turned on", async () => {
    renderPanel();
    // The explainer is always shown; the app config only appears once enabled.
    expect(screen.getByText(/Fill in the gaps automatically/i)).toBeTruthy();
    expect(screen.queryByText("Radarr")).toBeNull();

    await userEvent.click(
      screen.getByLabelText(/Turn automatic requests on or off/i),
    );

    expect(screen.getByText("Radarr")).toBeTruthy();
    expect(screen.getByText("Sonarr")).toBeTruthy();
    // The legend, exactly — the auto-send copy now says "guardrails" too, so a loose regex
    // matches three nodes.
    expect(screen.getByText("Guardrails")).toBeTruthy();
  });

  it("points to Connections when neither app is connected", async () => {
    renderPanel();
    await userEvent.click(
      screen.getByLabelText(/Turn automatic requests on or off/i),
    );
    // The connection (address + key) lives in Connections now; blank settings show the prompt
    // and a way to get there rather than profile/folder dropdowns.
    expect(
      screen.getByText(/Connect Radarr or Sonarr to start requesting/i),
    ).toBeTruthy();
    expect(
      screen.getAllByRole("button", { name: /Go to Connections/i }).length,
    ).toBeGreaterThan(0);
  });

  it("auto-saves the enabled flag and thresholds (no Save button)", async () => {
    renderPanel({
      "requests.min_rating": 7,
      "requests.min_votes": 100,
      "requests.max_per_run": 5,
    });
    await userEvent.click(
      screen.getByLabelText(/Turn automatic requests on or off/i),
    );
    // No Save button — flipping the toggle persists on its own (debounced).
    expect(screen.queryByRole("button", { name: /Save requests/i })).toBeNull();
    await waitFor(() => expect(putSettings).toHaveBeenCalled());

    const payload = putSettings.mock.calls.at(-1)?.[0] ?? {};
    expect(payload["requests.enabled"]).toBe(true);
    expect(payload["requests.min_rating"]).toBe(7);
    expect(payload["requests.max_per_run"]).toBe(5);
    // The connection is owned by Connections now — saving Requests must NEVER emit the URL/key,
    // or a stale/empty form value would silently wipe the API key saved there.
    expect(payload).not.toHaveProperty("requests.radarr.apikey");
    expect(payload).not.toHaveProperty("requests.radarr.url");
    expect(payload).not.toHaveProperty("requests.sonarr.apikey");
    expect(payload).not.toHaveProperty("requests.sonarr.url");
  });

  it("saves an upper year bound and warns when the range can match nothing", async () => {
    renderPanel({ "requests.enabled": true });

    const before = await screen.findByLabelText(/Released on or before/i);
    await userEvent.clear(before);
    await userEvent.type(before, "1990");
    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toHaveProperty(
        "requests.max_year",
        1990,
      ),
    );

    // An upper bound earlier than the lower bound can match nothing — the form says so.
    const after = screen.getByLabelText(/Released on or after/i);
    await userEvent.type(after, "2010");
    expect(
      await screen.findByText(/no titles can\s+match this range/i),
    ).toBeTruthy();
  });

  it("warns and points to Connections when a non-TMDB source has no MDBList key", async () => {
    // rating_source=imdb but no key on file: the key now lives in Connections, so the panel must
    // warn that the choice won't take effect and route the owner there — never save a key itself.
    renderPanel({ "requests.enabled": true, "requests.rating_source": "imdb" });

    expect(await screen.findByText(/MDBList isn.t connected/i)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Set up MDBList in Connections/i }),
    ).toBeTruthy();
    // The key field is gone from Requests entirely — no way to type a secret here anymore.
    expect(screen.queryByLabelText(/MDBList API key/i)).toBeNull();
    for (const [payload] of putSettings.mock.calls) {
      expect(payload).not.toHaveProperty("requests.mdblist.apikey");
    }
  });

  it("shows the MDBList connection is in use when its key is saved", async () => {
    renderPanel(WITH_SAVED_MDBLIST_KEY);
    // Connected: a plain confirmation pointing to Connections, and no warning.
    expect(
      await screen.findByText(/Using your MDBList connection/i),
    ).toBeTruthy();
    expect(screen.queryByText(/MDBList isn.t connected/i)).toBeNull();
  });

  it("shows no MDBList messaging at all when judging by TMDB", async () => {
    // TMDB needs no external ratings service, so neither the connected note nor the warning belongs
    // here — a regression dropping the `!== "tmdb"` guard would wrongly show one of them.
    renderPanel({ "requests.enabled": true, "requests.rating_source": "tmdb" });
    expect(await screen.findByText("Guardrails")).toBeTruthy();
    expect(screen.queryByText(/Using your MDBList connection/i)).toBeNull();
    expect(screen.queryByText(/MDBList isn.t connected/i)).toBeNull();
  });

  it("hides the connect prompt and shows the filing pickers once an app is connected", async () => {
    renderPanel({
      "requests.radarr.url": "http://radarr",
      "requests.radarr.apikey": "•••••", // a saved key comes back redacted -> "connected"
    });
    await userEvent.click(
      screen.getByLabelText(/Turn automatic requests on or off/i),
    );
    // Radarr is connected, so the top "connect first" callout is gone and its filing pickers render.
    expect(
      screen.queryByText(/Connect Radarr or Sonarr to start requesting/i),
    ).toBeNull();
    expect(await screen.findByText("Quality")).toBeTruthy();
    expect(screen.getByText("Save to")).toBeTruthy();
  });

  it("teaches the auto-send choice before the guardrails it sits on top of", async () => {
    renderPanel({ "requests.enabled": true });
    const autoSend = await screen.findByText(
      "Send on its own, or ask me first",
    );
    const guardrails = screen.getByText("Guardrails");
    // DOCUMENT_POSITION_FOLLOWING (4): guardrails come AFTER the auto-send fieldset. Read the other
    // way round, "Minimum rating 7" looked like the bar for requesting at all.
    expect(
      autoSend.compareDocumentPosition(guardrails) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("keeps the per-run cap with automatic sending, since that is all it caps", async () => {
    // `max_per_run` is only ever checked after the auto-send bars (`request_missing`), so with
    // automatic sending off it can never be reached — and must not read as a limit on the inbox.
    renderPanel({ "requests.enabled": true, "requests.auto_send": false });
    expect(await screen.findByText("Guardrails")).toBeTruthy();
    expect(
      screen.queryByText(/Most to send automatically in one run/i),
    ).toBeNull();

    await userEvent.click(
      screen.getByLabelText(/Send the strongest titles without asking/i),
    );
    expect(
      screen.getByText(/Most to send automatically in one run/i),
    ).toBeTruthy();
  });
});
