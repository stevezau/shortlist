import { Check, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { usePlexPin } from "@/lib/auth";
import type { PinStatus } from "@/lib/types";

function PlexMark({ className }: { className?: string }) {
  // The Plex chevron, inline so the button reads as a real "sign in with Plex" affordance.
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={className}
      fill="currentColor"
    >
      <path d="M4 2h6.4l7 10-7 10H4l7-10L4 2Z" />
    </svg>
  );
}

/**
 * "Sign in with Plex" — opens the plex.tv auth popup and waits for you to approve.
 *
 * The 4-character code is a FALLBACK, shown only when the browser blocked the popup. In the normal
 * flow the popup opens, you approve, and it closes itself — there is no reason to put a code on
 * screen, so we don't.
 */
export function PlexPinButton({
  label = "Sign in with Plex",
  onLinked,
}: {
  label?: string;
  onLinked?: (status: PinStatus) => void;
}) {
  const pin = usePlexPin(onLinked);

  if (pin.phase === "linked") {
    return (
      <p className="inline-flex items-center gap-2 text-sm font-medium text-success">
        <Check className="h-4 w-4" aria-hidden="true" />
        Signed in as {pin.status?.username ?? "your Plex account"}
      </p>
    );
  }

  const waiting = pin.phase === "waiting";

  return (
    <div className="space-y-3">
      <Button
        onClick={pin.start}
        disabled={waiting}
        size="lg"
        className="w-full gap-2 bg-plex text-plex-foreground hover:bg-plex/90 focus-visible:ring-plex/50"
      >
        {waiting ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : (
          <PlexMark className="h-4 w-4" />
        )}
        {waiting ? "Waiting for Plex…" : label}
      </Button>

      {waiting && !pin.popupBlocked && (
        <div className="space-y-2 text-center">
          <p className="text-sm text-muted-foreground">
            A Plex window opened — approve the login there and this continues on
            its own. No need to wait on this screen; it updates the moment Plex
            confirms.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={pin.start}
            className="gap-2"
          >
            <PlexMark className="h-3.5 w-3.5" />
            Reopen the Plex window
          </Button>
        </div>
      )}

      {waiting && pin.popupBlocked && pin.code && (
        <div className="rounded-lg border bg-muted/40 p-4 text-center text-sm">
          <p className="text-muted-foreground">
            Your browser blocked the Plex popup. Enter this code at{" "}
            <a
              href="https://plex.tv/link"
              target="_blank"
              rel="noreferrer"
              className="font-medium text-primary underline-offset-4 hover:underline"
            >
              plex.tv/link
            </a>
          </p>
          <p className="mt-3 font-mono text-3xl font-semibold tracking-[0.3em] text-foreground">
            {pin.code}
          </p>
        </div>
      )}

      {pin.phase === "error" && (
        <p role="alert" className="text-sm text-destructive-text">
          {pin.error}
        </p>
      )}
    </div>
  );
}
