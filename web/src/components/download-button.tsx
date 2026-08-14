import { Download } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";

/**
 * A download that says it is working.
 *
 * A plain `<a download>` hands the request to the browser and shows nothing at all while the server
 * builds the file. Both of ours zip the logs, which takes seconds on a busy instance — so the button
 * looked dead, and the honest reading of a dead button is that the click missed. People click again.
 *
 * Fetching the blob ourselves is what buys the spinner: the request is ours, so its lifetime is
 * ours to render. The cost is holding the file in memory before saving it, which is fine for a log
 * bundle and would not be for something huge.
 */
/** The name the SERVER asked for, from Content-Disposition — null when it did not ask. */
function filenameFrom(response: Response): string | null {
  const header = response.headers.get("content-disposition") ?? "";
  return /filename="?([^";]+)"?/.exec(header)?.[1] ?? null;
}

export function DownloadButton({
  url,
  filename,
  children,
  variant = "outline",
  className,
}: {
  url: string;
  filename: string;
  children: React.ReactNode;
  variant?: "outline" | "default" | "secondary";
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  const start = async () => {
    setBusy(true);
    setFailed(false);
    // Declared INSIDE the handler, never at component scope: a `let` declared during render and
    // mutated afterwards is what React Compiler's lint rules reject, and it would be shared across
    // renders besides.
    let href: string | null = null;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      // An auth proxy (Authelia, nginx auth_request) answers an expired session with a 302 to a
      // same-origin login page, which `redirect: "follow"` turns into a 200 of HTML. `ok` is true,
      // and without this the login page is saved as a .zip — and attached to a bug report. The old
      // anchor at least RENDERED that page, so the person could see what had happened.
      const kind = response.headers.get("content-type") ?? "";
      if (!kind.includes("zip") && !kind.includes("octet-stream")) {
        throw new Error(`unexpected content-type: ${kind}`);
      }
      const blob = await response.blob();
      href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      // The server's own name wins when it sends one: the logs endpoint stamps the time into it, and
      // hardcoding a name client-side turned two captures a day apart into "shortlist-logs.zip" and
      // "shortlist-logs (1).zip", with nothing on either saying when it was taken. A blob: URL
      // carries no Content-Disposition, so it has to be read off the response and reapplied.
      link.download = filenameFrom(response) ?? filename;
      // Appended before clicking: Chrome and Safari fire a detached anchor, Firefox historically did
      // not, and libraries still attach defensively.
      document.body.append(link);
      link.click();
      link.remove();
    } catch {
      // Deliberately not a raw status code — the one thing the person needs is that it did not
      // happen and pressing again is the fix.
      setFailed(true);
    } finally {
      setBusy(false);
      // Paired with the create in a `finally` so a throw in between cannot leak it. Seconds rather
      // than a tick: a next-tick revoke has been seen to race a queued large download into a
      // "Failed - Network error" in Chrome.
      if (href) {
        const created = href;
        setTimeout(() => URL.revokeObjectURL(created), 5_000);
      }
    }
  };

  return (
    <Button
      variant={variant}
      className={className}
      loading={busy}
      onClick={start}
      aria-live="polite"
    >
      {!busy && <Download aria-hidden="true" />}
      {busy
        ? "Preparing…"
        : failed
          ? "Couldn’t download — try again"
          : children}
    </Button>
  );
}
