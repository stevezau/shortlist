import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AudiencePicker } from "@/components/rows/audience-picker";
import type { User } from "@/lib/types";

function user(id: number, username: string): User {
  return {
    id,
    username,
    slug: username.toLowerCase(),
    user_type: "shared",
    enabled: true,
    cold_start: false,
    history_depth: 10,
    last_run_at: null,
    request_tag: "",
    hit_rate: null,
  };
}

describe("AudiencePicker", () => {
  it("keeps the people list behind a disclosure and expands on click", async () => {
    render(
      <AudiencePicker
        audience="subset"
        audienceUserIds={[4]}
        users={[user(4, "sarah"), user(5, "mike")]}
        onChange={vi.fn()}
      />,
    );

    // Collapsed on open: a one-line summary, individual user toggles hidden.
    const summary = screen.getByRole("button", {
      name: /1 of 2 people chosen/i,
    });
    expect(screen.queryByLabelText("sarah")).toBeNull();

    await userEvent.click(summary);
    expect(screen.getByLabelText("sarah")).toBeTruthy();
    expect(screen.getByLabelText("mike")).toBeTruthy();
  });

  it("warns in the summary when nobody is chosen", () => {
    render(
      <AudiencePicker
        audience="subset"
        audienceUserIds={[]}
        users={[user(4, "sarah")]}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/Nobody chosen/i)).toBeTruthy();
  });
});
