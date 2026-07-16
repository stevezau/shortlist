import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { UserCard, type UserCardProps } from "@/components/user-card";
import type { User } from "@/lib/types";

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1,
    username: "Sarah",
    slug: "sarah",
    user_type: "shared",
    enabled: true,
    cold_start: false,
    history_depth: 342,
    last_run_at: new Date(Date.now() - 6 * 3600 * 1000).toISOString(),
    request_tag: "",
    hit_rate: 0.4,
    ...overrides,
  };
}

function renderCard(props: Partial<UserCardProps> = {}) {
  const defaults: UserCardProps = {
    user: makeUser(),
    activeStage: null,
    runPending: false,
    onRunNow: vi.fn(),
    onToggleEnabled: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <UserCard {...merged} />
    </MemoryRouter>,
  );
  return merged;
}

describe("UserCard", () => {
  it("shows the refreshed-ago status and hit rate for a healthy enabled user", () => {
    renderCard();

    expect(screen.getByText("Sarah")).toBeInTheDocument();
    expect(screen.getByText(/Row refreshed 6h ago/)).toBeInTheDocument();
    expect(screen.getByText("40% watched")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /run now/i })).toBeEnabled();
    expect(
      screen.getByRole("switch", { name: /shortlist row for sarah/i }),
    ).toBeChecked();
  });

  it("hides the hit rate badge before the first measurement", () => {
    renderCard({ user: makeUser({ hit_rate: null }) });

    expect(screen.queryByText(/hit rate/)).not.toBeInTheDocument();
  });

  it("flags cold-start users and explains the fallback row", () => {
    renderCard({ user: makeUser({ cold_start: true }) });

    expect(screen.getByText("cold start")).toBeInTheDocument();
    expect(screen.getByText(/thin history/i)).toBeInTheDocument();
  });

  it('shows "never run yet" when the user has no runs', () => {
    renderCard({ user: makeUser({ last_run_at: null }) });

    expect(screen.getByText(/never run yet/i)).toBeInTheDocument();
  });

  it("shows the live stage and disables Run now while a run is in flight", () => {
    renderCard({ activeStage: "curating" });

    expect(screen.getByText("Running: curating with AI…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /run now/i })).toBeDisabled();
  });

  it("disables Run now and dims the card for a disabled user", () => {
    renderCard({ user: makeUser({ enabled: false }) });

    expect(screen.getByText(/turned off/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /run now/i })).toBeDisabled();
    expect(
      screen.getByRole("switch", { name: /shortlist row for sarah/i }),
    ).not.toBeChecked();
  });

  it("calls onRunNow with the user when Run now is clicked", async () => {
    const user = userEvent.setup();
    const { onRunNow, user: cardUser } = renderCard();

    await user.click(screen.getByRole("button", { name: /run now/i }));

    expect(onRunNow).toHaveBeenCalledExactlyOnceWith(cardUser);
  });

  it("calls onToggleEnabled with the flipped state when the switch is toggled", async () => {
    const user = userEvent.setup();
    const { onToggleEnabled, user: cardUser } = renderCard();

    await user.click(
      screen.getByRole("switch", { name: /shortlist row for sarah/i }),
    );

    expect(onToggleEnabled).toHaveBeenCalledExactlyOnceWith(cardUser, false);
  });
});
