import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CronInput } from "@/components/cron-input";

/** The shared "Custom" schedule box. Its whole job is that nothing is committed on trust, so the
 *  branches that matter are: what it shows back, and when it does (and doesn't) call onChange. */
describe("CronInput", () => {
  it("explains what it accepts before anything is typed", () => {
    render(<CronInput value="" onChange={vi.fn()} />);
    expect(screen.getByText(/plain English/i)).toBeInTheDocument();
  });

  it("keeps explaining itself when a schedule is already set", () => {
    // The box normally opens with a cron in it, so a hint shown only on an empty field is a hint
    // nobody ever sees — which is exactly how it shipped the first time.
    render(<CronInput value="17 */4 * * *" onChange={vi.fn()} />);
    expect(screen.getByText(/plain English/i)).toBeInTheDocument();
  });

  it("converts plain English and says what will happen", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<CronInput value="" onChange={onChange} />);

    await user.type(screen.getByRole("textbox"), "every 4 hours");

    // The meaning is shown BEFORE committing — that is the point of the field.
    expect(screen.getByText(/Every 4 hours/)).toBeInTheDocument();
    expect(screen.getByText("0 */4 * * *")).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();

    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith("0 */4 * * *");
    // The box now shows the cron it saved, not the English that produced it.
    expect(screen.getByRole("textbox")).toHaveValue("0 */4 * * *");
  });

  it("accepts a cron expression typed directly and describes it", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<CronInput value="" onChange={onChange} />);

    await user.type(screen.getByRole("textbox"), "17 */4 * * *");
    expect(
      screen.getByText(/Every 4 hours, at 17 minutes past/),
    ).toBeInTheDocument();

    await user.tab();
    expect(onChange).toHaveBeenCalledWith("17 */4 * * *");
  });

  it("refuses to save something it cannot read", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<CronInput value="" onChange={onChange} />);

    await user.type(screen.getByRole("textbox"), "whenever");
    expect(
      screen.getByText(/Not a schedule we recognise/i),
    ).toBeInTheDocument();

    await user.tab();
    // A typo used to save fine and then be silently replaced by the scheduler's default.
    expect(onChange).not.toHaveBeenCalled();
  });

  it("opens on the schedule already saved", () => {
    render(<CronInput value="30 3 * * *" onChange={vi.fn()} />);
    expect(screen.getByRole("textbox")).toHaveValue("30 3 * * *");
    expect(screen.getByText(/Every day at 3:30 AM/)).toBeInTheDocument();
  });
});
