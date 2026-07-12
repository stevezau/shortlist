import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSSE } from "@/lib/sse";
import type { RunUserStageEvent } from "@/lib/types";

type Listener = (event: MessageEvent<string>) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readonly listeners = new Map<string, Listener[]>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: Listener): void {
    const existing = this.listeners.get(name) ?? [];
    this.listeners.set(name, [...existing, listener]);
  }

  close(): void {
    this.closed = true;
  }

  emit(name: string, data: string): void {
    for (const listener of this.listeners.get(name) ?? []) {
      listener({ data } as MessageEvent<string>);
    }
  }
}

function latestSource(): FakeEventSource {
  const source = FakeEventSource.instances.at(-1);
  if (!source) throw new Error("no EventSource was created");
  return source;
}

describe("useSSE", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("opens exactly one connection to /api/events", () => {
    renderHook(() => useSSE({}));

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(latestSource().url).toBe("/api/events");
  });

  it("reports connected after the stream opens", () => {
    const { result } = renderHook(() => useSSE({}));
    expect(result.current.connected).toBe(false);

    act(() => latestSource().onopen?.());

    expect(result.current.connected).toBe(true);
  });

  it("dispatches run.user.stage to its handler with parsed JSON", () => {
    const onRunUserStage = vi.fn();
    const onRunFinished = vi.fn();
    renderHook(() => useSSE({ onRunUserStage, onRunFinished }));

    const stage: RunUserStageEvent = {
      user: "sarah",
      stage: "curating",
      counts: { candidates: 40 },
    };
    act(() => latestSource().emit("run.user.stage", JSON.stringify(stage)));

    expect(onRunUserStage).toHaveBeenCalledExactlyOnceWith(stage);
    expect(onRunFinished).not.toHaveBeenCalled();
  });

  it("dispatches privacy.probe.step log lines during the Privacy Check", () => {
    const onPrivacyProbeStep = vi.fn();
    renderHook(() => useSSE({ onPrivacyProbeStep }));

    act(() =>
      latestSource().emit(
        "privacy.probe.step",
        JSON.stringify({ message: "Creating probe collection…" }),
      ),
    );

    expect(onPrivacyProbeStep).toHaveBeenCalledExactlyOnceWith({
      message: "Creating probe collection…",
    });
  });

  it("ignores malformed event payloads instead of crashing", () => {
    const onRunUserStage = vi.fn();
    renderHook(() => useSSE({ onRunUserStage }));

    act(() => latestSource().emit("run.user.stage", "not json"));

    expect(onRunUserStage).not.toHaveBeenCalled();
  });

  it("uses the latest handlers without reopening the connection", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(
      (handlers: Parameters<typeof useSSE>[0]) => useSSE(handlers),
      {
        initialProps: { onRunFinished: first },
      },
    );

    rerender({ onRunFinished: second });
    act(() =>
      latestSource().emit(
        "run.finished",
        JSON.stringify({ run_id: 1, status: "ok" }),
      ),
    );

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledExactlyOnceWith({ run_id: 1, status: "ok" });
  });

  it("reconnects after an error with exponential backoff", () => {
    renderHook(() => useSSE({}));
    const original = latestSource();

    act(() => original.onerror?.());
    expect(original.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(1);

    act(() => vi.advanceTimersByTime(1000));
    expect(FakeEventSource.instances).toHaveLength(2);

    // Second failure: the retry delay doubles to 2s.
    act(() => latestSource().onerror?.());
    act(() => vi.advanceTimersByTime(1999));
    expect(FakeEventSource.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1));
    expect(FakeEventSource.instances).toHaveLength(3);
  });

  it("closes the stream and cancels pending reconnects on unmount", () => {
    const { unmount } = renderHook(() => useSSE({}));
    const source = latestSource();

    act(() => source.onerror?.());
    unmount();

    act(() => vi.advanceTimersByTime(60_000));
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(source.closed).toBe(true);
  });
});
