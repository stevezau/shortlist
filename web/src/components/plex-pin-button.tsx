import { Check, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { usePlexPin } from "@/lib/auth";
import type { PinStatus } from "@/lib/types";

/**
 * "Login with Plex" button with the PIN fallback UI. Opens the plex.tv auth
 * popup; when popups are blocked it shows the 4-char code and a plex.tv/link
 * pointer instead. The token from the linked pin stays in memory only.
 */
export function PlexPinButton({
  label = "Login with Plex",
  onLinked,
}: {
  label?: string;
  onLinked?: (status: PinStatus) => void;
}) {
  const pin = usePlexPin(onLinked);

  if (pin.phase === "linked") {
    return (
      <p className="inline-flex items-center gap-2 text-sm text-success">
        <Check className="h-4 w-4" aria-hidden="true" />
        Linked as {pin.status?.username ?? "your Plex account"}
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <Button onClick={pin.start} disabled={pin.phase === "waiting"}>
        {pin.phase === "waiting" && (
          <Loader2 className="animate-spin" aria-hidden="true" />
        )}
        {pin.phase === "waiting" ? "Waiting for Plex…" : label}
      </Button>

      {pin.phase === "waiting" && pin.code && (
        <div className="rounded-md border bg-card p-3 text-sm">
          {pin.popupBlocked ? (
            <p>
              Your browser blocked the Plex popup. No problem — enter this code
              at{" "}
              <a
                href="https://plex.tv/link"
                target="_blank"
                rel="noreferrer"
                className="text-primary underline-offset-4 hover:underline"
              >
                plex.tv/link
              </a>
              :
            </p>
          ) : (
            <p className="text-muted-foreground">
              Approve the login in the Plex window. Popup didn't appear? Enter
              this code at{" "}
              <a
                href="https://plex.tv/link"
                target="_blank"
                rel="noreferrer"
                className="text-primary underline-offset-4 hover:underline"
              >
                plex.tv/link
              </a>
              :
            </p>
          )}
          <p className="mt-2 font-mono text-2xl font-bold tracking-[0.4em] text-primary">
            {pin.code}
          </p>
        </div>
      )}

      {pin.phase === "error" && (
        <p role="alert" className="text-sm text-destructive">
          {pin.error}
        </p>
      )}
    </div>
  );
}
