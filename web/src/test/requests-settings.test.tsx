import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RequestsSettings } from "@/components/requests-settings";
import type { Settings } from "@/lib/types";

const { putSettings, getSeerrOptions } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
  getSeerrOptions: vi.fn(() =>
    Promise.resolve({
      // An admin that approves instantly and a service account that does not — the two ends of the
      // choice this screen exists to make legible.
      users: [
        {
          id: 1,
          name: "serverowner",
          auto_approve_movies: true,
          auto_approve_tv: true,
          is_plex_user: true,
        },
        {
          id: 4,
          name: "Shortlist",
          auto_approve_movies: false,
          auto_approve_tv: false,
          is_plex_user: false,
        },
        {
          id: 7,
          name: "MooHouse",
          auto_approve_movies: true,
          auto_approve_tv: false,
          is_plex_user: true,
        },
      ],
      default_user_id: 1,
    }),
  ),
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
      getSeerrOptions: () => getSeerrOptions(),
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
  beforeEach(() => {
    putSettings.mockClear();
    // Reset per test: one describe below points this at a rejection, and a leaked failure would
    // make every later account-list assertion pass for the wrong reason.
    getSeerrOptions.mockReset();
    getSeerrOptions.mockResolvedValue({
      users: [
        {
          id: 1,
          name: "serverowner",
          auto_approve_movies: true,
          auto_approve_tv: true,
          is_plex_user: true,
        },
        {
          id: 4,
          name: "Shortlist",
          auto_approve_movies: false,
          auto_approve_tv: false,
          is_plex_user: false,
        },
        {
          id: 7,
          name: "MooHouse",
          auto_approve_movies: true,
          auto_approve_tv: false,
          is_plex_user: true,
        },
      ],
      default_user_id: 1,
    });
  });

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

  it("saves the tag-by-person switch, off unless the owner turns it on", async () => {
    renderPanel({ "requests.enabled": true });
    const toggle = await screen.findByLabelText(
      /Also tag requests with the name of the person/i,
    );
    expect(toggle).not.toBeChecked();

    await userEvent.click(toggle);
    await waitFor(() =>
      expect(putSettings.mock.calls.at(-1)?.[0]).toMatchObject({
        "requests.auto_user_tag": true,
      }),
    );
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

  it("offers the amount-of-a-show choice for Sonarr and saves it", async () => {
    // Issue #100: every show used to arrive with all seasons monitored, so a twelve-season show
    // started twelve seasons of downloads the night it was picked.
    renderPanel({
      "requests.enabled": true,
      "requests.sonarr.url": "http://sonarr",
      "requests.sonarr.apikey": "•••••",
    });

    const monitor = await screen.findByLabelText(/How much of a show to grab/i);
    expect(monitor).toHaveValue("all");
    await userEvent.selectOptions(monitor, "firstSeason");

    expect(screen.getByText(/Season 1 only/)).toBeTruthy();
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith(
        expect.objectContaining({ "requests.sonarr.monitor": "firstSeason" }),
      ),
    );
  });

  it("does not offer it for Radarr, which has no seasons to limit", async () => {
    renderPanel({
      "requests.enabled": true,
      "requests.radarr.url": "http://radarr",
      "requests.radarr.apikey": "•••••",
    });
    await screen.findByText("Quality");
    expect(screen.queryByLabelText(/How much of a show to grab/i)).toBeNull();
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

  describe("language", () => {
    const ON = { "requests.enabled": true };

    it("ships on 'Any language', so an upgrade changes nobody's requests", async () => {
      renderPanel(ON);
      await screen.findByText("Guardrails");
      expect(
        screen.getByRole("button", { name: "Any language", pressed: true }),
      ).toBeTruthy();
    });

    it("hides the language list and the second bar while the mode is 'Any'", async () => {
      // On "any" neither is read at all, and a number on screen that nothing applies is worse than
      // no number: it reads as a bar that is in force.
      renderPanel(ON);
      await screen.findByText("Guardrails");
      expect(screen.queryByLabelText("Add a language")).toBeNull();
      expect(
        screen.queryByLabelText(/Minimum TMDB rating, other languages/i),
      ).toBeNull();
    });

    it("shows the derived bar rather than an empty box when nothing is set", async () => {
      // min_rating 7 + 1.5 = 8.5. The field must show the number the run will use — the setting's
      // whole point is that it follows the owner's own floor rather than a constant of ours.
      renderPanel({ ...ON, "requests.language_mode": "prefer" });
      const bar = await screen.findByLabelText(
        /Minimum TMDB rating, other languages/i,
      );
      expect((bar as HTMLInputElement).value).toBe("8.5");
      expect(
        screen.getByText(/Following your minimum rating, plus 1.5/i),
      ).toBeTruthy();
    });

    it("moves the derived bar when the owner's own floor moves", async () => {
      renderPanel({
        ...ON,
        "requests.language_mode": "prefer",
        "requests.min_rating": 6,
      });
      const bar = await screen.findByLabelText(
        /Minimum TMDB rating, other languages/i,
      );
      expect((bar as HTMLInputElement).value).toBe("7.5");
    });

    it("saves null for the bar until the owner types one", async () => {
      // Turning the mode on must NOT write a number. Saving the derived 8.5 here would freeze the
      // bar at today's minimum rating, so later raising the minimum would silently stop moving it —
      // the setting would read as "following" while doing nothing of the sort.
      renderPanel(ON);
      await userEvent.click(
        await screen.findByRole("button", { name: "Prefer these" }),
      );
      await waitFor(() => {
        const saved = putSettings.mock.calls.at(-1)![0];
        expect(saved["requests.language_mode"]).toBe("prefer");
        expect(saved["requests.min_rating_other"]).toBeNull();
      });
    });

    it("clearing the bar un-pins it rather than storing a zero", async () => {
      // `Number("") === 0`, and 0 is a REAL bar here — nothing can fail it. So a naive read of the
      // cleared field would silently turn "Prefer these" into "Any language" for auto-send, which
      // is the one thing this whole feature is built to distinguish.
      renderPanel({
        ...ON,
        "requests.language_mode": "prefer",
        "requests.min_rating_other": 9.1,
      });
      const bar = await screen.findByLabelText(
        /Minimum TMDB rating, other languages/i,
      );
      await userEvent.clear(bar);
      await waitFor(() => {
        const saved = putSettings.mock.calls.at(-1)![0];
        expect(saved["requests.min_rating_other"]).toBeNull();
      });
    });

    it("hides the second bar in 'Only these', where no rating can rescue a title", async () => {
      renderPanel({ ...ON, "requests.language_mode": "only" });
      await screen.findByText("Guardrails");
      expect(
        screen.queryByLabelText(/Minimum TMDB rating, other languages/i),
      ).toBeNull();
      expect(screen.getByLabelText("Add a language")).toBeTruthy();
    });

    it("warns that an empty list in 'Only these' requests nothing at all", async () => {
      // The inverse of the control, on a path that adds titles to Radarr — so it says so on screen
      // rather than leaving the owner to discover a silent night.
      renderPanel({
        ...ON,
        "requests.language_mode": "only",
        "requests.preferred_languages": [],
      });
      expect(
        await screen.findByText(/will never ask for anything/i),
      ).toBeTruthy();
    });

    it("warns differently in 'Prefer these', where an empty list raises the bar on everything", async () => {
      renderPanel({
        ...ON,
        "requests.language_mode": "prefer",
        "requests.preferred_languages": [],
      });
      expect(
        await screen.findByText(/can identify a language for counts as another language/i),
      ).toBeTruthy();
      expect(screen.queryByText(/never ask for anything/i)).toBeNull();
    });

    it("keeps the language picker to its own width, not the whole panel", async () => {
      // The bug this pins: `selectClass` hardcodes `w-full`, and appending `w-auto` does not undo it
      // — both utilities sit in the same Tailwind layer, so the winner is whichever comes later in
      // the generated stylesheet, not in the attribute. A two-letter choice spanned the whole panel.
      renderPanel({ ...ON, "requests.language_mode": "prefer" });
      const picker = await screen.findByLabelText("Add a language");
      expect(picker.className).not.toMatch(/\bw-full\b/);
      expect(picker.className).toMatch(/\bw-auto\b/);
    });

    it("adds a language and saves it", async () => {
      renderPanel({ ...ON, "requests.language_mode": "prefer" });
      await userEvent.selectOptions(
        await screen.findByLabelText("Add a language"),
        "ja",
      );
      await waitFor(() => {
        const saved = putSettings.mock.calls.at(-1)![0];
        expect(saved["requests.preferred_languages"]).toEqual(["en", "ja"]);
      });
    });

    it("says a bar below the minimum rating can never apply", async () => {
      renderPanel({
        ...ON,
        "requests.language_mode": "prefer",
        "requests.min_rating": 8,
        "requests.min_rating_other": 7,
      });
      expect(
        await screen.findByText(/it never applies/i),
      ).toBeTruthy();
    });
  });

  describe("choosing where requests go", () => {
    /** Requests on, routed through a CONNECTED Overseerr (a saved key reads back redacted). */
    const VIA_SEERR: Settings = {
      "requests.enabled": true,
      "requests.target": "overseerr",
      "requests.overseerr.url": "http://overseerr.test",
      "requests.overseerr.apikey": "•••••",
    };

    it("shows Radarr and Sonarr by default", async () => {
      renderPanel({ "requests.enabled": true });
      expect(await screen.findByText("Radarr")).toBeTruthy();
      expect(screen.getByText("Sonarr")).toBeTruthy();
      expect(screen.queryByLabelText("Request as")).toBeNull();
    });

    it("swaps the two Arr cards for one Overseerr card", async () => {
      renderPanel(VIA_SEERR);
      expect(await screen.findByLabelText("Request as")).toBeTruthy();
      expect(screen.queryByText("Radarr")).toBeNull();
      expect(screen.queryByText("Sonarr")).toBeNull();
    });

    it("hides both tag controls, which Overseerr's API cannot carry", async () => {
      // POST /request has no tags field at all, so leaving these on screen would offer a setting
      // that silently does nothing.
      renderPanel(VIA_SEERR);
      expect(await screen.findByLabelText("Request as")).toBeTruthy();
      expect(screen.queryByLabelText("Tag added items")).toBeNull();
      expect(screen.queryByLabelText("Also tag by person")).toBeNull();
    });

    it("keeps the guardrails on both routes — they are Shortlist's, not the app's", async () => {
      renderPanel(VIA_SEERR);
      expect(await screen.findByText("Guardrails")).toBeTruthy();
    });

    it("offers the way to a review queue, without restating what the dropdown said", async () => {
      renderPanel(VIA_SEERR);
      const picker = (await screen.findByLabelText(
        "Request as",
      )) as HTMLSelectElement;
      // Wait for the fetched list FIRST. The label exists on first paint, so asserting off
      // `findByLabelText` alone reads the screen before it knows which account it is describing —
      // the same early-read trap in its presence form.
      await screen.findByRole("option", { name: /Shortlist — requests wait/ });

      expect(
        screen.getByText(/Want to check them in Overseerr/),
      ).toBeTruthy();
      // NOT "they'll go straight to Radarr/Sonarr" — whether an approved request reaches a
      // download app is Overseerr's own setup, not something this screen can promise.
      expect(screen.queryByText(/straight to Radarr\/Sonarr/i)).toBeNull();
      await userEvent.selectOptions(picker, "4");
      await waitFor(() => {
        const saved = putSettings.mock.calls.at(-1)![0];
        expect(saved["requests.overseerr.request_as_user_id"]).toBe(4);
      });
    });

    it("saves the target when it is switched", async () => {
      renderPanel({ "requests.enabled": true });
      await userEvent.click(
        await screen.findByRole("button", { name: "Overseerr / Jellyseerr" }),
      );
      await waitFor(() => {
        const saved = putSettings.mock.calls.at(-1)![0];
        expect(saved["requests.target"]).toBe("overseerr");
      });
    });

    it("nudges to Connections when Overseerr is chosen but not connected", async () => {
      renderPanel({ "requests.enabled": true, "requests.target": "overseerr" });
      expect(
        await screen.findByText("Connect Overseerr to start requesting"),
      ).toBeTruthy();
    });
  });

  describe("when the saved account cannot be listed", () => {
    const VIA_BROKEN_SEERR: Settings = {
      "requests.enabled": true,
      "requests.target": "overseerr",
      "requests.overseerr.url": "http://overseerr.test",
      "requests.overseerr.apikey": "•••••",
      "requests.overseerr.request_as_user_id": 4,
    };

    beforeEach(() => {
      getSeerrOptions.mockRejectedValue(new Error("unreachable"));
    });

    it("keeps the picker usable rather than hiding it behind the error", async () => {
      // Hiding it left an owner whose instance was briefly down unable to change the setting at
      // all — including putting it back to the server default.
      renderPanel(VIA_BROKEN_SEERR);
      expect(await screen.findByText(/Couldn.t reach Overseerr/i)).toBeTruthy();
      expect(screen.getByLabelText("Request as")).toBeTruthy();
    });

    it("names a saved account that Overseerr no longer lists", async () => {
      // Same trap as the unreachable case, reached a different way: the account was deleted in
      // Overseerr, so the list loads FINE and simply lacks it. Keying the fallback on the error
      // flag would have covered only half of this.
      getSeerrOptions.mockResolvedValue({
        users: [
          {
            id: 1,
            name: "serverowner",
            auto_approve_movies: true,
            auto_approve_tv: true,
            is_plex_user: true,
          },
        ],
        default_user_id: 1,
      });
      renderPanel(VIA_BROKEN_SEERR);
      expect(
        await screen.findByRole("option", { name: /Account #4/ }),
      ).toBeTruthy();
      expect(
        (screen.getByLabelText("Request as") as HTMLSelectElement).value,
      ).toBe("4");
    });

    it("still shows the saved account rather than silently reading as the default", async () => {
      // The select would otherwise fall back to its first option, misreport the saved value, and
      // then have autosave WRITE that back.
      renderPanel(VIA_BROKEN_SEERR);
      // Wait for the FAILURE to land, not merely for the label: while the fetch is still pending
      // the fallback option does not exist yet and the select genuinely does read "0". Asserting
      // before then measures the loading state and calls it the bug.
      expect(
        await screen.findByRole("option", { name: /Account #4/ }),
      ).toBeTruthy();
      const picker = screen.getByLabelText("Request as") as HTMLSelectElement;
      expect(picker.value).toBe("4");
    });
  });

  describe("saying what will actually happen", () => {
    const VIA_SEERR: Settings = {
      "requests.enabled": true,
      "requests.target": "overseerr",
      "requests.overseerr.url": "http://overseerr.test",
      "requests.overseerr.apikey": "\u2022\u2022\u2022\u2022\u2022",
    };

    it("says each account's effect in the dropdown, not just its name", async () => {
      // The difference between "filed for you to look at" and "already downloading" is a property
      // of the account, so it belongs where the account is chosen.
      renderPanel(VIA_SEERR);
      expect(
        await screen.findByRole("option", {
          name: /Shortlist — requests wait for approval/,
        }),
      ).toBeTruthy();
      // The default account is already the first option; listing it AGAIN just asks "which of
      // these two identical lines did I want?".
      expect(
        screen.getAllByRole("option", { name: /serverowner/ }),
      ).toHaveLength(1);
    });

    it("resolves Server default to the account the API key actually is", async () => {
      // The commonest setting. Without `default_user_id` it would be an unknown, and the summary
      // would go vague on the very path most people are on.
      renderPanel(VIA_SEERR);
      expect(
        await screen.findByText(
          /approved there automatically, so they start downloading/,
        ),
      ).toBeTruthy();
    });

    it("changes what it says when a non-approving account is picked", async () => {
      renderPanel(VIA_SEERR);
      const picker = (await screen.findByLabelText(
        "Request as",
      )) as HTMLSelectElement;
      await screen.findByRole("option", { name: /Shortlist — requests wait/ });
      await userEvent.selectOptions(picker, "4");
      expect(
        await screen.findByText(/filed in Overseerr for you to approve there/),
      ).toBeTruthy();
    });

    it("names the double-approval trap and says how to escape it", async () => {
      // Auto-send off AND an account that cannot approve = every title approved twice. Legitimate,
      // almost never deliberate, and previously silent.
      renderPanel({
        ...VIA_SEERR,
        "requests.auto_send": false,
        "requests.overseerr.request_as_user_id": 4,
      });
      expect(await screen.findByText(/approve each title twice/)).toBeTruthy();
      expect(screen.getByText(/decide in only one place/)).toBeTruthy();
    });

    it("stays quiet about the trap when there is only one gate", async () => {
      renderPanel({ ...VIA_SEERR, "requests.auto_send": false });
      // Anchored on something positive first: an absence assertion that renders before the account
      // list resolves would pass against a broken screen.
      expect(
        await screen.findByText(
          /Nothing reaches Overseerr until you approve it/,
        ),
      ).toBeTruthy();
      expect(screen.queryByText(/approve each title twice/)).toBeNull();
    });

    it("spells out the setup that used to be undiscoverable", async () => {
      // Bars at the guardrails: Shortlist stops deciding and Overseerr becomes the only gate. You
      // could always do this; nothing ever said so.
      renderPanel({
        ...VIA_SEERR,
        "requests.min_demand": 2,
        "requests.min_rating": 7,
        "requests.auto_min_demand": 2,
        "requests.auto_min_rating": 7,
      });
      // Not "Nothing waits here" — max_per_run still queues the overflow, whatever the bars say.
      expect(await screen.findByText(/up to 5 a night/)).toBeTruthy();
    });

    it("still describes the Arr route in its own terms", async () => {
      renderPanel({ "requests.enabled": true });
      const summary = await screen.findByText(
        /go to Radarr or Sonarr as soon as a run finds them/,
      );
      // Scoped to the sentence, not the page: "Overseerr" legitimately appears elsewhere on the
      // Arr route — it is the other half of the chooser.
      expect(summary.textContent).not.toMatch(/Overseerr/);
    });
  });

  describe("choosing whose name requests go out under", () => {
    const VIA_SEERR: Settings = {
      "requests.enabled": true,
      "requests.target": "overseerr",
      "requests.overseerr.url": "http://overseerr.test",
      "requests.overseerr.apikey": "\u2022\u2022\u2022\u2022\u2022",
    };

    it("offers real people, grouped after the accounts made for this", async () => {
      // They were hidden for a while, and that was wrong: on most instances every account that
      // does NOT auto-approve belongs to a person, so hiding them left owners with nothing to pick
      // and every title downloading immediately (reported on discussion #110).
      renderPanel(VIA_SEERR);
      expect(
        await screen.findByRole("option", { name: /MooHouse/ }),
      ).toBeTruthy();
      const groups = [...document.querySelectorAll("optgroup")].map(
        (g) => g.label,
      );
      expect(groups).toEqual([
        "Accounts made for this",
        "People on your server",
      ]);
    });

    it("says what picking a person costs THEM, at the moment it is picked", async () => {
      renderPanel({ ...VIA_SEERR, "requests.overseerr.request_as_user_id": 7 });
      const note = await screen.findByText(/count against their quota/);
      expect(note.textContent).toMatch(/MooHouse/);
    });

    it("says nothing of the sort for an account made for this", async () => {
      renderPanel({ ...VIA_SEERR, "requests.overseerr.request_as_user_id": 4 });
      expect(
        await screen.findByRole("option", { name: /Shortlist — requests wait/ }),
      ).toBeTruthy();
      expect(screen.queryByText(/count against their quota/)).toBeNull();
    });
  });
});
