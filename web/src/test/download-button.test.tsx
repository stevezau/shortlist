import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DownloadButton } from "@/components/download-button";

/**
 * The save path itself, which the page-level tests do not reach.
 *
 * Those assert the "Preparing…" label and nothing else — mutation-tested, deleting the `response.ok`
 * guard OR the whole createObjectURL/click mechanism left them green. So the button could spin
 * convincingly and never download anything, or cheerfully save an error page, and the suite would
 * have agreed.
 */
function zip(body = "PK", headers: Record<string, string> = {}) {
  // String body, NOT a Blob: CI's Node rejects a Blob as a Response body with
  // "object.stream is not a function", while a newer local Node accepts it.
  return new Response(body, {
    status: 200,
    headers: { "content-type": "application/zip", ...headers },
  });
}

let clicked: HTMLAnchorElement[] = [];

function captureAnchorClicks() {
  clicked = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push(this);
  });
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob:fake"),
    revokeObjectURL: vi.fn(),
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DownloadButton", () => {
  it("actually saves the file it fetched", async () => {
    captureAnchorClicks();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(zip()));

    render(
      <DownloadButton url="/api/system/logs/download" filename="fallback.zip">
        Download .zip
      </DownloadButton>,
    );
    await userEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("uses the name the server asked for, not the one hardcoded here", async () => {
    // The logs endpoint stamps the time into its filename. Hardcoding a client-side name turned two
    // captures a day apart into "shortlist-logs.zip" and "shortlist-logs (1).zip", with nothing on
    // either saying when it was taken — and a blob: URL carries no Content-Disposition of its own.
    captureAnchorClicks();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        zip("PK", {
          "content-disposition":
            'attachment; filename="shortlist-logs-20260814.zip"',
        }),
      ),
    );

    render(
      <DownloadButton url="/api/system/logs/download" filename="fallback.zip">
        Download .zip
      </DownloadButton>,
    );
    await userEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(clicked[0]?.download).toBe("shortlist-logs-20260814.zip");
  });

  it("falls back to the given name when the server does not send one", async () => {
    captureAnchorClicks();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(zip()));

    render(
      <DownloadButton
        url="/api/support/report.zip"
        filename="shortlist-report.zip"
      >
        Download everything
      </DownloadButton>,
    );
    await userEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(clicked[0]?.download).toBe("shortlist-report.zip");
  });

  it("saves nothing when the request fails", async () => {
    captureAnchorClicks();
    // Carries a ZIP content-type on purpose, so only the status check can reject it — with a
    // text/plain body the content-type guard catches it and this says nothing about `response.ok`.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("nope", {
          status: 403,
          headers: { "content-type": "application/zip" },
        }),
      ),
    );

    render(
      <DownloadButton url="/api/support/report.zip" filename="r.zip">
        Download everything
      </DownloadButton>,
    );
    await userEvent.click(screen.getByRole("button"));

    expect(
      await screen.findByRole("button", { name: /try again/i }),
    ).toBeTruthy();
    expect(clicked).toHaveLength(0);
  });

  it("refuses an HTML page dressed as a download", async () => {
    // An auth proxy answers an expired session with a 302 to a same-origin login page, which
    // `redirect: "follow"` turns into a 200 of HTML. `response.ok` is true, so without a
    // content-type check the login page is saved as a .zip — and attached to a bug report.
    captureAnchorClicks();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<html>sign in</html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
      ),
    );

    render(
      <DownloadButton url="/api/system/logs/download" filename="logs.zip">
        Download .zip
      </DownloadButton>,
    );
    await userEvent.click(screen.getByRole("button"));

    expect(
      await screen.findByRole("button", { name: /try again/i }),
    ).toBeTruthy();
    expect(clicked).toHaveLength(0);
  });

  it("leaves no timer armed after it unmounts", async () => {
    // The revoke is deliberately delayed five seconds (a next-tick revoke raced a queued download
    // into "Failed - Network error" in Chrome). Fire-and-forget, that timer outlives the component
    // — and a callback touching `URL` after its environment is gone is an uncaught ReferenceError,
    // not a no-op. In CI it killed the suite process AFTER all 1283 tests passed: "window is not
    // defined", from processTimers, once in three runs.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      captureAnchorClicks();
      vi.stubGlobal("fetch", vi.fn(async () => zip()));
      const view = render(
        <DownloadButton url="/api/support/bundle" filename="bundle.zip">
          Download
        </DownloadButton>,
      );
      await userEvent.click(screen.getByRole("button", { name: /Download/i }));
      await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled());
      expect(vi.getTimerCount()).toBeGreaterThan(0);

      view.unmount();

      expect(vi.getTimerCount()).toBe(0);
      // ...and the blob is released rather than simply abandoned with its timer.
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake");
    } finally {
      vi.useRealTimers();
    }
  });
});
