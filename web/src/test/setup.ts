import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// testing-library gives an async query ONE SECOND to find its element, and that budget is spent on
// whatever else the machine is doing. The dashboard tests render a tree that waits on two queries
// (`getReport` and `getEngagement`) before the assertion's text exists, and under contention that
// exceeded 1s — intermittently, on a different assertion each time, so it read as "the suite is
// flaky" rather than "one timeout is too tight".
//
// Measured 2026-08-25: `vitest run --sequence.shuffle` on two dashboard files failed 2 of 6 runs,
// and 4 of 6 under `--pool=forks` — MORE isolation meaning more failures is the signature of a time
// budget, not of leaked state, because forks are slower to start.
//
// It must stay well UNDER vitest's own 5s per-test timeout, and the first attempt at this did not:
// setting it to exactly 5000 made 24 tests fail with "Test timed out in 5000ms". Those are the
// tests that legitimately exhaust the budget — the ones asserting an element never appears — and
// with the two numbers equal they blew the whole test instead of failing their query fast. 2.5s is
// 2.5x the old budget for a slow render while leaving 2.5s of slack beneath the test timeout.
configure({ asyncUtilTimeout: 2500 });

afterEach(() => {
  cleanup();
});

// jsdom has no EventSource, and `useSSE` (lib/sse.ts) is now reachable from more than one page's
// render tree (e.g. `useSyncWatched`, used by the dashboard's Impact report) — a test file that
// never expected to touch SSE would otherwise crash on an unrelated render. A no-op global stub is
// enough for tests that don't care about live events; a test that DOES (run-detail.test.tsx) stubs
// its own richer FakeEventSource, which simply overrides this one for that file.
class NoopEventSource {
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {}
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
}
if (typeof globalThis.EventSource === "undefined") {
  // @ts-expect-error minimal stub — jsdom ships no EventSource at all
  globalThis.EventSource = NoopEventSource;
}

// Run animation frames SYNCHRONOUSLY. jsdom schedules `requestAnimationFrame` on a real timer, so a
// callback can still be queued when a test file finishes and vitest tears the jsdom environment
// down — it then runs in a world with no `window` and vitest fails the ENTIRE run with an unhandled
// `ReferenceError: window is not defined`, while reporting every individual test as passed. That is
// a maximally confusing failure: nothing is red except the run.
//
// It bit the v1.2.0 tag build, on a commit whose web suite had already passed three times, so it is
// a genuine race rather than a broken test. `issue.tsx` is the only rAF caller in the app (its check
// panel defers a frame so the scroll target has real geometry), and in jsdom that deferral buys
// nothing — there is no layout, `matchMedia` doesn't exist, and `scrollIntoView` is optional-chained
// away. Running the callback inline removes the window in which it can outlive its environment.
globalThis.requestAnimationFrame = (cb: FrameRequestCallback): number => {
  cb(performance.now());
  return 0;
};
globalThis.cancelAnimationFrame = (): void => {};
