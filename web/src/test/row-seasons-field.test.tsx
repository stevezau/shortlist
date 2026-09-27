import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RowSeasonsField } from "@/components/rows/row-seasons-field";
import { seasonDate } from "@/lib/seasons";

const CATALOGUE = [
  { slug: "valentines", name: "Valentine's Day", emoji: "💘", month: 2, day: 14, description: "Valentine's films and romance" },
  { slug: "halloween", name: "Halloween", emoji: "🎃", month: 10, day: 31, description: "Halloween films and horror" },
  { slug: "christmas", name: "Christmas", emoji: "🎄", month: 12, day: 25, description: "Christmas films" },
];

vi.mock("@/lib/api", () => ({
  api: { getSeasons: () => Promise.resolve(CATALOGUE) },
}));

type Value = { seasons: string[]; season_lead_days: number; season_after_days: number };

function renderField(
  value: Value,
  { schedule = "30 3 * * *", name = "{season_emoji} {season} picks", status = null as never } = {},
) {
  const onChange = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RowSeasonsField value={value} onChange={onChange} schedule={schedule} name={name} status={status} />
    </QueryClientProvider>,
  );
  return onChange;
}

const ON: Value = { seasons: ["halloween", "christmas"], season_lead_days: 30, season_after_days: 0 };

describe("RowSeasonsField", () => {
  // "Follow the calendar" is gone: whether a row is seasonal is its kind now, picked in the row
  // editor. Turning it on — every season, a month ahead — is tested there
  // (`row-editor-kinds.test.tsx`, "turning it on follows every season, a month ahead").
  it("has no on/off switch of its own, and writes nothing on its own", async () => {
    const onChange = renderField(ON);
    await screen.findByRole("checkbox", { name: /Halloween/ });
    expect(screen.queryByRole("switch")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("lists each season with its day and when it would show", async () => {
    renderField(ON);
    const halloween = await screen.findByRole("checkbox", { name: /Halloween/ });
    expect(halloween).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Valentine/ })).not.toBeChecked();
    expect(
      screen.getByText(new RegExp(`${seasonDate("2026-10-01")} – ${seasonDate("2026-10-31")}`)),
    ).toBeInTheDocument();
    expect(screen.getByText(/Halloween films and horror/)).toBeInTheDocument();
  });

  it("adds a season in calendar order", async () => {
    const onChange = renderField(ON);
    await userEvent.click(await screen.findByRole("checkbox", { name: /Valentine/ }));
    expect(onChange).toHaveBeenCalledWith({ seasons: ["valentines", "halloween", "christmas"] });
  });

  it("will not untick the last season, which would make it a row that follows none", async () => {
    const onChange = renderField({ ...ON, seasons: ["christmas"] });
    await userEvent.click(await screen.findByRole("checkbox", { name: /Christmas/ }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("keeps the days before inside the range the server accepts", async () => {
    const onChange = renderField(ON);
    const lead = screen.getByLabelText(/days before/i);
    await userEvent.clear(lead);
    await userEvent.type(lead, "400");
    expect(onChange).toHaveBeenLastCalledWith({ season_lead_days: 90 });
  });

  it("warns when the row does not run every night", () => {
    renderField(ON, { schedule: "30 3 * * 0" });
    expect(screen.getByRole("status")).toHaveTextContent(/nightly/i);
  });

  it("suggests putting the season in the name when it is not there", () => {
    renderField(ON, { name: "Picks for {user}" });
    expect(screen.getByText(/\{season\}/)).toBeInTheDocument();
  });

  it("says where a saved row is in its calendar, from the server", () => {
    renderField(ON, {
      status: {
        showing: null,
        next: { slug: "christmas", name: "Christmas", emoji: "🎄", starts: "2026-11-25", ends: "2026-12-25" },
      } as never,
    });
    expect(screen.getByText(`Hidden until 🎄 Christmas starts on ${seasonDate("2026-11-25")}`)).toBeInTheDocument();
  });
});
