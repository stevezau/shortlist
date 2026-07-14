import { useQueryClient } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";

import { Logo } from "@/components/brand";
import { PlexPinButton } from "@/components/plex-pin-button";
import { ErrorState } from "@/components/query-boundary";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { resolveArea } from "@/lib/auth";
import { queryKeys, useSession, useSetupState } from "@/lib/queries";

/**
 * "Sign in with Plex" is the only auth (design doc §7) — and only the server
 * owner's account is authorized. After the PIN links, the backend session
 * cookie exists; refetching the session routes the owner onward.
 */
export function LoginPage() {
  const session = useSession();
  const authenticated = session.data?.authenticated ?? false;
  // Setup state is owner-only. Asking for it before sign-in 401s, and the visitor would sit
  // behind this very skeleton instead of seeing the button they came here to press.
  const setup = useSetupState({
    enabled: authenticated || !(session.data?.login_required ?? true),
  });
  const queryClient = useQueryClient();

  if (session.isPending || (authenticated && setup.isPending)) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Skeleton className="h-64 w-full max-w-md" />
      </div>
    );
  }

  if (session.isError) {
    return (
      <div className="mx-auto flex min-h-screen max-w-md items-center">
        <ErrorState
          error={session.error}
          onRetry={() => void session.refetch()}
        />
      </div>
    );
  }

  const completed = setup.data?.completed ?? false;
  const area = resolveArea(
    authenticated,
    completed,
    session.data?.login_required ?? true,
  );
  if (area !== "login") {
    return <Navigate to={area === "setup" ? "/setup" : "/"} replace />;
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-md animate-fade-in">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo size="lg" className="mb-4" />
          <h1 className="text-3xl font-semibold tracking-tight">Shortlist</h1>
          <p className="mt-2 max-w-xs text-sm text-muted-foreground">
            A private, AI-curated Picked-for-You row for every user on your Plex
            server.
          </p>
        </div>

        <Card className="shadow-elevated">
          <CardHeader className="pb-4">
            <CardTitle className="text-base">Sign in to continue</CardTitle>
            <CardDescription>
              Use the Plex account that owns this server.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <PlexPinButton
              onLinked={() => {
                void queryClient.invalidateQueries({
                  queryKey: queryKeys.session,
                });
                void queryClient.invalidateQueries({
                  queryKey: queryKeys.setupState,
                });
              }}
            />
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
