import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunDuration, RunStarted } from "@/pages/runs";
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
    error: null,
    promotion_blockers: [],
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

describe("RunStarted", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps pace with the duration beside it while a run is running", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:38:00Z")); // 8m after start

    render(<RunStarted run={makeRun({ finished_at: null })} />);
    expect(screen.getByText("8m ago")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3 * 60_000);
    });
    expect(screen.getByText("11m ago")).toBeInTheDocument();
  });

  it("starts no timer once the run has finished", () => {
    // Asserts the TIMER, not the rendered text. Asserting the text stayed "8m ago" passed for the
    // wrong reason: the clock was pinned to its mount value, so a finished run's "started N ago" was
    // frozen at whatever the page loaded with and never moved again on a tab left open.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:38:00Z"));
    const setInterval = vi.spyOn(globalThis, "setInterval");

    render(
      <RunStarted run={makeRun({ finished_at: "2026-07-19T03:33:00Z" })} />,
    );
    expect(screen.getByText("8m ago")).toBeInTheDocument();
    expect(setInterval).not.toHaveBeenCalled();

    setInterval.mockRestore();
  });

  it("a finished run's 'started N ago' still moves on with the clock", () => {
    // The other half: no timer must not mean no update. A run that finished an hour ago should not
    // still claim it started 8 minutes ago because that is when the tab was opened.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:38:00Z"));

    const { rerender } = render(
      <RunStarted run={makeRun({ finished_at: "2026-07-19T03:33:00Z" })} />,
    );
    expect(screen.getByText("8m ago")).toBeInTheDocument();

    vi.setSystemTime(new Date("2026-07-19T04:38:00Z"));
    act(() => {
      rerender(
        <RunStarted run={makeRun({ finished_at: "2026-07-19T03:33:00Z" })} />,
      );
    });

    expect(screen.queryByText("8m ago")).toBeNull();
  });
});
