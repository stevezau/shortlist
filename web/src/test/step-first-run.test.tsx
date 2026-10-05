import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { StepFirstRun } from "@/pages/setup/step-first-run";
import type { SSEHandlers } from "@/lib/sse";
const state = vi.hoisted(() => ({ run: undefined as unknown, start: vi.fn(), handlers: {} as SSEHandlers, refetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: { startRun: () => state.start() }, apiErrorMessage: (_e: unknown, fallback: string) => fallback }));
vi.mock("@/lib/queries", () => ({
  useUsers: () => ({ data: [{ id: 1, slug: "sam", username: "sam", display_name: "Sam", enabled: true }] }),
  useRun: () => ({ data: state.run, refetch: state.refetch }),
}));
vi.mock("@/lib/sse", () => ({ useSSE: (handlers: SSEHandlers) => { state.handlers = handlers; } }));
function mount(id?: number) {
  const update = vi.fn();
  render(<QueryClientProvider client={new QueryClient()}><StepFirstRun data={{ first_run_id: id }} update={update} next={vi.fn()} complete={vi.fn()} /></QueryClientProvider>);
  return update;
}
beforeEach(() => { state.run = undefined; state.start.mockReset(); state.refetch.mockReset(); });
it("resumes the recorded run after reload and preserves each person's skipped result", () => {
  state.run = { id: 42, status: "ok", users: [{ slug: "sam", status: "skipped", picks: [], reason: "No row was due" }] };
  mount(42);
  expect(screen.queryByRole("button", { name: "Build my rows" })).not.toBeInTheDocument();
  expect(screen.getByText(/skipped — no row was due/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Finish setup" })).toBeInTheDocument();
  expect(screen.getByText("First run complete")).toBeInTheDocument();
  expect(screen.queryByText("Rows are live on Plex")).not.toBeInTheDocument();
});
it("ignores completion events belonging to another run", () => {
  mount(42);
  act(() => state.handlers.onRunFinished?.({ run_id: 99, status: "ok" }));
  expect(screen.queryByText("Rows are live on Plex")).not.toBeInTheDocument();
  expect(state.refetch).not.toHaveBeenCalled();
});
it("records the run identity immediately after start so reload cannot offer a duplicate build", async () => {
  state.start.mockResolvedValue({ run_id: 43 });
  const update = mount();
  await userEvent.click(screen.getByRole("button", { name: "Build my rows" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith({ first_run_id: 43 }));
});

it("restores a built person's measured pick count after reload", () => {
  state.run = { id: 42, status: "ok", users: [{ slug: "sam", status: "ok", picks: [{ id: 1 }, { id: 2 }], duration_ms: 2000, reason: null }] };
  mount(42);
  expect(screen.getByText("row built — 2 picks in 2s")).toBeInTheDocument();
});

it("describes cold-start picks without claiming they were delivered", () => {
  state.run = { id: 42, status: "ok", users: [{ slug: "sam", status: "cold_start", picks: [{ id: 1 }], reason: null }] };
  mount(42);
  expect(screen.getByText("popular-title picks — 1 found")).toBeInTheDocument();
  act(() => state.handlers.onRunUserStage?.({ run_id: 42, seq: 1, user: "sam", stage: "done", counts: { picks: 1 } }));
  expect(screen.getByText("First run complete")).toBeInTheDocument();
  expect(screen.queryByText("Rows are live on Plex")).not.toBeInTheDocument();
});

it("claims live rows only when the recorded result contains delivered or retained titles", () => {
  state.run = { id: 42, status: "ok", users: [{ slug: "sam", status: "cold_start", picks: [{ id: 1 }], diff: { added: ["A film"] } }] };
  mount(42);
  expect(screen.getByText("Rows are live on Plex")).toBeInTheDocument();
});
