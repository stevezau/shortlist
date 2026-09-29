import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";
import type * as WizardModule from "@/lib/wizard";
import { SetupPage } from "@/pages/setup";
vi.mock("@/lib/queries", () => ({
  queryKeys: {}, useSession: () => ({ data: { authenticated: true, login_required: true } }),
  useSetupState: () => ({ data: { completed: false } }),
}));
vi.mock("@/lib/wizard", async (importOriginal) => ({
  ...await importOriginal<typeof WizardModule>(),
  useWizard: () => ({ loaded: true, step: 5, data: {}, next: vi.fn(), back: vi.fn(), update: vi.fn(), complete: vi.fn(), canProceed: true }),
}));
vi.mock("@/pages/setup/step-customize", () => ({ StepCustomize: ({ back }: { back: () => void }) => <><button onClick={back}>Back</button><button>Save &amp; continue</button></> }));
it("has one forward action on customization so Next cannot discard the draft", () => {
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><SetupPage /></MemoryRouter></QueryClientProvider>);
  expect(screen.getByRole("button", { name: "Save & continue" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
  expect(screen.getByText("Step 6 of 7")).toBeInTheDocument();
});
