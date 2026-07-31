import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

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
