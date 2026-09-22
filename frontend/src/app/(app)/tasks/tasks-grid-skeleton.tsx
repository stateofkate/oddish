import { Skeleton } from "@/components/ui/skeleton";

// Placeholder grid shown while the server fetches the (filtered) task page.
// Mirrors the TaskCard layout: identity, activity, results, trial previews, experiment links.
export function TasksGridSkeleton({ count = 9 }: { count?: number }) {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: count }).map((_, index) => (
        <div
          key={index}
          className="bg-card/95 rounded-lg border border-[#6f88b4]/20 px-5 py-5 shadow-xs"
        >
          <div className="flex items-start justify-between gap-3">
            <Skeleton className="mt-0.5 h-4 w-4 shrink-0 rounded" />
            <div className="min-w-0 flex-1 space-y-2">
              <Skeleton className="h-5 w-40 max-w-full" />
              <Skeleton className="h-4 w-12" />
            </div>
            <div className="flex shrink-0 flex-col items-end gap-2">
              <Skeleton className="h-3 w-16" />
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-4 w-14" />
            </div>
          </div>
          <div className="mt-4 flex justify-between">
            <Skeleton className="h-10 w-20" />
            <Skeleton className="h-10 w-16" />
          </div>
          <div className="mt-4 space-y-2 border-t border-border/60 pt-3">
            <Skeleton className="h-6 w-32" />
            <Skeleton className="h-3 w-40" />
            <Skeleton className="h-3 w-36" />
          </div>
          <div className="mt-4 space-y-1.5 border-t border-border/60 pt-3">
            <Skeleton className="h-3 w-20" />
            <div className="flex flex-wrap gap-1">
              {Array.from({ length: 7 }).map((__, i) => (
                <Skeleton key={i} className="h-[18px] w-[18px] rounded-[4px]" />
              ))}
            </div>
          </div>
          <div className="mt-4 space-y-2 border-t border-border/60 pt-3">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-3/4" />
          </div>
        </div>
      ))}
    </div>
  );
}
