import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunDuration } from "@/pages/runs";
import type { Run } from "@/lib/types";

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: 1,
    trigger: "manual",
    started_at: "2026-07-19T03:30:00Z",
    finished_at: null,
    status: "running",
    dry_run: false,
    stats: {} as Run["stats"],
    ...overrides,
  };
}

describe("RunDuration", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the fixed wall-clock for a finished run", () => {
    render(
      <RunDuration run={makeRun({ finished_at: "2026-07-19T03:52:30Z" })} />,
    );

    expect(screen.getByText("22m 30s")).toBeInTheDocument();
  });

  it("renders an em dash when the timestamps can't produce a duration", () => {
    render(
      <RunDuration run={makeRun({ finished_at: "2026-07-19T03:29:00Z" })} />,
    );

    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("ticks up live while a run is still running, then clears its timer on unmount", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:30:10Z")); // 10s after start
    const clearSpy = vi.spyOn(globalThis, "clearInterval");

    const { unmount } = render(
      <RunDuration run={makeRun({ finished_at: null })} />,
    );
    expect(screen.getByText("10.0s")).toBeInTheDocument();

    // Advancing the timer also advances the mocked clock, so each tick re-reads Date.now().
    act(() => {
      vi.advanceTimersByTime(5000); // 5 one-second ticks → 15s elapsed
    });
    expect(screen.getByText("15.0s")).toBeInTheDocument();

    unmount();
    expect(clearSpy).toHaveBeenCalled();
  });
});
