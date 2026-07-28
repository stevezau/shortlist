import { useCallback, useEffect, useRef, useState } from "react";

import { api, apiErrorMessage } from "./api";
import type { PinStatus } from "./types";

export type AppArea = "login" | "setup" | "app";

/**
 * Where a visitor belongs, given auth + setup state. Pure — unit-tested.
 *
 * An install with nothing worth protecting opens straight into the wizard: no Plex server linked,
 * no token, no users. Being asked to sign in before you can configure anything is a door with no
 * house behind it. Signing in with Plex is not a gate in front of setup; it IS a step of setup
 * ("connect your Plex server"), and it's the step that claims the instance.
 *
 * `loginRequired` is the SERVER's judgement of that, not ours — it is true the moment there is a
 * linked server OR a Plex token seeded from the environment, because an instance can hold a token
 * worth stealing while nobody has claimed it.
 */
export function resolveArea(
  authenticated: boolean,
  setupCompleted: boolean,
  loginRequired: boolean,
): AppArea {
  if (!authenticated) return loginRequired ? "login" : "setup";
  if (!setupCompleted) return "setup";
  return "app";
}

/** The plex.tv auth-app URL that auto-completes the PIN for the user. */
export function plexAuthUrl(clientId: string, code: string): string {
  return (
    "https://app.plex.tv/auth#?" +
    `clientID=${encodeURIComponent(clientId)}` +
    `&code=${encodeURIComponent(code)}` +
    "&context%5Bdevice%5D%5Bproduct%5D=Shortlist"
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
  /** Set once the account links. Carries no Plex token — the backend keeps that. */
  status: PinStatus | null;
  start: () => void;
}

/**
 * "Sign in with Plex" PIN flow: create a pin, open the plex.tv auth popup, and poll every 2s
 * until the account links (or 5 minutes pass). No Plex token ever reaches this code — the
 * backend mints it and keeps it, which is why the wizard does not ask you to sign in twice.
 */
export function usePlexPin(
  onLinked?: (status: PinStatus) => void,
): PlexPinState {
  const [phase, setPhase] = useState<PinPhase>("idle");
  const [code, setCode] = useState<string | null>(null);
  const [popupBlocked, setPopupBlocked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<PinStatus | null>(null);

  // Assigned in an effect rather than during render — see the note in lib/sse.ts. Safe because
  // onLinkedRef is only read from the pin-polling interval, which runs after the commit.
  const onLinkedRef = useRef(onLinked);
  useEffect(() => {
    onLinkedRef.current = onLinked;
  });

  const timersRef = useRef<{ poll: ReturnType<typeof setInterval> | null }>({
    poll: null,
  });
  const popupRef = useRef<Window | null>(null);
  const disposedRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (timersRef.current.poll !== null) {
      clearInterval(timersRef.current.poll);
      timersRef.current.poll = null;
    }
  }, []);

  // Close the plex.tv popup once we're done with it. It has served its purpose the moment the
  // account links, and leaving it open makes the user close it by hand — which reads as "did that
  // work?". Guarded because the browser may refuse .close() in some states.
  const closePopup = useCallback(() => {
    try {
      popupRef.current?.close();
    } catch {
      /* the browser may block closing a window in some states — harmless */
    }
    popupRef.current = null;
  }, []);

  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      stopPolling();
      closePopup();
    };
  }, [stopPolling, closePopup]);

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
          apiErrorMessage(caught, "Could not start the Plex login. Try again."),
        );
        return;
      }
      if (disposedRef.current) return;

      setCode(pin.code);
      popupRef.current = window.open(
        plexAuthUrl(pin.client_id, pin.code),
        "shortlist-plex-auth",
        "width=600,height=720",
      );
      if (!popupRef.current) setPopupBlocked(true);

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
              closePopup();
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
  }, [stopPolling, closePopup]);

  return { phase, code, popupBlocked, error, status, start };
}
