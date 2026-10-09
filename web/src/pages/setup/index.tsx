import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Navigate, useNavigate } from "react-router";

import { ErrorState } from "@/components/query-boundary";
import { Wordmark } from "@/components/brand";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { resolveArea } from "@/lib/auth";
import { queryKeys, useSession, useSetupState } from "@/lib/queries";
import { TOTAL_STEPS, useWizard, WIZARD_STEPS } from "@/lib/wizard";

import { StepConnect } from "./step-connect";
import { StepCurator } from "./step-curator";
import { StepCustomize } from "./step-customize";
import { StepFirstRun } from "./step-first-run";
import { StepHistory } from "./step-history";
import { StepUsers } from "./step-users";
import { StepWelcome } from "./step-welcome";
import type { StepHeader, StepProps } from "./step-props";

const STEP_COMPONENTS: readonly ((props: StepProps) => ReactNode)[] = [
  StepWelcome,
  StepConnect,
  StepHistory,
  StepCurator,
  StepUsers,
  StepCustomize,
  StepFirstRun,
];

function Wizard() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const wizard = useWizard(() => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.setupState });
    navigate("/", { replace: true });
  });

  const [header, setHeader] = useState<StepHeader | null>(null);

  if (!wizard.loaded) {
    return <Skeleton className="mx-auto mt-16 h-96 w-full max-w-2xl" />;
  }

  const meta = WIZARD_STEPS[wizard.step];
  const Step = STEP_COMPONENTS[wizard.step];
  if (!meta || !Step) return null;

  return (
    <main className={`mx-auto w-full px-5 py-6 sm:px-10 sm:py-8 ${wizard.step === 5 ? "max-w-[1110px]" : "max-w-2xl"}`}>
      <header className="mb-7">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3"><Wordmark size="sm" /><span className="text-sm text-muted-foreground">setup</span></div>
          <p className="text-sm text-muted-foreground">{header?.stepLabel ?? `Step ${wizard.step + 1} of ${TOTAL_STEPS}`}</p>
        </div>
        <div
          role="progressbar"
          aria-valuemin={1}
          aria-valuemax={TOTAL_STEPS}
          aria-valuenow={wizard.step + 1}
          aria-label={`Setup step ${wizard.step + 1} of ${TOTAL_STEPS}`}
          className="mb-8 mt-5 flex gap-1.5"
        >
          {WIZARD_STEPS.map((step, index) => (
            <div
              key={step.title}
              className={
                index === wizard.step
                  ? "h-[3px] flex-1 rounded-full bg-foreground"
                  : index < wizard.step ? "h-[3px] flex-1 rounded-full bg-foreground/40" : "h-[3px] flex-1 rounded-full bg-muted"
              }
            />
          ))}
        </div>
        <div>
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
            <h1 className="text-[28px] font-semibold leading-tight tracking-tight sm:text-3xl">
              {header?.title ?? meta.title}
            </h1>
            {header?.badge && <Badge variant={header.badge.variant}>{header.badge.text}</Badge>}
          </div>
          <p className="max-w-xl text-[13px] leading-relaxed text-muted-foreground sm:text-sm">{header?.why ?? meta.why}</p>
        </div>
      </header>

      <Step
        data={wizard.data}
        update={wizard.update}
        next={wizard.next}
        complete={wizard.complete}
        back={wizard.back}
        setHeader={setHeader}
      />

      {wizard.step > 0 && wizard.step < TOTAL_STEPS - 1 && wizard.step !== 5 && (
        <footer className="mt-8 flex items-center justify-between border-t pt-4">
          <Button variant="ghost" onClick={wizard.back}>
            <ArrowLeft aria-hidden="true" />
            Back
          </Button>
          <div className="flex flex-wrap items-center justify-end gap-4">
            {wizard.step === 2 && !wizard.canProceed && (
              <span className="text-sm text-muted-foreground">Next unlocks when the key tests OK</span>
            )}
            <Button onClick={wizard.next} disabled={!wizard.canProceed}>
              Next
              <ArrowRight aria-hidden="true" />
            </Button>
          </div>
        </footer>
      )}
      {wizard.step === TOTAL_STEPS - 1 && (
        <footer className="mt-8 border-t pt-4">
          <Button variant="ghost" onClick={wizard.back}>
            <ArrowLeft aria-hidden="true" />
            Back
          </Button>
        </footer>
      )}
    </main>
  );
}

/** Route guard + the wizard itself. */
export function SetupPage() {
  const session = useSession();
  const setup = useSetupState();

  if (session.isPending || setup.isPending) {
    return <Skeleton className="mx-auto mt-16 h-96 w-full max-w-2xl" />;
  }
  if (session.isError) {
    return (
      <div className="mx-auto mt-16 max-w-2xl px-4">
        <ErrorState
          error={session.error}
          onRetry={() => void session.refetch()}
        />
      </div>
    );
  }

  const area = resolveArea(
    session.data.authenticated,
    setup.data?.completed ?? false,
    session.data.login_required,
  );
  if (area === "login") return <Navigate to="/login" replace />;
  if (setup.isError) return <div className="mx-auto mt-16 max-w-2xl px-4"><ErrorState error={setup.error} onRetry={() => void setup.refetch()} /></div>;
  if (area === "app") return <Navigate to="/" replace />;

  return <Wizard />;
}
