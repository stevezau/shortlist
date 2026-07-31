import {
  MutationCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { AppShell } from "@/components/layout/app-shell";
import { EmptyState, ErrorState } from "@/components/query-boundary";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { resolveArea } from "@/lib/auth";
import { queryKeys, useSession, useSetupState } from "@/lib/queries";
import { DashboardPage } from "@/pages/dashboard";
import { LoginPage } from "@/pages/login";
import { RequestsPage } from "@/pages/requests";
import { RowRenamePage } from "@/pages/row-rename";
import { RowsPage } from "@/pages/rows";
import { RunDetailPage } from "@/pages/run-detail";
import { RunUserTracePage } from "@/pages/run-user-trace";
import { LogsPage } from "@/pages/logs";
import { RunsPage } from "@/pages/runs";
import { SettingsPage } from "@/pages/settings";
import { SetupPage } from "@/pages/setup";
import { JobsPage } from "@/pages/jobs";
import { UninstallPage } from "@/pages/uninstall";
import { UserDetailPage } from "@/pages/user-detail";
import { UsersPage } from "@/pages/users";

const queryClient = new QueryClient({
  // Any mutation might enqueue background work — disabling someone, pausing them, editing a row —
  // and the activity poll idles at 30s, so its toast could arrive half a minute after the click that
  // caused it. Refreshing the job queue after EVERY mutation is one cheap request and means no future
  // enqueue site has to remember to do it; wiring each call site individually is what left this one
  // silent for 30 seconds in the first place.
  mutationCache: new MutationCache({
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs });
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      // Retrying a 401/403 just delays the login screen behind a spinner — the answer
      // will not change until the visitor signs in.
      retry: (failureCount, error) =>
        !(
          error instanceof ApiError &&
          (error.status === 401 || error.status === 403)
        ) && failureCount < 1,
    },
  },
});

/**
 * Main-app gate.
 *
 * A fresh install nobody has claimed goes straight to the wizard — signing in with Plex is not a
 * gate in front of setup, it IS a step of setup, and it's the one that claims the instance. Once
 * claimed, an unauthenticated visitor goes to /login, an owner with an unfinished wizard goes to
 * /setup, and everyone else gets the app.
 */
function RequireApp() {
  const session = useSession();
  const authenticated = session.data?.authenticated ?? false;
  const loginRequired = session.data?.login_required ?? true;
  // Setup state is owner-only once the instance is claimed: asking for it before we know who this
  // is just 401s, and the visitor would sit behind a skeleton instead of the login screen.
  const setup = useSetupState({ enabled: authenticated || !loginRequired });

  if (session.isPending) {
    return (
      <div className="mx-auto mt-16 w-full max-w-4xl px-4">
        <Skeleton className="h-96 w-full" />
      </div>
    );
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
  if (!authenticated && loginRequired) return <Navigate to="/login" replace />;
  if (setup.isPending) {
    return (
      <div className="mx-auto mt-16 w-full max-w-4xl px-4">
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  const area = resolveArea(
    authenticated,
    setup.data?.completed ?? false,
    loginRequired,
  );
  if (area === "login") return <Navigate to="/login" replace />;
  if (area === "setup") return <Navigate to="/setup" replace />;
  return <AppShell />;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="login" element={<LoginPage />} />
          <Route path="setup" element={<SetupPage />} />
          <Route element={<RequireApp />}>
            <Route index element={<DashboardPage />} />
            <Route path="rows" element={<RowsPage />} />
            <Route path="rows/:id/rename" element={<RowRenamePage />} />
            <Route path="users" element={<UsersPage />} />
            <Route path="users/:id" element={<UserDetailPage />} />
            <Route path="runs" element={<RunsPage />} />
            <Route path="logs" element={<LogsPage />} />
            <Route path="runs/:id" element={<RunDetailPage />} />
            <Route
              path="runs/:id/trace/:userId"
              element={<RunUserTracePage />}
            />
            <Route path="requests" element={<RequestsPage />} />
            <Route path="jobs" element={<JobsPage />} />
            {/* Merged into Jobs. Redirect rather than remove: the old page was linked from docs
                and may be bookmarked, and a 404 would read as the feature being gone. */}
            <Route path="schedule" element={<Navigate to="/jobs" replace />} />
            {/* The page was /tools until the nav started calling it Jobs. Kept as a redirect:
                bookmarks and the `action_url` baked into notifications already in the DB. */}
            <Route path="tools" element={<Navigate to="/jobs" replace />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="settings/uninstall" element={<UninstallPage />} />
            <Route
              path="*"
              element={
                <EmptyState
                  title="Page not found"
                  hint="That address doesn't exist. Use the navigation on the left."
                />
              }
            />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
