import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunDuration, RunStarted } from "@/pages/runs";
import type { Run } from "@/lib/types";

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: 1,
    trigger: "manual",
    started_at: "2026-07-19T03:30:00Z",
    began_at: "2026-07-19T03:30:00Z",
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

  it("shows no duration at all for a run that is still queued", () => {
    // `runs.started_at` is stamped by the column default at INSERT — when the run was ASKED for,
    // not when it began. Treating "no finish time" as "running" therefore ticked a duration up from
    // the button press for a run still waiting on the writer lock, under a tooltip saying
    // "Running…" beside a badge saying "queued".
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:30:11Z")); // 11s after it was queued

    render(
      <RunDuration run={makeRun({ status: "queued", finished_at: null })} />,
    );

    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText(/11\.0s/)).toBeNull();
  });

  it("says a run cancelled while still queued NEVER RAN, rather than claiming the wait as work", () => {
    // Reported from a real server: three runs queued together, cancelled nine minutes later, each
    // showing "9m 26s". None of them had executed a single step. The duration was measured from
    // `started_at`, which is stamped when the ROW is created, so it was timing the queue.
    render(
      <RunDuration
        run={makeRun({
          status: "aborted",
          began_at: null,
          finished_at: "2026-07-19T03:39:26Z",
        })}
      />,
    );

    expect(screen.getByText("never ran")).toBeInTheDocument();
    expect(screen.queryByText(/9m/)).toBeNull();
  });

  it("times a run that DID start from when it began, not from when it was queued", () => {
    // It waited 5 minutes behind another run, then worked for 2. The answer is 2 minutes.
    render(
      <RunDuration
        run={makeRun({
          status: "ok",
          began_at: "2026-07-19T03:35:00Z",
          finished_at: "2026-07-19T03:37:00Z",
        })}
      />,
    );

    expect(screen.getByText("2m 0s")).toBeInTheDocument();
  });

  it("does not tick a queued run up as time passes", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:30:10Z"));

    render(
      <RunDuration run={makeRun({ status: "queued", finished_at: null })} />,
    );

    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    // Still nothing — the whole point is that waiting is not elapsed work.
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText(/s$/)).toBeNull();
  });
});

describe("RunStarted", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says a queued run was QUEUED, not started", () => {
    // Same root cause as the duration beside it: `started_at` is the moment the run was asked for.
    // A run sitting behind the writer lock read "Started · 5m ago" when it had not begun.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T03:35:00Z")); // 5m after it was queued

    render(
      <RunStarted run={makeRun({ status: "queued", finished_at: null })} />,
    );

    expect(screen.getByText("queued 5m ago")).toBeInTheDocument();
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
