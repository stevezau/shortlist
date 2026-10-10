import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { UserDetailHeader } from "@/components/user-detail/user-detail-header";
import type { User } from "@/lib/types";
import { makeUser } from "@/test/user-fixtures";

const SARAH: User = makeUser({ id: 4, history_depth: 120 });

function renderHeader(user: User) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UserDetailHeader user={user} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UserDetailHeader picks watched", () => {
  it("says how many picks were watched in 30 days", () => {
    renderHeader({ ...SARAH, picks_watched_30d: 3 });
    expect(screen.getByText(/3 picks watched in\s+30 days/)).toBeInTheDocument();
  });

  it("uses the singular for exactly one pick", () => {
    renderHeader({ ...SARAH, picks_watched_30d: 1 });
    expect(screen.getByText(/1 pick watched in\s+30 days/)).toBeInTheDocument();
  });

  it("says 0 picks rather than dropping the figure when none were watched lately", () => {
    renderHeader({ ...SARAH, picks_watched_30d: 0 });
    expect(screen.getByText(/0 picks watched in\s+30 days/)).toBeInTheDocument();
  });

  it("leaves the figure out for someone who has never had a pick", () => {
    renderHeader(SARAH);
    expect(screen.queryByText(/picks? watched/)).toBeNull();
  });
});
