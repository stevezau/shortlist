import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  UNINSTALL_CONFIRM_PHRASE,
  UninstallDialog,
  type UninstallDialogProps,
} from "@/components/uninstall-dialog";

function renderDialog(props: Partial<UninstallDialogProps> = {}) {
  const defaults: UninstallDialogProps = {
    open: true,
    onOpenChange: vi.fn(),
    onConfirm: vi.fn(),
    pending: false,
    onPreview: vi.fn(),
    previewPending: false,
    preview: null,
  };
  const merged = { ...defaults, ...props };
  render(<UninstallDialog {...merged} />);
  return merged;
}

// Radix overlays set pointer-events on body; skip that check so clicks land.
const user = () => userEvent.setup({ pointerEventsCheck: 0 });

describe("UninstallDialog", () => {
  it("keeps the destructive button disabled until the exact phrase is typed", async () => {
    const u = user();
    renderDialog();
    const confirmButton = screen.getByRole("button", {
      name: /uninstall and restore server/i,
    });

    expect(confirmButton).toBeDisabled();

    await u.type(screen.getByLabelText(/type/i), "uninstall row");
    expect(confirmButton).toBeDisabled();
  });

  it("enables confirmation once the phrase matches and calls onConfirm on click", async () => {
    const u = user();
    const { onConfirm } = renderDialog();

    await u.type(screen.getByLabelText(/type/i), UNINSTALL_CONFIRM_PHRASE);
    const confirmButton = screen.getByRole("button", {
      name: /uninstall and restore server/i,
    });
    expect(confirmButton).toBeEnabled();

    await u.click(confirmButton);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("accepts the phrase case-insensitively with surrounding whitespace", async () => {
    const u = user();
    renderDialog();

    await u.type(screen.getByLabelText(/type/i), "  Uninstall Shortlist ");

    expect(
      screen.getByRole("button", { name: /uninstall and restore server/i }),
    ).toBeEnabled();
  });

  it("disables both actions while the uninstall request is pending", async () => {
    const u = user();
    renderDialog({ pending: true });

    await u.type(screen.getByLabelText(/type/i), UNINSTALL_CONFIRM_PHRASE);

    expect(
      screen.getByRole("button", { name: /uninstall and restore server/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /keep shortlist/i }),
    ).toBeDisabled();
  });

  it("shows live progress while running so a long wait doesn't read as frozen", () => {
    renderDialog({
      pending: true,
      preview: {
        filters_restored: 48,
        collections_deleted: ["a", "b"],
        rows_disabled: 1,
        dry_run: true,
        message: "Preview only — nothing was changed.",
      },
    });

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/Restoring share filters/i);
    expect(status).toHaveTextContent(/48 user share filters/i);
    expect(status).toHaveTextContent(/about one per second/i); // sets the expectation of a wait
    expect(status).toHaveTextContent(/elapsed/i); // the live timer
  });

  it("requests a dry-run preview and renders what would change", async () => {
    const u = user();
    const { onPreview } = renderDialog({
      preview: {
        filters_restored: 3,
        collections_deleted: ["✨ Picked for You (sarah)"],
        rows_disabled: 2,
        dry_run: true,
        message: "Preview only — nothing was changed.",
      },
    });

    await u.click(
      screen.getByRole("button", { name: /preview what would change/i }),
    );
    expect(onPreview).toHaveBeenCalledOnce();

    expect(
      screen.getByText(/3 share filters restored · 1 collection deleted/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/✨ Picked for You \(sarah\)/)).toBeInTheDocument();
    expect(
      screen.getByText(/preview only — nothing was changed\./i),
    ).toBeInTheDocument();
  });

  it("explains what uninstall does — and does not promise a config wipe", () => {
    renderDialog();

    expect(
      screen.getByText(/restores each user's share filters/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    expect(screen.queryByText(/local config/i)).not.toBeInTheDocument();
  });
});
