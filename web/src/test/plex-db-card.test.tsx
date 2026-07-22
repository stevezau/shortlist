import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PlexDbCard } from "@/components/settings/plex-db-card";
import type { Settings } from "@/lib/types";

// Capture the payload builder rather than discarding it: it is the ONLY real contract this card
// has, and a wrong settings key means the feature is silently off forever.
const payloads: Array<() => Record<string, unknown>> = [];
vi.mock("@/lib/autosave", () => ({
  useAutosavedSettings: (_deps: unknown, build: () => Record<string, unknown>) => {
    payloads.push(build);
    return { isPending: false, isError: false, error: null, saved: false, retry: vi.fn() };
  },
}));
vi.mock("@/lib/api", () => ({ api: { testConnection: vi.fn() } }));

function renderCard(settings: Settings = {} as Settings) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <PlexDbCard settings={settings} />
    </QueryClientProvider>,
  );
}

describe("PlexDbCard", () => {
  it("is empty by default — reading someone's Plex database is opt-in", () => {
    renderCard();

    expect(screen.getByLabelText(/Read watched state/i)).toHaveValue("");
  });

  it("shows the configured path", () => {
    renderCard({ "plex.db_path": "/plexdb" } as unknown as Settings);

    expect(screen.getByLabelText(/Read watched state/i)).toHaveValue("/plexdb");
  });

  it("says it is read-only, because that is the thing people will worry about", () => {
    renderCard();

    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is ever written to it/i)).toBeInTheDocument();
  });

  it("explains the constraint rather than letting someone set it and wonder", () => {
    renderCard();

    expect(
      screen.getByText(/same machine as Plex/i),
    ).toBeInTheDocument();
  });

  it("saves the path under the key the backend actually reads, trimmed", async () => {
    payloads.length = 0;
    renderCard();

    await userEvent.type(screen.getByLabelText(/Read watched state/i), "  /plexdb  ");

    expect(payloads.at(-1)!()).toEqual({ "plex.db_path": "/plexdb" });
  });

  it("cannot be tested until a path is entered", async () => {
    renderCard();

    expect(screen.getByRole("button", { name: /test/i })).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Read watched state/i), "/plexdb");
    expect(screen.getByRole("button", { name: /test/i })).toBeEnabled();
  });
});
