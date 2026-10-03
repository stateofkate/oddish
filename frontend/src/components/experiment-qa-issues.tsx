"use client";

// The experiment page's QA view: the task split chart, with the category
// breakdown opening under it. The URL carries the same view parameters the
// standalone QA issues page used.
import { useEffect, useRef, useState, type ReactNode } from "react";
import useSWR from "swr";
import { ChevronDown, ChevronRight } from "lucide-react";
import { QaCategories } from "@/components/qa-issue-categories";
import { QaSplit } from "@/components/qa-split";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import type { QaIssueReport } from "@/lib/qa-issues";
import { useQaIssuesView } from "@/lib/use-qa-issues-view";

export type QaIssuesTarget = {
  /** The experiment page without query parameters. */
  baseHref: string;
  /** The QA issue report through this app's API. */
  apiHref: string;
  /** Share pages show task names without links to internal pages. */
  linkTasks: boolean;
};

export function ExperimentQaIssues({
  baseHref,
  apiHref,
  linkTasks,
  action,
  refreshKey,
}: QaIssuesTarget & { action?: ReactNode; refreshKey?: string }) {
  const view = useQaIssuesView(baseHref);
  const version = view?.version;
  const { data, error, mutate } = useSWR<QaIssueReport>(
    `${apiHref}${version ? `?version=${version}` : ""}`,
    fetcher,
    { revalidateOnFocus: false, keepPreviousData: true }
  );
  // Refetch when the caller's task data says a verdict changed.
  const lastRefreshKey = useRef(refreshKey);
  useEffect(() => {
    if (lastRefreshKey.current === refreshKey) return;
    lastRefreshKey.current = refreshKey;
    void mutate();
  }, [refreshKey, mutate]);
  // A link that opens a category or a version opens the breakdown too.
  const [open, setOpen] = useState(
    () => Boolean(view?.category) || version != null
  );

  // The action does not depend on the report, so it stays reachable while
  // the report loads or after it fails. keepPreviousData would otherwise keep
  // showing the last report when a newly selected version fails.
  if (!data || error) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3">
        {error ? (
          <p className="text-destructive text-sm">Could not load QA issues.</p>
        ) : (
          <Skeleton className="h-28 min-w-0 flex-1 rounded-xl" />
        )}
        {action}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div
        className="cursor-pointer"
        onClick={(event) => {
          // The action button carries its own click.
          if ((event.target as HTMLElement).closest("button")) return;
          setOpen(true);
        }}
      >
        <QaSplit
          summary={data.summary}
          burndown={data.burndown}
          pointsHref={baseHref}
          selected={data.version ?? undefined}
          action={action}
        />
      </div>
      <section className="bg-card rounded-xl border p-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="text-muted-foreground hover:text-foreground flex w-full items-center gap-1 px-3 py-1 text-sm font-medium"
        >
          {open ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
          Categories
          {data.version ? (
            <span className="ml-2 font-normal">As of v{data.version}</span>
          ) : null}
        </button>
        {open && !data.categories.length && (
          <p className="text-muted-foreground py-6 text-center text-sm">
            No issues.
          </p>
        )}
        {open && (
          <QaCategories
            key={data.version ?? "now"}
            categories={data.categories}
            initial={data}
            baseHref={baseHref}
            apiHref={apiHref}
            linkTasks={linkTasks}
          />
        )}
      </section>
    </div>
  );
}
