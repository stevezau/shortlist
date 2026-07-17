import { useEffect, useRef, useState } from "react";

import { eventsUrl } from "./api";
import type {
  RunFinishedEvent,
  RunUserStageEvent,
  UninstallProgressEvent,
} from "./types";

export interface SSEHandlers {
  onRunUserStage?: (event: RunUserStageEvent) => void;
  onRunFinished?: (event: RunFinishedEvent) => void;
  onUninstallProgress?: (event: UninstallProgressEvent) => void;
}

const INITIAL_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;

function parseData<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

/**
 * Subscribe to Shortlist's shared event stream (`GET /api/events`).
 *
 * One EventSource per page: call this once at page level and fan the events
 * out via the handlers — never once per widget (rules/frontend.md).
 * Reconnects automatically with exponential backoff after connection loss.
 */
export function useSSE(handlers: SSEHandlers): { connected: boolean } {
  const [connected, setConnected] = useState(false);

  // Keep the latest handlers in a ref so callers can pass inline objects
  // without tearing down and re-opening the connection every render.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryMs = INITIAL_RETRY_MS;
    let disposed = false;

    const connect = (): void => {
      if (disposed) return;
      source = new EventSource(eventsUrl());

      source.onopen = () => {
        retryMs = INITIAL_RETRY_MS;
        setConnected(true);
      };

      source.addEventListener(
        "run.user.stage",
        (event: MessageEvent<string>) => {
          const data = parseData<RunUserStageEvent>(event.data);
          if (data) handlersRef.current.onRunUserStage?.(data);
        },
      );

      source.addEventListener("run.finished", (event: MessageEvent<string>) => {
        const data = parseData<RunFinishedEvent>(event.data);
        if (data) handlersRef.current.onRunFinished?.(data);
      });

      source.addEventListener(
        "uninstall.progress",
        (event: MessageEvent<string>) => {
          const data = parseData<UninstallProgressEvent>(event.data);
          if (data) handlersRef.current.onUninstallProgress?.(data);
        },
      );

      source.onerror = () => {
        setConnected(false);
        source?.close();
        source = null;
        retryTimer = setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      source?.close();
      setConnected(false);
    };
  }, []);

  return { connected };
}
