import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useHashScroll } from "@/lib/use-hash-scroll";

/** A page whose anchor target appears only once `ready` flips — the shape of a query-backed page. */
function Page({ ready }: { ready: boolean }) {
  useHashScroll(ready);
  return ready ? <div id="danger">Danger zone</div> : <p>loading…</p>;
}

function renderAt(path: string, ready: boolean) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Page ready={ready} />
    </MemoryRouter>,
  );
}

describe("useHashScroll", () => {
  let scrollIntoView: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    scrollIntoView = vi.fn<() => void>();
    Element.prototype.scrollIntoView = scrollIntoView;
  });

  it("scrolls to the hashed element once it exists", () => {
    // The bug this exists for: on a cold load the browser resolves #danger while the page is still a
    // skeleton, finds nothing, and gives up — so the link landed at the top of the page.
    const { rerender } = renderAt("/settings#danger", false);
    expect(scrollIntoView).not.toHaveBeenCalled(); // nothing to scroll to yet

    rerender(
      <MemoryRouter initialEntries={["/settings#danger"]}>
        <Page ready={true} />
      </MemoryRouter>,
    );
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.instances[0]).toBe(
      document.getElementById("danger"),
    );
  });

  it("does nothing when there is no hash", () => {
    renderAt("/settings", true);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("does nothing when the hash names no element on the page", () => {
    // A stale bookmark to a section that no longer exists must not throw — it just stays put.
    renderAt("/settings#schedules", true);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
