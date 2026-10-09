import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";
import { StepFirstRun } from "@/pages/setup/step-first-run";
import type { SSEHandlers } from "@/lib/sse";
const state = vi.hoisted(() => ({ header: vi.fn(), run: undefined as unknown, start: vi.fn(), handlers: {} as SSEHandlers, refetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiUrl: (path: string) => path, api: { startRun: () => state.start() }, apiErrorMessage: (_e: unknown, fallback: string) => fallback }));
vi.mock("@/lib/queries", () => ({
  useUsers: () => ({ data: [{ id: 1, slug: "sam", username: "sam", display_name: "Sam", enabled: true }] }),
  useRun: () => ({ data: state.run, refetch: state.refetch }),
}));
vi.mock("@/lib/sse", () => ({ useSSE: (handlers: SSEHandlers) => { state.handlers = handlers; } }));
function mount(id?: number) {
  const update = vi.fn();
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><StepFirstRun data={{ first_run_id: id }} update={update} next={vi.fn()} complete={vi.fn()} setHeader={state.header} /></MemoryRouter></QueryClientProvider>);
  return update;
}
beforeEach(() => { state.header.mockReset(); state.run = undefined; state.start.mockReset(); state.refetch.mockReset(); });
it("resumes the recorded run after reload and preserves each person's skipped result", () => {
  state.run = { id: 42, status: "ok", users: [{ slug: "sam", status: "skipped", picks: [], reason: "No row was due" }] };
  mount(42);
  expect(screen.queryByRole("button", { name: "Build my rows" })).not.toBeInTheDocument();
  expect(screen.getByText(/skipped — no row was due/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Go to dashboard" })).toBeInTheDocument();
  expect(state.header).toHaveBeenLastCalledWith(expect.objectContaining({ title: "First run complete" }));
  expect(state.header).not.toHaveBeenCalledWith(expect.objectContaining({ title: "Your rows are on Plex" }));
});
it("ignores completion events belonging to another run", () => {
  mount(42);
  act(() => state.handlers.onRunFinished?.({ run_id: 99, status: "ok" }));
  expect(screen.queryByText("Your rows are on Plex")).not.toBeInTheDocument();
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
  expect(state.header).toHaveBeenLastCalledWith(expect.objectContaining({ title: "First run complete" }));
  expect(state.header).not.toHaveBeenCalledWith(expect.objectContaining({ title: "Your rows are on Plex" }));
});

it("claims live rows only when the recorded result contains delivered or retained titles", () => {
  state.run = { id: 42, status: "ok", users: [{ slug: "sam", status: "cold_start", picks: [{ id: 1 }], diff: { added: ["A film"] } }] };
  mount(42);
  expect(state.header).toHaveBeenLastCalledWith(expect.objectContaining({ title: "Your rows are on Plex", stepLabel: "Step 7 of 7 · Done", badge: { text: "run ok", variant: "success" } }));
});

it("says who will get a row before the run, with no invented time estimate", () => {
  mount();
  expect(screen.getByText("1 person gets a row")).toBeInTheDocument();
  expect(screen.getByText("Sam")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Build my rows" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Skip for now/ })).toBeInTheDocument();
  expect(screen.queryByText(/minutes|seconds/)).not.toBeInTheDocument();
});

it("shows a poster strip of each built person's picks once the run is finished", () => {
  state.run = {
    id: 42, status: "ok",
    users: [{ slug: "sam", status: "ok", picks: [{ rank: 1, rating_key: 11 }, { rank: 2, rating_key: 12 }], diff: { added: ["A film"] } }],
  };
  mount(42);
  expect(screen.getByTestId("picks-sam").querySelectorAll("img")).toHaveLength(2);
});

it("replaces the unrecorded-person line with a one-line privacy note and a fix link", () => {
  state.run = {
    id: 42, status: "ok", began_at: "2026-10-09T10:00:00Z", finished_at: "2026-10-09T10:00:41Z",
    privacy: { can_see_others: ["sam"] },
    users: [{ slug: "other", username: "other", status: "ok", picks: [{ rank: 1, rating_key: 1 }], diff: { added: ["A"] } }],
  };
  mount(42);
  expect(screen.queryByText(/not recorded for this person/)).not.toBeInTheDocument();
  expect(screen.getByText("no row — see the privacy note below")).toBeInTheDocument();
  expect(screen.getByTestId("first-run-privacy")).toHaveTextContent("Sam");
  expect(screen.getByRole("button", { name: /Fix in Privacy/ })).toBeInTheDocument();
  expect(state.header).toHaveBeenLastCalledWith(expect.objectContaining({ why: expect.stringContaining("Built for 1 person in 41 seconds.") }));
});

const OK_RUN = { id: 42, status: "ok", users: [{ slug: "sam", username: "sam", display_name: "Sam", status: "ok", picks: [{ rank: 1, rating_key: 1 }], diff: { added: ["A"] } }] };

it("warns on the badge and in the callout when Plex is not applying the hide rules", () => {
  state.run = { ...OK_RUN, privacy: { can_see_others: [], unreadable_filters: [], filters_not_enforced: ["sam"] } };
  mount(42);
  expect(screen.getByTestId("first-run-privacy")).toHaveTextContent("Plex isn’t applying the hide rules on Sam");
  expect(state.header).toHaveBeenLastCalledWith(expect.objectContaining({ badge: { text: "run ok", variant: "warning" } }));
});

it("names an account whose hide rules could not be saved or checked", () => {
  state.run = { ...OK_RUN, privacy: { can_see_others: [], write_failed: ["sam"], unchecked: ["pat"] } };
  mount(42);
  const note = screen.getByTestId("first-run-privacy");
  expect(note).toHaveTextContent("Couldn’t save hide rules for Sam");
  expect(note).toHaveTextContent("Couldn’t check what pat can see");
  expect(state.header).toHaveBeenLastCalledWith(expect.objectContaining({ badge: { text: "run ok", variant: "warning" } }));
});

it("keeps the success badge and shows no privacy note when nothing was flagged", () => {
  state.run = { ...OK_RUN, privacy: { can_see_others: [], unreadable_filters: [], filters_not_enforced: [], write_failed: [], unchecked: [] } };
  mount(42);
  expect(screen.queryByTestId("first-run-privacy")).not.toBeInTheDocument();
  expect(state.header).toHaveBeenLastCalledWith(expect.objectContaining({ badge: { text: "run ok", variant: "success" } }));
});
