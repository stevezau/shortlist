import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { RunLogPanel } from "@/components/runs/run-log-panel";
import type { RunLogEntry } from "@/lib/types";

function entry(patch: Partial<RunLogEntry>): RunLogEntry {
  return {
    seq: 0,
    ts: "2026-09-23T02:00:00Z",
    run_id: 5,
    user: "sarah",
    stage: "history",
    counts: {},
    level: "info",
    ...patch,
  };
}

/**
 * A line the run's own work logged at WARNING or ERROR (`run_log.capture_warnings`): no person, the
 * level as its stage, the message as its reason. Before these existed the log carried narration only,
 * and a run's real warnings reached nobody but the container's log.
 */
const entries: RunLogEntry[] = [
  entry({ seq: 0 }),
  entry({
    seq: 1,
    user: "",
    stage: "warning",
    level: "warning",
    reason: "Plex returned 1 watched title with no tmdb:// guid",
  }),
  entry({
    seq: 2,
    user: "",
    stage: "error",
    level: "error",
    reason: "plex.tv refused a filter write",
  }),
];

function renderPanel() {
  render(
    <RunLogPanel runId={5} entries={entries} running={false} people={["sarah"]} />,
  );
  return within(screen.getByRole("log", { name: /Run activity log/i }));
}

describe("RunLogPanel — lines the run logged at a level", () => {
  it("shows a warning in the warning colour, with no person in its subject column", () => {
    const log = renderPanel();

    const message = log.getByText(/no tmdb:\/\/ guid/);
    expect(message).toHaveClass("text-warning");
    expect(message).toHaveTextContent(/^warning — Plex returned/);
    // A blank subject rendered as an empty bold column; it has no person, like a server-wide phase.
    expect(message.previousElementSibling).toHaveTextContent("——");
  });

  it("shows an error-level line as an error", () => {
    const log = renderPanel();

    expect(log.getByText(/refused a filter write/)).toHaveClass(
      "text-destructive-text",
    );
  });

  it("keeps an error-level line under Errors and both out of Per-person", async () => {
    const user = userEvent.setup();
    const log = renderPanel();

    await user.click(screen.getByRole("button", { name: "Errors" }));
    expect(log.getByText(/refused a filter write/)).toBeInTheDocument();
    expect(log.queryByText(/no tmdb:\/\/ guid/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Per-person" }));
    expect(log.queryByText(/no tmdb:\/\/ guid/)).not.toBeInTheDocument();
    expect(log.queryByText(/refused a filter write/)).not.toBeInTheDocument();
    expect(log.getByText("sarah")).toBeInTheDocument();
  });
});
