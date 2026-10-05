/**
 * The rail's Privacy dot (and the dashboard's Privacy cell) read the live privacy status at a glance.
 *
 * Each read costs a plex.tv roster read AND a PMS collections read. The rail is on every page, so
 * with the page's own options (60s stale, refetch on focus) an owner alt-tabbing anywhere in the app
 * re-read plex.tv every minute. The glance is allowed to be five minutes old and does not chase
 * window focus; the Privacy page itself still reads fresh on open and on "Read again".
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { usePrivacyGlance } from "@/lib/privacy-attention";
import { queryKeys } from "@/lib/queries";

const { getPrivacyStatus } = vi.hoisted(() => ({ getPrivacyStatus: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { getPrivacyStatus: () => getPrivacyStatus() } };
});

describe("usePrivacyGlance", () => {
  it("holds a reading for five minutes and does not re-read plex.tv on window focus", async () => {
    getPrivacyStatus.mockResolvedValue({ summary: "clean", accounts: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => usePrivacyGlance(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const query = client.getQueryCache().find({ queryKey: queryKeys.privacyStatus });
    const options = query?.observers[0]?.options;
    expect(options?.staleTime).toBe(5 * 60_000);
    expect(options?.refetchOnWindowFocus).toBe(false);
  });

  it("shares the Privacy page's cache entry, so opening that page shows the same reading", async () => {
    getPrivacyStatus.mockResolvedValue({ summary: "clean", accounts: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => usePrivacyGlance(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(client.getQueryData(queryKeys.privacyStatus)).toEqual({ summary: "clean", accounts: [] });
  });
});
