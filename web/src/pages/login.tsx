import { useQueryClient } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";

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
 * "Login with Plex" is the only auth (design doc §7) — and only the server
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
    <main className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-2xl">
            <span aria-hidden="true">✨</span>
            <span className="text-primary">Rowarr</span>
          </CardTitle>
          <CardDescription>
            A private, AI-curated Picked-for-You row for every user on your Plex
            server. Sign in with the Plex account that owns this server — no
            password ever touches Rowarr.
          </CardDescription>
        </CardHeader>
        <CardContent>
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
    </main>
  );
}
