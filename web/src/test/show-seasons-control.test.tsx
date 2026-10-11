import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ShowSeasonsControl } from "@/components/show-seasons-control";
import type { SonarrMonitor } from "@/lib/sonarr-monitor";

function renderControl(
  mode: SonarrMonitor,
  count: number,
  target: "arr" | "overseerr" = "arr",
) {
  const onChange = vi.fn();
  render(
    <ShowSeasonsControl
      mode={mode}
      count={count}
      target={target}
      onChange={onChange}
    />,
  );
  return onChange;
}

describe("ShowSeasonsControl", () => {
  it("offers the pilot and unmonitored choices only when requests go straight to Sonarr", () => {
    const { unmount } = render(
      <ShowSeasonsControl
        mode="firstSeason"
        count={1}
        target="arr"
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Pilot episode" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add unmonitored" })).toBeInTheDocument();
    unmount();

    render(
      <ShowSeasonsControl
        mode="firstSeason"
        count={1}
        target="overseerr"
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "First seasons" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pilot episode" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add unmonitored" })).toBeNull();
  });

  it("says where later seasons come from on the Overseerr route only", () => {
    renderControl("firstSeason", 1, "overseerr");
    expect(screen.getByText(/Monitor New Seasons/)).toBeInTheDocument();
  });

  it("does not mention Overseerr's new-season option on the Sonarr route", () => {
    renderControl("firstSeason", 1, "arr");
    expect(screen.queryByText(/Monitor New Seasons/)).toBeNull();
    expect(
      screen.getByText(/picked up only with Every season/),
    ).toBeInTheDocument();
  });

  it("reports the picked start with the count kept", async () => {
    const onChange = renderControl("firstSeason", 3);
    await userEvent.click(screen.getByRole("button", { name: "Last seasons" }));
    expect(onChange).toHaveBeenCalledWith({ mode: "lastSeason", count: 3 });
  });

  it("steps the count up and down", async () => {
    const onChange = renderControl("lastSeason", 2);
    await userEvent.click(screen.getByRole("button", { name: "More seasons" }));
    expect(onChange).toHaveBeenLastCalledWith({ mode: "lastSeason", count: 3 });
    await userEvent.click(screen.getByRole("button", { name: "Fewer seasons" }));
    expect(onChange).toHaveBeenLastCalledWith({ mode: "lastSeason", count: 1 });
  });

  it("cannot step below 1 or above 99", () => {
    const { unmount } = render(
      <ShowSeasonsControl mode="firstSeason" count={1} target="arr" onChange={() => {}} />,
    );
    expect(screen.getByRole("button", { name: "Fewer seasons" })).toBeDisabled();
    unmount();
    render(
      <ShowSeasonsControl mode="firstSeason" count={99} target="arr" onChange={() => {}} />,
    );
    expect(screen.getByRole("button", { name: "More seasons" })).toBeDisabled();
  });

  it("ticking Every season sends mode all and disables the stepper", async () => {
    const onChange = renderControl("firstSeason", 2);
    await userEvent.click(screen.getByRole("checkbox", { name: "Every season" }));
    expect(onChange).toHaveBeenCalledWith({ mode: "all", count: 2 });
  });

  it("with Every season ticked the stepper is off", () => {
    renderControl("all", 1);
    expect(screen.getByRole("checkbox", { name: "Every season" })).toBeChecked();
    expect(screen.getByRole("button", { name: "More seasons" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Fewer seasons" })).toBeDisabled();
  });

  it("defaults to First seasons when unticked from a fresh Every season", async () => {
    const onChange = renderControl("all", 1);
    await userEvent.click(screen.getByRole("checkbox", { name: "Every season" }));
    expect(onChange).toHaveBeenCalledWith({ mode: "firstSeason", count: 1 });
  });

  it("while Every season is ticked, picking a start is remembered rather than sent", async () => {
    const onChange = renderControl("all", 1);
    await userEvent.click(screen.getByRole("button", { name: "Last seasons" }));
    expect(onChange).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("checkbox", { name: "Every season" }));
    expect(onChange).toHaveBeenCalledWith({ mode: "lastSeason", count: 1 });
  });

  it("describes the count in the hint", () => {
    renderControl("firstSeason", 3);
    expect(screen.getByText(/Seasons 1 to 3\./)).toBeInTheDocument();
  });

  it("warns that a stored pilot choice will not be sent through Overseerr", () => {
    renderControl("pilot", 1, "overseerr");
    expect(screen.getByRole("alert")).toHaveTextContent(/won’t be sent/);
    expect(screen.getByRole("alert")).toHaveTextContent(/first or last seasons/);
  });

  it("warns the same for add-unmonitored, and not at all on the Sonarr route", () => {
    const { unmount } = render(
      <ShowSeasonsControl mode="none" count={1} target="overseerr" onChange={() => {}} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/Add unmonitored/);
    unmount();
    render(<ShowSeasonsControl mode="none" count={1} target="arr" onChange={() => {}} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
