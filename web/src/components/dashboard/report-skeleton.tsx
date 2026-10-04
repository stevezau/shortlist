import { Skeleton } from "@/components/ui/skeleton";

/**
 * Placeholder for the Impact report while there is nothing at all to show: the head row and five
 * stat tiles, one chart-height block, then two list cards — the same grids as the real report, so
 * the page does not jump when it arrives. Motion follows the Skeleton primitive.
 */
export function ReportSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading the impact report" className="space-y-6">
      <div className="overflow-hidden rounded-xl border bg-card">
        <div className="flex items-start justify-between gap-6 border-b px-4 py-3.5 sm:px-5">
          <div className="space-y-2">
            <Skeleton className="h-5 w-20" />
            <Skeleton className="h-4 w-64 max-w-full" />
          </div>
          <Skeleton className="h-9 w-56" />
        </div>
        <div className="grid gap-x-8 gap-y-5 px-4 py-4 sm:grid-cols-2 sm:px-5 lg:grid-cols-3 xl:grid-cols-5">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="space-y-2">
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-9 w-20" />
              <Skeleton className="h-3 w-32" />
            </div>
          ))}
        </div>
      </div>
      <Skeleton className="h-56 w-full rounded-xl" />
      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-64 w-full rounded-xl" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    </div>
  );
}
