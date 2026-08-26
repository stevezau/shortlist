/**
 * A row's own Sonarr/Radarr settings in the row editor.
 *
 * The two behaviours worth pinning are both about what is NOT shown: a shared row can never surface
 * a missing title, so offering it these controls would be offering something silently ignored; and a
 * field that inherits must show the global rather than a value the row does not actually have.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  RowRequestSettings,
  type RowRequestInput,
} from "@/components/rows/row-request-settings";
import type { Settings } from "@/lib/types";

const INHERITS: RowRequestInput = {
  req_min_rating: null,
  req_min_demand: null,
  req_min_year: null,
  req_max_year: null,
  req_auto_send: null,
  req_auto_user_tag: null,
  req_max_per_row: null,
  req_radarr_root_folder: null,
  req_radarr_quality_profile_id: null,
  req_sonarr_root_folder: null,
  req_sonarr_quality_profile_id: null,
  req_sonarr_monitor: null,
  req_language_mode: null,
  req_preferred_languages: null,
  req_min_rating_other: null,
};

const SETTINGS = {
  "requests.enabled": true,
  "requests.min_rating": 7.3,
  "requests.min_demand": 2,
  "requests.max_per_run": 10,
  "requests.auto_send": true,
  "requests.radarr.root_folder": "/data/Movies",
  "requests.sonarr.monitor": "all",
  "requests.auto_user_tag": true,
  "requests.language_mode": "prefer",
  "requests.preferred_languages": ["en"],
  "requests.min_rating_other": null,
} as unknown as Settings;

function renderSection(
  input: Partial<RowRequestInput> = {},
  { requestsEnabled = true } = {},
) {
  const set = vi.fn();
  render(
    <RowRequestSettings
      input={{ ...INHERITS, ...input }}
      set={set}
      settings={SETTINGS}
      requestsEnabled={requestsEnabled}
    />,
  );
  return set;
}

describe("RowRequestSettings", () => {
  it("names the global each field is inheriting, not a value the row does not have", () => {
    renderSection();
    expect(screen.getByText(/at least 7.3/)).toBeInTheDocument();
    expect(screen.getByText(/2 people/)).toBeInTheDocument();
    expect(screen.getByText(/\/data\/Movies/)).toBeInTheDocument();
  });

  it("describes the run cap as shared rather than offering to raise it", () => {
    // A row may only ever take LESS of the run's cap. Phrasing it as this row's own limit would
    // invite someone to raise it and quietly get nothing.
    renderSection();
    expect(
      screen.getByText(/up to 10 per run, shared between rows/),
    ).toBeInTheDocument();
  });

  it("hides the control until the row stops inheriting", () => {
    renderSection();
    expect(screen.queryByLabelText("Earliest release year")).toBeNull();
  });

  it("shows the control once the row overrides", () => {
    renderSection({ req_min_year: 2020, req_max_year: 0 });
    expect(screen.getByLabelText("Earliest release year")).toHaveValue(2020);
  });

  it("turning a toggle off seeds a concrete value rather than leaving null", async () => {
    // Leaving it null would render "inheriting" again on the next paint, so the switch would appear
    // not to work at all.
    const set = renderSection();
    await userEvent.click(
      screen.getByLabelText("Use the global minimum rating for this row"),
    );
    expect(set).toHaveBeenCalledWith({ req_min_rating: 7 });
  });

  it("turning it back on clears to null so the row inherits again", async () => {
    const set = renderSection({ req_min_rating: 6 });
    await userEvent.click(
      screen.getByLabelText("Use the global minimum rating for this row"),
    );
    expect(set).toHaveBeenCalledWith({ req_min_rating: null });
  });

  it("says a zero row cap means it never asks on its own", () => {
    renderSection({ req_max_per_row: 0 });
    expect(
      screen.getByText(/never asks for anything on its own/),
    ).toBeInTheDocument();
  });

  it("names the global tag-by-person setting while the row inherits it", () => {
    renderSection();
    expect(screen.getByText(/tag by person/)).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Tag this row's requests by person"),
    ).toBeNull();
  });

  it("a row opting out of tag-by-person sends false, not null", async () => {
    // null would read as "inherit" on the next paint and the global would switch it straight back on.
    const set = renderSection();
    await userEvent.click(
      screen.getByLabelText(
        "Use the global tag-by-person setting for this row",
      ),
    );
    expect(set).toHaveBeenCalledWith({ req_auto_user_tag: false });
  });

  it("a row can opt IN to tag-by-person while the global is off", async () => {
    const set = renderSection({ req_auto_user_tag: false });
    await userEvent.click(
      screen.getByLabelText("Tag this row's requests by person"),
    );
    expect(set).toHaveBeenCalledWith({ req_auto_user_tag: true });
  });

  it("names the global amount-of-a-show in Sonarr's own words while inheriting", () => {
    // Sonarr's label, not ours: someone comparing this row with Sonarr's Add Series screen must not
    // have to work out which of our words means which of theirs.
    renderSection();
    expect(screen.getByText(/All Episodes/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("overriding seeds a real mode, since inheriting 'all' would look like nothing happened", async () => {
    const set = renderSection();
    await userEvent.click(
      screen.getByLabelText(
        "Use the global amount-of-a-show setting for this row",
      ),
    );
    expect(set).toHaveBeenCalledWith({ req_sonarr_monitor: "firstSeason" });
  });

  it("says what the chosen mode actually downloads", async () => {
    const set = renderSection({ req_sonarr_monitor: "firstSeason" });
    expect(screen.getByText(/Season 1 only/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox"), "none");
    expect(set).toHaveBeenCalledWith({ req_sonarr_monitor: "none" });
  });

  it("warns when requests are switched off entirely", () => {
    // Otherwise the whole section reads as live configuration that does nothing.
    renderSection({}, { requestsEnabled: false });
    expect(screen.getByText(/Requests are turned off/)).toBeInTheDocument();
  });

  describe("language", () => {
    it("names the global language policy, including the bar it derives", () => {
      // min_rating 7.3 + 1.5 = 8.8. The caption has to show the number the run will actually use,
      // not "follows your minimum rating" — a row editor that restates the rule answers nothing.
      renderSection();
      expect(
        screen.getByText(/prefer English, others need 8.8/),
      ).toBeInTheDocument();
    });

    it("hides the language picker while the row inherits", () => {
      renderSection();
      expect(
        screen.queryByLabelText("Add a language to this row"),
      ).not.toBeInTheDocument();
    });

    it("seeds a sensible override when the row stops inheriting", async () => {
      // Turning the toggle off must produce a row that DOES something (a null language list would
      // be a row that silently requests nothing) — but it must NOT land on "only", the one mode
      // that DISCARDS titles rather than queueing them. Flipping a toggle to see what a control
      // does should not quietly start throwing candidates away.
      const set = renderSection();
      await userEvent.click(
        screen.getByLabelText("Use the global language setting for this row"),
      );
      expect(set).toHaveBeenCalledWith({
        req_language_mode: "prefer",
        req_preferred_languages: ["en"],
      });
    });

    it("warns differently in 'prefer', where an empty list raises the bar on everything", async () => {
      // Not the same consequence as "only", so not the same sentence: here nothing is discarded,
      // every title just counts as another language and has to clear the higher bar.
      renderSection({
        req_language_mode: "prefer",
        req_preferred_languages: [],
      });
      expect(
        screen.getByText(/every title counts as another language/i),
      ).toBeInTheDocument();
      expect(
        screen.queryByText(/never ask for anything/i),
      ).not.toBeInTheDocument();
    });

    it("does not warn about an empty list when the row is inheriting one", async () => {
      // null means "use the owner's list", and the run will. Warning "this row will never ask for
      // anything" there tells the owner the opposite of what happens.
      renderSection({
        req_language_mode: "only",
        req_preferred_languages: null,
      });
      expect(
        screen.queryByText(/never ask for anything/i),
      ).not.toBeInTheDocument();
    });

    it("clears all three fields when the row goes back to inheriting", async () => {
      // Leaving a stale list or bar behind would mean a row that reads as "inherits" on screen
      // while the run still had its own values to resolve.
      const set = renderSection({
        req_language_mode: "only",
        req_preferred_languages: ["en"],
        req_min_rating_other: 9,
      });
      await userEvent.click(
        screen.getByLabelText("Use the global language setting for this row"),
      );
      expect(set).toHaveBeenCalledWith({
        req_language_mode: null,
        req_preferred_languages: null,
        req_min_rating_other: null,
      });
    });

    it("lets a row pick its own mode", async () => {
      const set = renderSection({
        req_language_mode: "only",
        req_preferred_languages: ["en"],
      });
      await userEvent.selectOptions(
        screen.getByLabelText("Language for this row"),
        "prefer",
      );
      expect(set).toHaveBeenCalledWith({ req_language_mode: "prefer" });
    });

    it("warns when the last language is removed, as the settings screen does", async () => {
      // The settings screen already warns here. Without the same warning on the row, removing the
      // last chip leaves a row reading "Only these" with nothing listed — which the run reads as
      // "request nothing from this row", the inverse of the control and entirely silent.
      renderSection({
        req_language_mode: "only",
        req_preferred_languages: [],
      });
      expect(
        screen.getByText(/this row will never ask for anything/i),
      ).toBeInTheDocument();
    });

    it("adds and removes languages on the row", async () => {
      const set = renderSection({
        req_language_mode: "only",
        req_preferred_languages: ["en"],
      });
      await userEvent.selectOptions(
        screen.getByLabelText("Add a language to this row"),
        "ja",
      );
      expect(set).toHaveBeenCalledWith({
        req_preferred_languages: ["en", "ja"],
      });

      await userEvent.click(screen.getByLabelText(/Remove English/));
      expect(set).toHaveBeenCalledWith({ req_preferred_languages: [] });
    });
  });
});
