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
  let scrollIntoView: ReturnType<
    typeof vi.fn<(options?: ScrollIntoViewOptions) => void>
  >;

  beforeEach(() => {
    scrollIntoView = vi.fn<(options?: ScrollIntoViewOptions) => void>();
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

  it("animates the jump, and does not when the viewer asked for no motion", () => {
    // Asserting the ARGUMENT, not just the call: `.claude/rules/testing.md` — if removing a
    // parameter from the code would not break the test, the test is not covering it. The CSS guard
    // cannot catch this one, because a `behavior` passed here overrides the computed value.
    renderAt("/settings#danger", true);
    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "center",
    });

    // Assigned, not spied: this jsdom has no `window.matchMedia` at all, which is why the hook
    // optional-calls it — and why the assertion above is the real "no preference expressed" case.
    scrollIntoView.mockClear();
    const original = window.matchMedia;
    window.matchMedia = vi.fn(
      () => ({ matches: true }) as unknown as MediaQueryList,
    );
    renderAt("/settings#danger", true);
    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "auto",
      block: "center",
    });
    window.matchMedia = original;
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
