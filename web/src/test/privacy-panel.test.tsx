import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  PrivacyPanel,
  type PrivacyPanelProps,
} from "@/components/privacy-panel";

function renderPanel(props: Partial<PrivacyPanelProps> = {}) {
  const defaults: PrivacyPanelProps = {
    phase: "idle",
    logLines: [],
    tiers: null,
    error: null,
    skipped: false,
    onRun: vi.fn(),
    onSkip: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  render(<PrivacyPanel {...merged} />);
  return merged;
}

describe("PrivacyPanel", () => {
  it("offers the run button with plain-English copy when idle", async () => {
    const user = userEvent.setup();
    const { onRun } = renderPanel();

    const runButton = screen.getByRole("button", {
      name: /run privacy check/i,
    });
    expect(screen.getByText(/throwaway test row/i)).toBeInTheDocument();

    await user.click(runButton);
    expect(onRun).toHaveBeenCalledOnce();
  });

  it("streams the live log while running", () => {
    renderPanel({
      phase: "running",
      logLines: ["Creating probe collection…", "Writing canary exclusion…"],
    });

    expect(screen.getByText(/checking your server/i)).toBeInTheDocument();
    expect(screen.getByText("Creating probe collection…")).toBeInTheDocument();
    expect(screen.getByText("Writing canary exclusion…")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /run privacy check/i }),
    ).not.toBeInTheDocument();
  });

  it("shows the big green panel with tier results on pass", () => {
    renderPanel({ phase: "passed", tiers: { t1: true, t2: true } });

    expect(screen.getByRole("status")).toHaveTextContent(
      /your server keeps rows private/i,
    );
    expect(
      screen.getByText(/share filters: kept private/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/home view: kept private/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/i understand the risk/i),
    ).not.toBeInTheDocument();
  });

  it("shows the failure panel with the leaked tier and a retry", () => {
    renderPanel({ phase: "failed", tiers: { t1: true, t2: false } });

    expect(screen.getByRole("alert")).toHaveTextContent(
      /privacy check failed/i,
    );
    expect(
      screen.getByText(/home view: visible to others/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /run the check again/i }),
    ).toBeInTheDocument();
  });

  it("keeps the skip escape hatch behind the details fold and wires onSkip", async () => {
    const user = userEvent.setup();
    const { onSkip } = renderPanel({ phase: "failed", tiers: null });

    await user.click(screen.getByText(/i understand the risk/i));
    await user.click(
      screen.getByRole("button", { name: /skip — continue without/i }),
    );

    expect(onSkip).toHaveBeenCalledOnce();
  });

  it("surfaces a run error as the failure detail", () => {
    renderPanel({
      phase: "failed",
      error: "plex.tv returned 429 — try again in a minute.",
    });

    expect(screen.getByText(/plex\.tv returned 429/i)).toBeInTheDocument();
  });

  it("notes when verification was skipped", () => {
    renderPanel({ phase: "failed", skipped: true });

    expect(
      screen.getByText(/privacy verification skipped/i),
    ).toBeInTheDocument();
  });
});
