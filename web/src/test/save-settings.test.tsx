import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { useSaveSettings } from "@/lib/queries";

describe("useSaveSettings", () => {
  it("refreshes the rows too, so an open editor can't send a stale default-row name back", async () => {
    // The default row's name IS a setting (`row.name_template`). An editor opened from a cached
    // row list after a Settings save would send the old name on Save, and the server writes it
    // back into Settings — undoing the rename.
    vi.spyOn(api, "putSettings").mockResolvedValue({} as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useSaveSettings(), { wrapper });
    await act(() => result.current.mutateAsync({ "row.name_template": "✨ Picks" }));

    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey);
    expect(keys).toContainEqual(["settings"]);
    expect(keys).toContainEqual(["collections"]);
  });
});
