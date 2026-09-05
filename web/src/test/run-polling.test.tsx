import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { useRun, useRunsPaged } from "@/lib/queries";

const { getRun, getRuns } = vi.hoisted(() => ({
  getRun: vi.fn(),
  getRuns: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, getRun, getRuns } };
});

const RUNNING = {
  id: 2,
  trigger: "manual",
  status: "running",
  started_at: "2026-07-15T04:18:00Z",
  finished_at: null,
  dry_run: false,
  stats: {},
};
const FINISHED = {
  ...RUNNING,
  status: "aborted",
  finished_at: "2026-07-15T04:21:00Z",
};

/**
 * The refetch predicate as the hook actually installed it, invoked directly.
 *
 * Reading it off the cached query and calling it beats advancing fake timers: it asserts the same
 * contract without the flakiness that combining fake timers with React Query's async `queryFn` has
 * repeatedly caused in this suite, and it fails loudly if the option is simply absent — which is the
 * regression that matters, since the option missing is exactly today's bug.
 */
function installedInterval(
  client: QueryClient,
  queryKey: readonly unknown[],
  data: unknown,
): number | false {
  const query = client.getQueryCache().find({ queryKey, exact: true });
  if (!query)
    throw new Error(`no query cached for ${JSON.stringify(queryKey)}`);
  // `Query.options` is typed as the narrower `QueryOptions`, which omits the observer-level
  // `refetchInterval` the hook actually sets — hence the cast rather than a plain property read.
  const { refetchInterval } = query.options as unknown as {
    refetchInterval?: (q: { state: { data: unknown } }) => number | false;
  };
  if (typeof refetchInterval !== "function")
    throw new Error(
      "the run query has no refetchInterval — the SSE stream is its only refresh, and " +
        "EventSource replays nothing it missed while disconnected",
    );
  return refetchInterval({ state: { data } });
}

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

describe("run queries poll as a fallback for a dropped SSE stream", () => {
  let client: QueryClient;

  beforeEach(() => {
    getRun.mockReset().mockResolvedValue(RUNNING);
    getRuns.mockReset().mockResolvedValue([RUNNING]);
    client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
  });

  it("keeps refetching one run until it finishes", async () => {
    const { result } = renderHook(() => useRun(2), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(installedInterval(client, ["runs", 2], RUNNING)).toBe(5_000);
    expect(installedInterval(client, ["runs", 2], FINISHED)).toBe(false);
  });

  it("keeps refetching the runs list until every page has settled", async () => {
    const { result } = renderHook(() => useRunsPaged(), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const key = ["runs", "paged"];
    expect(installedInterval(client, key, { pages: [[RUNNING]] })).toBe(5_000);
    expect(installedInterval(client, key, { pages: [[FINISHED]] })).toBe(false);
  });
});
