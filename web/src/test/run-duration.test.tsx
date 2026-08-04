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

  it("does not tick per-second once the run has finished", () => {
    // What `active` is actually for: a finished run must not hold a one-second timer. It still
    // ticks, just slowly — the next test is why "inactive" must not mean frozen.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:38:00Z"));

    render(
      <RunStarted run={makeRun({ finished_at: "2026-07-19T03:33:00Z" })} />,
    );
    expect(screen.getByText("8m ago")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(screen.getByText("8m ago")).toBeInTheDocument();
  });

  it("a finished run's 'started N ago' still moves on with the clock", () => {
    // Slow must not mean never. This assertion used to read "still 8m ago" and passed for the wrong
    // reason: the clock was pinned to its mount value, so every finished run on a tab left open went
    // on claiming it started whenever the page happened to load.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:38:00Z"));

    render(
      <RunStarted run={makeRun({ finished_at: "2026-07-19T03:33:00Z" })} />,
    );
    expect(screen.getByText("8m ago")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(60 * 60_000);
    });

    expect(screen.queryByText("8m ago")).toBeNull();
  });
});
