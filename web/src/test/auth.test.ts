import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { plexAuthUrl, usePlexPin } from "@/lib/auth";

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return {
    ApiError,
    api: { createPin: vi.fn(), getPin: vi.fn() },
  };
});

const { api, ApiError } = await import("@/lib/api");
const createPin = vi.mocked(api.createPin);
const getPin = vi.mocked(api.getPin);

const POLL_MS = 2_000;
const TIMEOUT_MS = 5 * 60 * 1000;
const PIN = { id: 42, code: "WXYZ", client_id: "cid" };

/** Advance fake timers inside act so the hook's interval-driven state updates flush. */
async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("plexAuthUrl", () => {
  it("builds the plex.tv auth-app URL with the client id and code encoded", () => {
    const url = plexAuthUrl("client 1", "AB/CD");
    expect(url).toContain("clientID=client%201");
    expect(url).toContain("code=AB%2FCD");
    expect(url).toContain("product%5D=Rowarr");
  });
});

describe("usePlexPin", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    createPin.mockReset();
    getPin.mockReset();
    vi.stubGlobal(
      "open",
      vi.fn(() => ({ close: vi.fn() })),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("starts idle and moves to waiting with the code once the pin is created", async () => {
    createPin.mockResolvedValue(PIN);
    getPin.mockResolvedValue({ linked: false });

    const { result } = renderHook(() => usePlexPin());
    expect(result.current.phase).toBe("idle");

    act(() => result.current.start());
    expect(result.current.phase).toBe("waiting");

    await tick(0); // flush createPin → code set, popup opened, polling armed
    expect(result.current.code).toBe("WXYZ");
    expect(result.current.popupBlocked).toBe(false);
    expect(window.open).toHaveBeenCalledOnce();
  });

  it("reaches 'linked' and reports the status when a poll returns linked", async () => {
    createPin.mockResolvedValue(PIN);
    getPin.mockResolvedValue({
      linked: true,
      username: "steve",
      account_id: 7,
    });
    const onLinked = vi.fn();

    const { result } = renderHook(() => usePlexPin(onLinked));
    act(() => result.current.start());
    await tick(0);
    await tick(POLL_MS); // first poll

    expect(result.current.phase).toBe("linked");
    expect(result.current.status).toEqual({
      linked: true,
      username: "steve",
      account_id: 7,
    });
    expect(onLinked).toHaveBeenCalledExactlyOnceWith({
      linked: true,
      username: "steve",
      account_id: 7,
    });
  });

  it("flags a blocked popup so the code-entry fallback can show", async () => {
    createPin.mockResolvedValue(PIN);
    getPin.mockResolvedValue({ linked: false });
    vi.stubGlobal(
      "open",
      vi.fn(() => null),
    ); // browser blocked window.open

    const { result } = renderHook(() => usePlexPin());
    act(() => result.current.start());
    await tick(0);

    expect(result.current.popupBlocked).toBe(true);
    // A blocked popup is not an error — polling continues, the code is shown for manual entry.
    expect(result.current.phase).toBe("waiting");
    expect(result.current.code).toBe("WXYZ");
  });

  it("errors out when the pin never links before the timeout", async () => {
    createPin.mockResolvedValue(PIN);
    getPin.mockResolvedValue({ linked: false });

    const { result } = renderHook(() => usePlexPin());
    act(() => result.current.start());
    await tick(0);
    await tick(TIMEOUT_MS + POLL_MS); // poll past the 5-minute window

    expect(result.current.phase).toBe("error");
    expect(result.current.error).toMatch(/timed out/i);
  });

  it("surfaces a createPin failure as an error phase", async () => {
    createPin.mockRejectedValue(new ApiError(500, "Plex is down"));

    const { result } = renderHook(() => usePlexPin());
    act(() => result.current.start());
    await tick(0);

    expect(result.current.phase).toBe("error");
    expect(result.current.error).toBe("Plex is down");
  });
});
