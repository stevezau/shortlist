import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";
import type { SetupState } from "./types";

export const TOTAL_STEPS = 7;

export type CuratorProvider =
  | "anthropic"
  | "openai"
  // Every self-hosted runtime (Ollama, llama.cpp, LM Studio, vLLM, LocalAI) plus hosted OpenAI-API
  // gateways. "ollama" is its pre-merge name, still stored on instances configured before it.
  | "openai_compatible"
  | "google"
  | "ollama"
  | "none";

/** The wizard's persisted blob — round-tripped through /api/setup/state. */
export interface WizardData {
  plex_url?: string;
  server_name?: string;
  /** Step 1 gate: the server was probed and linked. */
  linked?: boolean;
  history_source?: "tautulli" | "plex";
  /** Step 2 gate: a working TMDB key is on file (recommendations are impossible without one). */
  tmdb_set?: boolean;
  /** Step 3 gate: a curator card was chosen (None counts). */
  curator_provider?: CuratorProvider;
  /**
   * Step 3 gate: a key/URL provider was saved AND passed its test. "none" needs no key, so it's
   * ready the moment it's picked. Choosing a paid provider's card is NOT enough — without this the
   * run silently degrades to heuristic mode with a key the user believes is active.
   */
  curator_ready?: boolean;
  customized?: boolean;
}

export interface WizardStepMeta {
  title: string;
  /** The one-line "what & why" shown under every step title (design doc §3). */
  why: string;
}

export const WIZARD_STEPS: readonly WizardStepMeta[] = [
  {
    title: "Welcome",
    why: "A private, AI-curated Picked-for-You row for every user on your Plex server.",
  },
  {
    title: "Connect Plex",
    why: "Shortlist reads watch history and writes rows on your server.",
  },
  {
    title: "Recommendations & history",
    why: "A TMDB key (required) powers recommendations. Watch history is read from Plex per user; Tautulli is optional, only for friendlier display names.",
  },
  {
    title: "Choose your curator",
    why: "An LLM re-ranks titles you already own — it can't invent anything. Shortlist is fully functional without one.",
  },
  {
    title: "Pick your users",
    why: "Choose who gets a nightly row. You can change this any time.",
  },
  {
    title: "Make it yours",
    why: "Row name, row size, and when rows refresh.",
  },
  {
    title: "First run",
    why: "Build every enabled user's row right now and watch it happen live.",
  },
];

export function clampStep(step: number): number {
  if (!Number.isFinite(step)) return 0;
  return Math.min(Math.max(Math.trunc(step), 0), TOTAL_STEPS - 1);
}

/** Whether Next is allowed to leave `step` given what the wizard knows. */
export function canLeaveStep(step: number, data: WizardData): boolean {
  switch (step) {
    case 1:
      return data.linked === true;
    // TMDB is how Shortlist finds similar titles; without a key every run dies at the first user.
    case 2:
      return data.tmdb_set === true;
    // "none" is a first-class choice (heuristic mode) and is ready the moment it's picked. A
    // key/URL provider must have been saved AND passed its test — picking the card alone must not
    // open the gate, or the run silently degrades to heuristic mode with a key that was never saved.
    case 3:
      return data.curator_provider === "none" || data.curator_ready === true;
    default:
      return true;
  }
}

export interface WizardApi {
  /** False until GET /api/setup/state resolved (resume-on-refresh). */
  loaded: boolean;
  step: number;
  data: WizardData;
  canProceed: boolean;
  next: () => void;
  back: () => void;
  update: (patch: Partial<WizardData>) => void;
  /** Marks setup completed on the server, then calls onComplete. */
  complete: () => Promise<void>;
}

function persist(
  step: number,
  data: WizardData,
  completed = false,
): Promise<SetupState> {
  return api.putSetupState({
    step,
    state: data as Record<string, unknown>,
    completed,
  });
}

/**
 * Wizard state machine: owns the current step + persisted data blob, loads
 * saved progress on mount, and writes progress back on every transition so a
 * refresh resumes mid-wizard.
 */
export function useWizard(onComplete?: () => void): WizardApi {
  const [step, setStep] = useState(0);
  const [data, setData] = useState<WizardData>({});
  const [loaded, setLoaded] = useState(false);

  const stepRef = useRef(step);
  const dataRef = useRef(data);
  const onCompleteRef = useRef(onComplete);
  onCompleteRef.current = onComplete;

  useEffect(() => {
    let cancelled = false;
    api
      .getSetupState()
      .then((saved) => {
        if (cancelled) return;
        const resumedStep = clampStep(saved.step);
        const resumedData = (saved.state as WizardData) ?? {};
        stepRef.current = resumedStep;
        dataRef.current = resumedData;
        setStep(resumedStep);
        setData(resumedData);
      })
      .catch(() => {
        // Fresh install (no state yet) or transient failure — start at step 0.
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const update = useCallback((patch: Partial<WizardData>) => {
    const merged = { ...dataRef.current, ...patch };
    dataRef.current = merged;
    setData(merged);
    persist(stepRef.current, merged).catch(() => {
      // Best-effort: the next successful save carries the same data.
    });
  }, []);

  const next = useCallback(() => {
    if (!canLeaveStep(stepRef.current, dataRef.current)) return;
    const target = clampStep(stepRef.current + 1);
    if (target === stepRef.current) return;
    stepRef.current = target;
    setStep(target);
    persist(target, dataRef.current).catch(() => {});
  }, []);

  const back = useCallback(() => {
    const target = clampStep(stepRef.current - 1);
    if (target === stepRef.current) return;
    stepRef.current = target;
    setStep(target);
    persist(target, dataRef.current).catch(() => {});
  }, []);

  const complete = useCallback(async () => {
    try {
      await persist(stepRef.current, dataRef.current, true);
    } catch {
      // Even if the write is lost, the guard will route back into the wizard
      // rather than stranding the user — safe to proceed.
    }
    onCompleteRef.current?.();
  }, []);

  return {
    loaded,
    step,
    data,
    canProceed: canLeaveStep(step, data),
    next,
    back,
    update,
    complete,
  };
}
