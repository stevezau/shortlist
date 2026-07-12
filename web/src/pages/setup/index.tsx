import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { Navigate, useNavigate } from "react-router-dom";

import { ErrorState } from "@/components/query-boundary";
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
import { StepPrivacy } from "./step-privacy";
import { StepUsers } from "./step-users";
import { StepWelcome } from "./step-welcome";
import type { StepProps } from "./step-props";

const STEP_COMPONENTS: readonly ((props: StepProps) => JSX.Element)[] = [
  StepWelcome,
  StepConnect,
  StepHistory,
  StepCurator,
  StepUsers,
  StepPrivacy,
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

  if (!wizard.loaded) {
    return <Skeleton className="mx-auto mt-16 h-96 w-full max-w-2xl" />;
  }

  const meta = WIZARD_STEPS[wizard.step];
  const Step = STEP_COMPONENTS[wizard.step];
  if (!meta || !Step) return null;

  return (
    <main className="mx-auto w-full max-w-2xl px-4 py-10">
      <header className="mb-8 space-y-4">
        <p className="text-lg font-semibold tracking-tight text-primary">
          <span aria-hidden="true">✨</span> Rowarr setup
        </p>
        <div
          role="progressbar"
          aria-valuemin={1}
          aria-valuemax={TOTAL_STEPS}
          aria-valuenow={wizard.step + 1}
          aria-label={`Setup step ${wizard.step + 1} of ${TOTAL_STEPS}`}
          className="flex gap-1"
        >
          {WIZARD_STEPS.map((step, index) => (
            <div
              key={step.title}
              className={
                index <= wizard.step
                  ? "h-1.5 flex-1 rounded-full bg-primary"
                  : "h-1.5 flex-1 rounded-full bg-muted"
              }
            />
          ))}
        </div>
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {meta.title}
          </h1>
          <p className="text-sm text-muted-foreground">{meta.why}</p>
        </div>
      </header>

      <Step
        data={wizard.data}
        update={wizard.update}
        next={wizard.next}
        complete={wizard.complete}
      />

      {wizard.step > 0 && wizard.step < TOTAL_STEPS - 1 && (
        <footer className="mt-8 flex items-center justify-between border-t pt-4">
          <Button variant="ghost" onClick={wizard.back}>
            <ArrowLeft aria-hidden="true" />
            Back
          </Button>
          <Button onClick={wizard.next} disabled={!wizard.canProceed}>
            Next
            <ArrowRight aria-hidden="true" />
          </Button>
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
  if (area === "app") return <Navigate to="/" replace />;

  return <Wizard />;
}
