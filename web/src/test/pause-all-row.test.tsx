import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";
import { PauseAllRow } from "@/components/settings/pause-all-row";
import { api } from "@/lib/api";

it("keeps Pause all available and explains a failed pause without claiming success", async () => {
  const save = vi.spyOn(api, "putSettings").mockRejectedValue(new Error("offline"));
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}><MemoryRouter><PauseAllRow settings={{ paused_all: false }} /></MemoryRouter></QueryClientProvider>);
  await userEvent.click(screen.getByRole("button", { name: "Pause all" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t change/i);
  expect(screen.getByRole("button", { name: "Pause all" })).toBeEnabled();
  expect(screen.queryByText("Processing paused.", { exact: false })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(save).toHaveBeenCalledTimes(2);
  expect(save).toHaveBeenLastCalledWith({ paused_all: true });
  save.mockRestore();
});
