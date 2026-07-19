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

/** Requests on, judging by IMDb, with a key already saved — the state the MDBList field is edited in. */
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
    expect(screen.getByText(/Guardrails/i)).toBeTruthy();
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

  it("never saves the redacted sentinel as the MDBList key", async () => {
    renderPanel(WITH_SAVED_MDBLIST_KEY);
    const field = screen.getByLabelText(/MDBList API key/i);
    expect(field).toHaveValue("•••••");

    // Clicking in clears the dots, so typing can't append to them ("•••••abc123" used to be saved
    // verbatim as the key).
    await userEvent.click(field);
    expect(field).toHaveValue("");
    await userEvent.type(field, "abc123");

    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toHaveProperty(
        "requests.mdblist.apikey",
        "abc123",
      ),
    );
    for (const [payload] of putSettings.mock.calls) {
      expect(payload["requests.mdblist.apikey"]).not.toBe("•••••");
    }
  });

  it("leaves a saved MDBList key alone when the field is focused but not retyped", async () => {
    renderPanel(WITH_SAVED_MDBLIST_KEY);
    const field = screen.getByLabelText(/MDBList API key/i);

    await userEvent.click(field); // focus blanks the dots…
    await userEvent.tab(); // …and leaving without typing must not wipe the key

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    for (const [payload] of putSettings.mock.calls) {
      expect(payload).not.toHaveProperty("requests.mdblist.apikey");
    }
    expect(field).toHaveValue("•••••");
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
});
