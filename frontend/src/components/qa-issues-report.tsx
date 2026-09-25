// The page shell renders on the server; the category list is a client
// component (qa-issue-categories) so categories open without a page load.
import { Suspense } from "react";
import Link from "next/link";
import { QaCategories, QaIssuesLink } from "@/components/qa-issue-categories";
import { QaSplit } from "@/components/qa-split";
import { Skeleton } from "@/components/ui/skeleton";
import type { QaIssueReportResult } from "@/lib/qa-issues";

const PILL = "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs";

interface Props {
  report: Promise<QaIssueReportResult>;
  /** The experiment or delivery page. */
  backHref: string;
  /** This page without query parameters. */
  baseHref: string;
  /** The same report through this app's API, for the browser. */
  apiHref: string;
  /** Share pages show task names without links to internal pages. */
  linkTasks?: boolean;
}

export function QaIssuesPage({
  report,
  backHref,
  baseHref,
  apiHref,
  linkTasks = true,
}: Props) {
  return (
    <div className="space-y-4 py-3">
      <Link
        href={backHref}
        className="text-muted-foreground text-sm hover:underline"
      >
        ← Back
      </Link>
      <Suspense fallback={<QaIssuesSkeleton />}>
        <QaIssuesBody
          report={report}
          baseHref={baseHref}
          apiHref={apiHref}
          linkTasks={linkTasks}
        />
      </Suspense>
    </div>
  );
}

function QaIssuesSkeleton() {
  return (
    <div className="space-y-4" aria-busy>
      <Skeleton className="h-8 w-72" />
      <Skeleton className="h-36" />
      <Skeleton className="h-64" />
    </div>
  );
}

async function QaIssuesBody({
  report,
  baseHref,
  apiHref,
  linkTasks,
}: Omit<Props, "backHref"> & { linkTasks: boolean }) {
  const result = await report;
  if (!result.ok) {
    return (
      <p className="border-destructive/50 text-destructive rounded-lg border p-4 text-sm">
        Could not load QA issues ({result.status}
        {result.detail ? `: ${result.detail}` : ""}).
      </p>
    );
  }
  const { report: data } = result;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {data.scope_name}
        </h1>
        {data.version && (
          <span className="flex items-baseline gap-2 text-sm">
            <span className={`${PILL} bg-muted font-medium`}>
              As of v{data.version}
            </span>
            <QaIssuesLink baseHref={baseHref} set={{ version: undefined }}>
              Now
            </QaIssuesLink>
          </span>
        )}
      </div>
      <QaSplit
        summary={data.summary}
        burndown={data.burndown}
        pointsHref={baseHref}
        selected={data.version ?? undefined}
      />
      <section className="bg-card rounded-xl border p-2">
        <h2 className="text-muted-foreground px-3 py-1 text-sm font-medium">
          Categories
        </h2>
        {!data.categories.length && (
          <p className="text-muted-foreground py-10 text-center text-sm">
            No issues.
          </p>
        )}
        <QaCategories
          key={data.version ?? "now"}
          categories={data.categories}
          initial={data}
          baseHref={baseHref}
          apiHref={apiHref}
          linkTasks={linkTasks}
        />
      </section>
    </div>
  );
}
