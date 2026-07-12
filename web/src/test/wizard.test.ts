import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resolveArea } from "@/lib/auth";
import {
  canLeaveStep,
  clampStep,
  TOTAL_STEPS,
  useWizard,
  WIZARD_STEPS,
} from "@/lib/wizard";

vi.mock("@/lib/api", () => ({
  api: {
    getSetupState: vi.fn(),
    putSetupState: vi.fn(),
  },
}));

const { api } = await import("@/lib/api");
const getSetupState = vi.mocked(api.getSetupState);
const putSetupState = vi.mocked(api.putSetupState);

describe("clampStep", () => {
  it("keeps in-range steps and clamps everything else", () => {
    expect(clampStep(0)).toBe(0);
    expect(clampStep(7)).toBe(7);
    expect(clampStep(-3)).toBe(0);
    expect(clampStep(99)).toBe(TOTAL_STEPS - 1);
    expect(clampStep(Number.NaN)).toBe(0);
    expect(clampStep(2.9)).toBe(2);
  });
});

describe("canLeaveStep", () => {
  it("gates step 1 on a linked server", () => {
    expect(canLeaveStep(1, {})).toBe(false);
    expect(canLeaveStep(1, { linked: true })).toBe(true);
  });

  it("gates step 3 on a chosen curator (None counts)", () => {
    expect(canLeaveStep(3, {})).toBe(false);
    expect(canLeaveStep(3, { curator_provider: "none" })).toBe(true);
    expect(canLeaveStep(3, { curator_provider: "anthropic" })).toBe(true);
  });

  it("gates step 5 on privacy passing or an explicit skip", () => {
    expect(canLeaveStep(5, {})).toBe(false);
    expect(canLeaveStep(5, { privacy_passed: true })).toBe(true);
    expect(canLeaveStep(5, { privacy_skipped: true })).toBe(true);
  });

  it("leaves the ungated steps open", () => {
    for (const step of [0, 4, 6, 7]) {
      expect(canLeaveStep(step, {})).toBe(true);
    }
  });

  it("will not leave the history step without a TMDB key", () => {
    // Without one there is nothing to recommend FROM: every run dies at the first user.
    expect(canLeaveStep(2, {})).toBe(false);
    expect(canLeaveStep(2, { history_source: "plex" })).toBe(false);
    expect(canLeaveStep(2, { tmdb_set: true })).toBe(true);
  });
});

describe("resolveArea (route guards)", () => {
  it("sends unauthenticated visitors to login regardless of setup", () => {
    expect(resolveArea(false, false)).toBe("login");
    expect(resolveArea(false, true)).toBe("login");
  });

  it("sends authenticated owners with unfinished setup to the wizard", () => {
    expect(resolveArea(true, false)).toBe("setup");
  });

  it("sends fully set-up owners to the app", () => {
    expect(resolveArea(true, true)).toBe("app");
  });
});

describe("useWizard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    putSetupState.mockResolvedValue({ step: 0, state: {}, completed: false });
  });

  it("resumes step and data from the persisted setup state", async () => {
    getSetupState.mockResolvedValue({
      step: 3,
      state: { linked: true, server_name: "SFLIX" },
      completed: false,
    });

    const { result } = renderHook(() => useWizard());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.step).toBe(3);
    expect(result.current.data).toEqual({ linked: true, server_name: "SFLIX" });
  });

  it("starts at step 0 when no state exists yet", async () => {
    getSetupState.mockRejectedValue(new Error("404"));

    const { result } = renderHook(() => useWizard());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.step).toBe(0);
    expect(result.current.data).toEqual({});
  });

  it("advances on next() and persists the new step", async () => {
    getSetupState.mockResolvedValue({ step: 0, state: {}, completed: false });
    const { result } = renderHook(() => useWizard());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.next());

    expect(result.current.step).toBe(1);
    expect(putSetupState).toHaveBeenCalledWith({
      step: 1,
      state: {},
      completed: false,
    });
  });

  it("refuses to advance past a gated step", async () => {
    getSetupState.mockResolvedValue({ step: 1, state: {}, completed: false });
    const { result } = renderHook(() => useWizard());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.next());

    expect(result.current.step).toBe(1);
    expect(result.current.canProceed).toBe(false);
    expect(putSetupState).not.toHaveBeenCalled();
  });

  it("advances a gated step once its condition is met", async () => {
    getSetupState.mockResolvedValue({ step: 1, state: {}, completed: false });
    const { result } = renderHook(() => useWizard());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.update({ linked: true }));
    act(() => result.current.next());

    expect(result.current.step).toBe(2);
  });

  it("floors back() at step 0", async () => {
    getSetupState.mockResolvedValue({ step: 0, state: {}, completed: false });
    const { result } = renderHook(() => useWizard());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.back());

    expect(result.current.step).toBe(0);
    expect(putSetupState).not.toHaveBeenCalled();
  });

  it("merges update() patches and persists them at the current step", async () => {
    getSetupState.mockResolvedValue({
      step: 2,
      state: { linked: true },
      completed: false,
    });
    const { result } = renderHook(() => useWizard());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.update({ history_source: "tautulli" }));

    expect(result.current.data).toEqual({
      linked: true,
      history_source: "tautulli",
    });
    expect(putSetupState).toHaveBeenCalledWith({
      step: 2,
      state: { linked: true, history_source: "tautulli" },
      completed: false,
    });
  });

  it("marks setup completed and then calls onComplete", async () => {
    getSetupState.mockResolvedValue({
      step: 7,
      state: { linked: true },
      completed: false,
    });
    const onComplete = vi.fn();
    const { result } = renderHook(() => useWizard(onComplete));
    await waitFor(() => expect(result.current.loaded).toBe(true));

    await act(() => result.current.complete());

    expect(putSetupState).toHaveBeenCalledWith({
      step: 7,
      state: { linked: true },
      completed: true,
    });
    expect(onComplete).toHaveBeenCalledOnce();
    // onComplete must run after the persistence attempt settled.
    expect(putSetupState.mock.invocationCallOrder[0]).toBeLessThan(
      onComplete.mock.invocationCallOrder[0] ?? Infinity,
    );
  });

  it("gates step 5 on a passing Privacy Check", () => {
    expect(canLeaveStep(5, {})).toBe(false);
    expect(canLeaveStep(5, { privacy_passed: true })).toBe(true);
  });

  it("lets an owner past step 5 by explicitly accepting the risk", () => {
    // The skip is deliberate but consequential: the server still refuses real writes,
    // so step 7 must fall back to a dry run (see step-first-run).
    expect(canLeaveStep(5, { privacy_skipped: true })).toBe(true);
  });

  it("has a title and one-line why for every step", () => {
    expect(WIZARD_STEPS).toHaveLength(TOTAL_STEPS);
    for (const step of WIZARD_STEPS) {
      expect(step.title.length).toBeGreaterThan(0);
      expect(step.why.length).toBeGreaterThan(0);
    }
  });
});
