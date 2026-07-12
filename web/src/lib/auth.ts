import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "./api";
import type { PinStatus } from "./types";

export type AppArea = "login" | "setup" | "app";

/** Where a visitor belongs, given auth + setup state. Pure — unit-tested. */
export function resolveArea(
  authenticated: boolean,
  setupCompleted: boolean,
): AppArea {
  if (!authenticated) return "login";
  if (!setupCompleted) return "setup";
  return "app";
}

/** The plex.tv auth-app URL that auto-completes the PIN for the user. */
export function plexAuthUrl(clientId: string, code: string): string {
  return (
    "https://app.plex.tv/auth#?" +
    `clientID=${encodeURIComponent(clientId)}` +
    `&code=${encodeURIComponent(code)}` +
    "&context%5Bdevice%5D%5Bproduct%5D=Rowarr"
  );
}

const POLL_INTERVAL_MS = 2_000;
const PIN_TIMEOUT_MS = 5 * 60 * 1000;

export type PinPhase = "idle" | "waiting" | "linked" | "error";

export interface PlexPinState {
  phase: PinPhase;
  /** 4-char code for the plex.tv/link fallback, shown while waiting. */
  code: string | null;
  /** True when window.open was blocked — show the code-entry fallback. */
  popupBlocked: boolean;
  error: string | null;
  /** Set once linked. The token inside stays in memory only — never persist it. */
  status: PinStatus | null;
  start: () => void;
}

/**
 * "Login with Plex" PIN flow: create a pin, open the plex.tv auth popup, and
 * poll every 2s until the account links (or 5 minutes pass). The scoped token
 * returned on link is handed to `onLinked` and kept in component memory only.
 */
export function usePlexPin(
  onLinked?: (status: PinStatus) => void,
): PlexPinState {
  const [phase, setPhase] = useState<PinPhase>("idle");
  const [code, setCode] = useState<string | null>(null);
  const [popupBlocked, setPopupBlocked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<PinStatus | null>(null);

  const onLinkedRef = useRef(onLinked);
  onLinkedRef.current = onLinked;

  const timersRef = useRef<{ poll: ReturnType<typeof setInterval> | null }>({
    poll: null,
  });
  const disposedRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (timersRef.current.poll !== null) {
      clearInterval(timersRef.current.poll);
      timersRef.current.poll = null;
    }
  }, []);

  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      stopPolling();
    };
  }, [stopPolling]);

  const start = useCallback(() => {
    stopPolling();
    setPhase("waiting");
    setError(null);
    setStatus(null);
    setPopupBlocked(false);

    void (async () => {
      let pin;
      try {
        pin = await api.createPin();
      } catch (caught) {
        if (disposedRef.current) return;
        setPhase("error");
        setError(
          caught instanceof ApiError
            ? caught.message
            : "Could not start the Plex login. Try again.",
        );
        return;
      }
      if (disposedRef.current) return;

      setCode(pin.code);
      const popup = window.open(
        plexAuthUrl(pin.client_id, pin.code),
        "rowarr-plex-auth",
        "width=600,height=720",
      );
      if (!popup) setPopupBlocked(true);

      const startedAt = Date.now();
      let inFlight = false;
      timersRef.current.poll = setInterval(() => {
        if (inFlight) return;
        if (Date.now() - startedAt > PIN_TIMEOUT_MS) {
          stopPolling();
          setPhase("error");
          setError("The Plex link timed out. Start the login again.");
          return;
        }
        inFlight = true;
        api
          .getPin(pin.id)
          .then((pinStatus) => {
            if (disposedRef.current) return;
            if (pinStatus.linked) {
              stopPolling();
              setStatus(pinStatus);
              setPhase("linked");
              onLinkedRef.current?.(pinStatus);
            }
          })
          .catch(() => {
            // Transient poll failure — keep polling until the timeout.
          })
          .finally(() => {
            inFlight = false;
          });
      }, POLL_INTERVAL_MS);
    })();
  }, [stopPolling]);

  return { phase, code, popupBlocked, error, status, start };
}
