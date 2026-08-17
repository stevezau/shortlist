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
  req_max_per_row: null,
  req_radarr_root_folder: null,
  req_radarr_quality_profile_id: null,
  req_sonarr_root_folder: null,
  req_sonarr_quality_profile_id: null,
};

const SETTINGS = {
  "requests.enabled": true,
  "requests.min_rating": 7.3,
  "requests.min_demand": 2,
  "requests.max_per_run": 10,
  "requests.auto_send": true,
  "requests.radarr.root_folder": "/data/Movies",
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
    expect(screen.getByText(/never asks for anything on its own/)).toBeInTheDocument();
  });

  it("warns when requests are switched off entirely", () => {
    // Otherwise the whole section reads as live configuration that does nothing.
    renderSection({}, { requestsEnabled: false });
    expect(screen.getByText(/Requests are turned off/)).toBeInTheDocument();
  });
});
