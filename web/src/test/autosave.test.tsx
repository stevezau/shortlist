import { act, render } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AUTOSAVE_DELAY_MS, useAutosave } from "@/lib/autosave";

function Probe({ value, save }: { value: string; save: () => void }) {
  useAutosave(value, save);
  return null;
}

describe("useAutosave", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("writes nothing for merely opening a page, even under StrictMode's double effect", () => {
    // The app mounts under <StrictMode>; in dev it runs every effect twice, which used to defeat a
    // "first render" flag and PUT the whole form back to the server on load.
    const save = vi.fn();
    render(
      <StrictMode>
        <Probe value="a" save={save} />
      </StrictMode>,
    );
    act(() => void vi.advanceTimersByTime(AUTOSAVE_DELAY_MS * 2));
    expect(save).not.toHaveBeenCalled();
  });

  it("saves once after an edit, and again when the edit is reverted", () => {
    const save = vi.fn();
    const { rerender } = render(
      <StrictMode>
        <Probe value="a" save={save} />
      </StrictMode>,
    );
    rerender(
      <StrictMode>
        <Probe value="b" save={save} />
      </StrictMode>,
    );
    act(() => void vi.advanceTimersByTime(AUTOSAVE_DELAY_MS));
    expect(save).toHaveBeenCalledTimes(1);
    rerender(
      <StrictMode>
        <Probe value="a" save={save} />
      </StrictMode>,
    );
    act(() => void vi.advanceTimersByTime(AUTOSAVE_DELAY_MS));
    expect(save).toHaveBeenCalledTimes(2);
  });
});
