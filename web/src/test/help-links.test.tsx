import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HelpLinks } from "@/components/layout/app-shell";
import type * as ApiModule from "@/lib/api";

const { getDebugBundle } = vi.hoisted(() => ({ getDebugBundle: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getVersion: vi.fn().mockResolvedValue({ version: "1.2.3" }),
      getDebugBundle,
    },
  };
});

function renderLinks() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <HelpLinks />
    </QueryClientProvider>,
  );
}

describe("HelpLinks — Copy diagnostics", () => {
  beforeEach(() => {
    getDebugBundle.mockReset();
    Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
  });

  it("copies the secrets-free bundle to the clipboard on success", async () => {
    getDebugBundle.mockResolvedValue("shortlist diagnostics\nversion: 1.2.3");
    renderLinks();

    await userEvent.click(
      screen.getByRole("button", { name: /copy diagnostics/i }),
    );

    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        "shortlist diagnostics\nversion: 1.2.3",
      ),
    );
    expect(
      await screen.findByText(/copied — paste into the issue/i),
    ).toBeInTheDocument();
  });

  it("surfaces an error label when the bundle cannot be fetched", async () => {
    getDebugBundle.mockRejectedValue(new Error("500"));
    renderLinks();

    await userEvent.click(
      screen.getByRole("button", { name: /copy diagnostics/i }),
    );

    expect(
      await screen.findByText(/couldn’t copy — try again/i),
    ).toBeInTheDocument();
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
  });

  it("surfaces an error label when the clipboard write is blocked", async () => {
    getDebugBundle.mockResolvedValue("bundle");
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockRejectedValue(new Error("blocked")),
      },
    });
    renderLinks();

    await userEvent.click(
      screen.getByRole("button", { name: /copy diagnostics/i }),
    );

    expect(
      await screen.findByText(/couldn’t copy — try again/i),
    ).toBeInTheDocument();
  });
});
