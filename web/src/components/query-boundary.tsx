import type { UseQueryResult } from "@tanstack/react-query";
import { Inbox, type LucideIcon, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  const message =
    error instanceof ApiError
      ? error.message
      : "Something went wrong while loading this. The details are in the server log.";
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4"
    >
      <p className="text-sm text-foreground">{message}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        <RefreshCw aria-hidden="true" />
        Try again
      </Button>
    </div>
  );
}

export function EmptyState({
  icon: Icon = Inbox,
  title,
  hint,
  action,
}: {
  icon?: LucideIcon;
  title: string;
  hint: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-12 text-center">
      {/* Bare, never on a tile: the design direction has no icon tiles. */}
      <Icon aria-hidden="true" className="h-6 w-6 text-muted-foreground" />
      <div className="space-y-1">
        <p className="font-medium">{title}</p>
        <p className="mx-auto max-w-sm text-sm text-muted-foreground">{hint}</p>
      </div>
      {action}
    </div>
  );
}

interface QueryBoundaryProps<T> {
  query: UseQueryResult<T>;
  /** Loading state — always a skeleton, never a spinner-only page. */
  skeleton: ReactNode;
  /** When true for the loaded data, render `empty` instead of `children`. */
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}

/**
 * Renders the four mandatory data-view states (rules/frontend.md):
 * loading skeleton → error with retry → empty with explanation → success.
 */
export function QueryBoundary<T>({
  query,
  skeleton,
  isEmpty,
  empty,
  children,
}: QueryBoundaryProps<T>) {
  if (query.isPending) return <>{skeleton}</>;
  if (query.isError)
    return (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} />
    );
  if (isEmpty?.(query.data) && empty) return <>{empty}</>;
  return <>{children(query.data)}</>;
}
