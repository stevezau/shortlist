/**
 * The sidebar's help block.
 *
 * It used to carry three actions — docs, "Report a bug", and "Copy diagnostics" — which asked the
 * person to already know that a bug report wants diagnostics attached and that the third button is
 * where those come from. Two of the three now live behind one "Have an issue?" door, alongside the
 * checks that answer most reports before they are filed; the copy behaviour itself is covered where
 * it now lives, in `issue-page.test.tsx`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { HelpLinks } from "@/components/layout/app-shell";
import type * as ApiModule from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getVersion: vi.fn().mockResolvedValue({ version: "1.2.3" }),
    },
  };
});

function renderLinks() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <HelpLinks />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("HelpLinks", () => {
  it("offers one door for problems, routed in-app rather than out to GitHub", () => {
    // In-app on purpose: the page runs the checks first and only then offers to file a report.
    // Sending someone straight to GitHub skips every answer we could have given them.
    renderLinks();

    const issue = screen.getByRole("link", { name: /have an issue\?/i });
    expect(issue.getAttribute("href")).toBe("/issue");
  });

  it("still links out to the docs", () => {
    renderLinks();

    const docs = screen.getByRole("link", { name: /help & docs/i });
    expect(docs.getAttribute("href")).toContain("github.com");
    expect(docs.getAttribute("target")).toBe("_blank");
  });

  it("no longer carries the bug-report and diagnostics actions itself", () => {
    // Both moved onto the issue page. Left here they were a second, competing entry point — and the
    // one that produced reports with no diagnostics attached.
    renderLinks();

    expect(screen.queryByRole("link", { name: /report a bug/i })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /copy diagnostics/i }),
    ).toBeNull();
  });
});
