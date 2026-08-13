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
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = filename;
      link.click();
      // Freed on the next tick rather than immediately: revoking synchronously can cancel the save
      // in some browsers before the click has been acted on.
      setTimeout(() => URL.revokeObjectURL(href), 0);
    } catch {
      // Deliberately not a raw status code — the one thing the person needs is that it did not
      // happen and pressing again is the fix.
      setFailed(true);
    } finally {
      setBusy(false);
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
