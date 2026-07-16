import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowSourcesField } from "@/components/rows/row-sources-field";
import type { Settings } from "@/lib/types";

const { getSettings } = vi.hoisted(() => ({ getSettings: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: { getSettings: () => getSettings() },
}));

function renderField(value: string[], onChange: (next: string[]) => void) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RowSourcesField value={value} onChange={onChange} />
    </QueryClientProvider>,
  );
}

/** The real thing: a parent that actually holds the row's draft, as the row editor does. */
function renderLive(initial: string[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Harness() {
    const [value, setValue] = useState(initial);
    return <RowSourcesField value={value} onChange={setValue} />;
  }
  render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

const aiSwitch = () =>
  screen.getByLabelText(/Enable AI — suggests from your library for this row/i);

describe("RowSourcesField", () => {
  beforeEach(() => {
    getSettings.mockReset();
    getSettings.mockResolvedValue({} as Settings);
  });

  it("shows the inherit message and no source switches when empty (global default)", () => {
    renderField([], () => {});
    expect(
      screen.getByText(/uses the sources you enabled in Settings/i),
    ).toBeTruthy();
    expect(screen.queryByLabelText(/for this row/i)).toBeNull();
  });

  it("reveals per-source switches when the row overrides sources", () => {
    renderField(["tmdb_similar"], () => {});
    // The custom mode renders a switch per known source, each labelled "…for this row".
    expect(
      screen.getByLabelText(/Enable TMDB — similar titles for this row/i),
    ).toBeTruthy();
    expect(
      screen.getByLabelText(/Enable Trakt — related titles for this row/i),
    ).toBeTruthy();
  });

  it("keeps a source whose dependency isn't met (intent) and shows what's needed — never strips it", async () => {
    // No curator, but the row carries llm_library. Intent model: it's kept (not stripped), the toggle
    // stays usable and ON, and a note explains the missing dependency (fixed globally, not per-row).
    const onChange = vi.fn();
    renderField(["tmdb_similar", "llm_library"], onChange);

    await waitFor(() => expect(aiSwitch()).not.toBeDisabled());
    expect(aiSwitch()).toBeChecked(); // still on — intent preserved
    expect(onChange).not.toHaveBeenCalled(); // NOT auto-stripped
    expect(
      screen.getByText(/Needs an AI curator — set one up in Connections/i),
    ).toBeInTheDocument();
  });

  it("shows no library-source dependency note once a curator is configured", async () => {
    getSettings.mockResolvedValue({ "curator.provider": "anthropic" });
    const onChange = vi.fn();
    renderField(["llm_library"], onChange);

    await waitFor(() => expect(aiSwitch()).not.toBeDisabled());
    expect(
      screen.queryByText(/Needs an AI curator — set one up in Connections/i),
    ).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("locks the switches until it knows which sources this setup can run", () => {
    getSettings.mockReturnValue(new Promise(() => {})); // settings still in flight
    renderField(["tmdb_similar"], () => {});
    // Briefly-live toggles let an owner turn on a source their setup can't actually run.
    expect(aiSwitch()).toBeDisabled();
    expect(
      screen.getByLabelText(/Enable TMDB — similar titles for this row/i),
    ).toBeDisabled();
  });

  it("says the row has fallen back to the global set when Custom is left with nothing ticked", async () => {
    renderLive(["tmdb_similar"]);

    await userEvent.click(
      screen.getByLabelText(/Enable TMDB — similar titles for this row/i),
    );

    // It used to snap silently back to the global view, switches and all.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /falls back to the global default/i,
    );
    expect(
      screen.getByLabelText(/Enable TMDB — similar titles for this row/i),
    ).toBeTruthy();
  });
});
