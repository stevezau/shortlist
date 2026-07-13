import type { UseQueryResult } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { QueryBoundary } from "@/components/query-boundary";
import { ApiError } from "@/lib/api";

// The boundary only reads a handful of fields off the query result; build just those and cast,
// rather than standing up a real QueryClient for a pure branch test.
function queryResult<T>(
  partial: Partial<UseQueryResult<T>>,
): UseQueryResult<T> {
  return {
    isPending: false,
    isError: false,
    refetch: vi.fn(),
    ...partial,
  } as unknown as UseQueryResult<T>;
}

function renderBoundary<T>(
  query: UseQueryResult<T>,
  extra: Partial<{
    isEmpty: (data: T) => boolean;
    empty: React.ReactNode;
  }> = {},
) {
  render(
    <QueryBoundary
      query={query}
      skeleton={<div data-testid="skeleton" />}
      isEmpty={extra.isEmpty}
      empty={extra.empty}
    >
      {(data) => <div data-testid="content">{String(data)}</div>}
    </QueryBoundary>,
  );
}

describe("QueryBoundary", () => {
  it("renders the skeleton while pending", () => {
    renderBoundary(queryResult<string>({ isPending: true }));

    expect(screen.getByTestId("skeleton")).toBeInTheDocument();
    expect(screen.queryByTestId("content")).not.toBeInTheDocument();
  });

  it("renders an error alert with a retry that refetches", async () => {
    const refetch = vi.fn();
    renderBoundary(
      queryResult<string>({
        isError: true,
        error: new ApiError(502, "Upstream is down"),
        refetch,
      }),
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Upstream is down");
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("renders the empty node when isEmpty reports the data empty", () => {
    renderBoundary(queryResult<string[]>({ data: [] }), {
      isEmpty: (data) => data.length === 0,
      empty: <div data-testid="empty" />,
    });

    expect(screen.getByTestId("empty")).toBeInTheDocument();
    expect(screen.queryByTestId("content")).not.toBeInTheDocument();
  });

  it("renders children with the data on success", () => {
    renderBoundary(queryResult<string>({ data: "loaded" }));

    expect(screen.getByTestId("content")).toHaveTextContent("loaded");
  });

  it("renders children (not empty) when isEmpty is false", () => {
    renderBoundary(queryResult<string[]>({ data: ["one"] }), {
      isEmpty: (data) => data.length === 0,
      empty: <div data-testid="empty" />,
    });

    expect(screen.getByTestId("content")).toBeInTheDocument();
    expect(screen.queryByTestId("empty")).not.toBeInTheDocument();
  });
});
