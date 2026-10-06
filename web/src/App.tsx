import {
  MutationCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { lazy, Suspense, type ComponentType } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useSearchParams,
} from "react-router";

import { AppShell } from "@/components/layout/app-shell";
import { ErrorState } from "@/components/query-boundary";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { basePath } from "@/lib/base-path";
import { resolveArea } from "@/lib/auth";
import { queryKeys, useSession, useSetupState } from "@/lib/queries";

/** A route's page, fetched the first time someone opens it rather than in the first paint. Pages are
 *  named exports, and `React.lazy` wants a default one. */
function page<K extends string>(
  load: () => Promise<{ [P in K]: ComponentType }>,
  name: K,
) {
  return lazy(() => load().then((module) => ({ default: module[name] })));
}

const ActivityPage = page(() => import("@/pages/activity"), "ActivityPage");
const AssistantAccessPage = page(() => import("@/pages/assistant-access"), "AssistantAccessPage");
const AssistantChangePage = page(() => import("@/pages/assistant-change"), "AssistantChangePage");
const AssistantConsentPage = page(() => import("@/pages/assistant-consent"), "AssistantConsentPage");
const DashboardPage = page(() => import("@/pages/dashboard"), "DashboardPage");
const IssuePage = page(() => import("@/pages/issue"), "IssuePage");
const LoginPage = page(() => import("@/pages/login"), "LoginPage");
const NotFoundPage = page(() => import("@/pages/not-found"), "NotFoundPage");
const RequestsPage = page(() => import("@/pages/requests"), "RequestsPage");
const RowEditPage = page(() => import("@/pages/row-edit"), "RowEditPage");
const RowRenamePage = page(() => import("@/pages/row-rename"), "RowRenamePage");
const RowsPage = page(() => import("@/pages/rows"), "RowsPage");
const RunDetailPage = page(() => import("@/pages/run-detail"), "RunDetailPage");
const RunUserTracePage = page(() => import("@/pages/run-user-trace"), "RunUserTracePage");
const RunsPage = page(() => import("@/pages/runs"), "RunsPage");
const SettingsPage = page(() => import("@/pages/settings"), "SettingsPage");
const SetupPage = page(() => import("@/pages/setup"), "SetupPage");
const SharingPage = page(() => import("@/pages/sharing"), "SharingPage");
const UninstallPage = page(() => import("@/pages/uninstall"), "UninstallPage");
const UserDetailPage = page(() => import("@/pages/user-detail"), "UserDetailPage");
const UsersPage = page(() => import("@/pages/users"), "UsersPage");
const WatchingAccountPage = page(() => import("@/pages/watching-account"), "WatchingAccountPage");

function PageSkeleton() {
  return (
    <div className="mx-auto mt-16 w-full max-w-4xl px-4">
      <Skeleton className="h-96 w-full" />
    </div>
  );
}

/**
 * Old /jobs links, carried onto the Activity page's Jobs tab. The Jobs page's own badges wrote
 * `?tab=activity&filter=failed` for its run feed; that view is now `?view=activity` inside the tab,
 * and the filter comes along.
 */
function LegacyJobsRedirect() {
  const [searchParams] = useSearchParams();
  const next = new URLSearchParams({ tab: "jobs" });
  if (searchParams.get("tab") === "activity") next.set("view", "activity");
  const filter = searchParams.get("filter");
  if (filter) next.set("filter", filter);
  return <Navigate to={`/activity?${next.toString()}`} replace />;
}

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

  if (session.isPending) return <PageSkeleton />;
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
  if (setup.isPending) return <PageSkeleton />;

  const area = resolveArea(
    authenticated,
    setup.data?.completed ?? false,
    loginRequired,
  );
  if (area === "login") return <Navigate to="/login" replace />;
  if (area === "setup") return <Navigate to="/setup" replace />;
  return <AppShell />;
}

/** Every route. Separate from {@link App} so a test can mount it under a `MemoryRouter`. */
export function AppRoutes() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Routes>
        <Route path="login" element={<LoginPage />} />
        <Route path="setup" element={<SetupPage />} />
        <Route path="assistant/consent" element={<AssistantConsentPage />} />
        <Route path="assistant/changes/:changeId" element={<AssistantChangePage />} />
        {/* Old addresses, redirected rather than removed: they are in bookmarks, in the docs, and in
            the `action_url` of notifications already stored in the database. Outside the auth gate on
            purpose — the gate applies to wherever they land. */}
        <Route path="sharing" element={<Navigate to="/privacy" replace />} />
        <Route path="jobs" element={<LegacyJobsRedirect />} />
        <Route path="logs" element={<Navigate to="/activity?tab=log" replace />} />
        {/* /schedule was merged into Jobs, and Jobs was /tools before the nav called it Jobs. */}
        <Route path="schedule" element={<Navigate to="/activity?tab=jobs" replace />} />
        <Route path="tools" element={<Navigate to="/activity?tab=jobs" replace />} />
        <Route element={<RequireApp />}>
          <Route index element={<DashboardPage />} />
          <Route path="rows" element={<RowsPage />} />
          {/* Before "rows/:id", or "new" would be parsed as a row id. */}
          <Route path="rows/new" element={<RowEditPage />} />
          <Route path="rows/:id/rename" element={<RowRenamePage />} />
          <Route path="rows/:id" element={<RowEditPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="privacy" element={<SharingPage />} />
          <Route path="users/:id" element={<UserDetailPage />} />
          <Route path="watching-account" element={<WatchingAccountPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:id" element={<RunDetailPage />} />
          <Route
            path="runs/:id/trace/row/:rowSlug"
            element={<RunUserTracePage />}
          />
          <Route
            path="runs/:id/trace/:userId"
            element={<RunUserTracePage />}
          />
          <Route path="requests" element={<RequestsPage />} />
          <Route path="activity" element={<ActivityPage />} />
          <Route path="issue" element={<IssuePage />} />
          <Route path="assistant-access" element={<AssistantAccessPage />} />
          {/* One route for /settings and its three tabs, so moving between them (or arriving from an
              old /settings#section link, rewritten to its tab in place) keeps the page mounted and
              every unsaved draft with it. "settings/uninstall" is a static segment and wins. */}
          <Route path="settings/:tab?" element={<SettingsPage />} />
          <Route path="settings/uninstall" element={<UninstallPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={basePath || "/"}>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
