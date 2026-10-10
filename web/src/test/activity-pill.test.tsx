import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { ActivityPill } from "@/components/layout/activity-pill";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners: Record<string, ((event: MessageEvent<string>) => void)[]> = {};
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.instances.push(this);
  }
  addEventListener(
    type: string,
    handler: (event: MessageEvent<string>) => void,
  ) {
    (this.listeners[type] ??= []).push(handler);
  }
  close() {}
  emit(type: string, data: unknown) {
    for (const handler of this.listeners[type] ?? []) {
      handler({ data: JSON.stringify(data) } as MessageEvent<string>);
    }
  }
}
vi.stubGlobal("EventSource", FakeEventSource);

describe("ActivityPill", () => {
  it("names the row, library and write a person is on, not just the phase", () => {
    render(
      <MemoryRouter>
        <ActivityPill />
      </MemoryRouter>,
    );

    act(() => {
      FakeEventSource.instances.at(-1)?.emit("run.user.stage", {
        seq: 1,
        run_id: 14,
        user: "sam",
        stage: "delivering",
        counts: {
          row: "Because you watched Dune",
          library: "TV Shows",
          adding: 3,
          removing: 2,
        },
      });
    });

    const text =
      "sam — writing the row to Plex — Because you watched Dune · TV Shows · adding 3 titles · removing 2 titles";
    const pill = screen.getByRole("link");
    expect(pill).toHaveTextContent(text);
    // The sidebar truncates it; the full sentence stays one hover away.
    expect(pill).toHaveAttribute("title", text);
  });
});
