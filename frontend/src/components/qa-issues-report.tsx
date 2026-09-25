// Server components only: every control is a link or a GET form over the URL
// state in QaIssuesView, so the page ships no client JavaScript of its own.
import { Suspense } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  GitPullRequest,
  Search,
} from "lucide-react";
import { Bar, QaSplit } from "@/components/qa-split";
import { CopyJsonButton } from "@/components/qa-report/copy-json-button";
import { Skeleton } from "@/components/ui/skeleton";
import { qaStatusLabel } from "@/lib/deliveries";
import type { DeliveryQAStatus } from "@/lib/types";
import {
  COPY_LIMIT,
  PAGE_SIZE,
  qaIssuesHref,
  type QaIssue,
  type QaIssueCategory,
  type QaIssueReport,
  type QaIssueReportResult,
  type QaIssueTask,
  type QaIssuesView,
} from "@/lib/qa-issues";

const RED = "bg-red-500";
const GREY = "bg-muted-foreground/30";
const PILL = "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs";
const BLOCKING_PILL = `${PILL} bg-red-500/10 text-red-700 dark:text-red-300`;

interface Props {
  report: Promise<QaIssueReportResult>;
  view: QaIssuesView;
  /** The experiment or delivery page. */
  backHref: string;
  /** This page without query parameters. */
  baseHref: string;
  /** Share pages show task names without links to internal pages. */
  linkTasks?: boolean;
}

export function QaIssuesPage({
  report,
  view,
  backHref,
  baseHref,
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
          view={view}
          baseHref={baseHref}
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
  view,
  baseHref,
  linkTasks,
}: Omit<Props, "backHref">) {
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
  const href = (next: Partial<QaIssuesView>) =>
    qaIssuesHref(baseHref, { ...view, page: 1, ...next });
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">
        {data.scope_name}
      </h1>
      <QaSplit summary={data.summary} burndown={data.burndown} />
      <section className="bg-card rounded-xl border p-2">
        <h2 className="text-muted-foreground px-3 py-1 text-sm font-medium">
          Categories
        </h2>
        {!data.categories.length && (
          <p className="text-muted-foreground py-10 text-center text-sm">
            No issues.
          </p>
        )}
        {data.categories.map((category) => {
          const open = view.category === category.slug;
          return (
            <CategoryRow
              key={category.slug}
              category={category}
              scale={data.categories[0].issues}
              open={open}
              href={href({ category: open ? undefined : category.slug })}
            >
              {open && (
                <CategoryDetail
                  data={data}
                  view={view}
                  href={href}
                  linkTasks={linkTasks}
                />
              )}
            </CategoryRow>
          );
        })}
      </section>
    </div>
  );
}

function CategoryRow({
  category,
  scale,
  open,
  href,
  children,
}: {
  category: QaIssueCategory;
  /** The largest category's issue count, so all bars share one scale. */
  scale: number;
  open: boolean;
  href: string;
  children: React.ReactNode;
}) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div>
      <Link
        href={href}
        scroll={false}
        aria-expanded={open}
        title={category.description}
        className="hover:bg-muted/60 focus-visible:ring-ring flex items-center gap-3 rounded-lg px-3 py-2.5 focus-visible:ring-2 focus-visible:outline-none"
      >
        <Chevron
          className="text-muted-foreground h-4 w-4 shrink-0"
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-sm font-medium">{category.name}</span>
            <span className="text-sm tabular-nums">
              Issues: <span className="font-semibold">{category.issues}</span>{" "}
              <span
                className={
                  category.blocking_issues
                    ? "text-red-600 dark:text-red-400"
                    : "text-muted-foreground"
                }
              >
                ({category.blocking_issues} blocking)
              </span>
            </span>
            <span className="text-muted-foreground text-xs tabular-nums">
              Tasks: {category.tasks}
              {category.dimension !== "Other" && ` · ${category.dimension}`}
            </span>
          </div>
          <div className="mt-1.5 max-w-xs">
            <Bar
              of={scale}
              parts={[
                [category.blocking_issues, RED],
                [category.issues - category.blocking_issues, GREY],
              ]}
            />
          </div>
        </div>
      </Link>
      {open && (
        <div className="bg-muted/30 mt-1 rounded-lg border p-3">
          <p className="text-muted-foreground mb-3 text-xs">
            {category.description}
          </p>
          {children}
        </div>
      )}
    </div>
  );
}

function CategoryDetail({
  data,
  view,
  href,
  linkTasks,
}: {
  data: QaIssueReport;
  view: QaIssuesView;
  href: (next: Partial<QaIssuesView>) => string;
  linkTasks?: boolean;
}) {
  const pages = Math.max(1, Math.ceil(data.task_total / PAGE_SIZE));
  // Not clamped: data.tasks is the window for the URL page, so a page past
  // the end shows no tasks and links back to the last page.
  const page = view.page;
  // data.tasks is the COPY_LIMIT window that holds this page.
  const offset = ((page - 1) * PAGE_SIZE) % COPY_LIMIT;
  const shown = data.tasks.slice(offset, offset + PAGE_SIZE);
  return (
    <div className="space-y-3">
      <form className="flex flex-wrap items-center gap-2" method="get">
        <input type="hidden" name="category" value={view.category} />
        <label className="relative min-w-40 flex-1">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 h-4 w-4 -translate-y-1/2" />
          <input
            type="search"
            name="q"
            defaultValue={view.q}
            placeholder="Task, author, PR, text"
            aria-label="Search issues"
            className="bg-background focus-visible:ring-ring w-full rounded-md border py-1.5 pr-2 pl-8 text-sm focus-visible:ring-1 focus-visible:outline-none"
          />
        </label>
        <label className="flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm">
          <input
            type="checkbox"
            name="blocking"
            value="1"
            defaultChecked={view.blocking}
          />
          Blocking only
        </label>
        <button
          type="submit"
          className="hover:bg-muted rounded-md border px-2.5 py-1.5 text-sm"
        >
          Apply
        </button>
        {data.tasks.length > 0 && (
          <CopyJsonButton
            value={copyValue(data)}
            label={`${data.tasks.length} tasks`}
            className="self-stretch"
          />
        )}
      </form>
      {!shown.length && (
        <p className="text-muted-foreground py-6 text-sm">
          {view.q || view.blocking ? "No matches." : "No tasks."}
        </p>
      )}
      <ul className="space-y-2">
        {shown.map((task) => (
          <TaskRow key={task.task_id} task={task} linkTasks={linkTasks} />
        ))}
      </ul>
      {(pages > 1 || page > 1) && (
        <nav className="flex items-center justify-between text-sm">
          <PageLink
            href={href({
              category: view.category,
              page: Math.min(page - 1, pages),
            })}
            disabled={page <= 1}
          >
            Previous
          </PageLink>
          <span className="text-muted-foreground tabular-nums">
            Page {page} of {pages}
          </span>
          <PageLink
            href={href({ category: view.category, page: page + 1 })}
            disabled={page >= pages}
          >
            Next
          </PageLink>
        </nav>
      )}
    </div>
  );
}

/** What a fixing agent needs: each task and the text of its issues. */
function copyValue(data: QaIssueReport) {
  const category = data.categories.find((c) => c.slug === data.category);
  return {
    category: category?.name ?? data.category,
    description: category?.description,
    tasks: data.tasks.map((task) => ({
      task: task.task_name,
      pr: task.pr_url,
      issues: task.issues.map((issue) => ({
        blocking: issue.blocking,
        found_by: issue.found_by,
        file: issue.file ?? undefined,
        title: issue.title,
        text: issue.text,
      })),
    })),
  };
}

function PageLink({
  href,
  disabled,
  children,
}: {
  href: string;
  disabled: boolean;
  children: string;
}) {
  return disabled ? (
    <span className="text-muted-foreground/50 px-2 py-1">{children}</span>
  ) : (
    <Link
      href={href}
      scroll={false}
      className="hover:bg-muted rounded-md px-2 py-1"
    >
      {children}
    </Link>
  );
}

function TaskRow({
  task,
  linkTasks,
}: {
  task: QaIssueTask;
  linkTasks?: boolean;
}) {
  const status = qaStatusLabel(task.qa_status as DeliveryQAStatus["status"]);
  const taskHref = `/tasks/${encodeURIComponent(task.task_id)}`;
  return (
    <li className="bg-card rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm">
        {linkTasks ? (
          <Link href={taskHref} className="font-medium hover:underline">
            {task.task_name}
          </Link>
        ) : (
          <span className="font-medium">{task.task_name}</span>
        )}
        <span className={`${PILL} bg-muted text-muted-foreground`}>
          {status}
        </span>
        {task.blocking_issues > 0 && (
          <span
            className={BLOCKING_PILL}
            title="Blocking issues in all categories"
          >
            <AlertTriangle className="h-3 w-3" /> Blocking:{" "}
            {task.blocking_issues}
          </span>
        )}
        <span className="text-muted-foreground ml-auto flex items-center gap-3 text-xs">
          {task.author && (
            <span>
              {task.author.includes("@") ? task.author : `@${task.author}`}
            </span>
          )}
          {task.pr_url && (
            <a
              href={task.pr_url}
              target="_blank"
              rel="noreferrer"
              className="hover:text-foreground inline-flex items-center gap-1"
            >
              <GitPullRequest className="h-3.5 w-3.5" />
              {task.pr_number ? `#${task.pr_number}` : "PR"}
            </a>
          )}
        </span>
      </div>
      <ul className="mt-1 divide-y">
        {task.issues.map((issue, index) => (
          <IssueRow
            key={index}
            issue={issue}
            taskHref={linkTasks ? taskHref : undefined}
          />
        ))}
      </ul>
    </li>
  );
}

function IssueRow({ issue, taskHref }: { issue: QaIssue; taskHref?: string }) {
  return (
    <li className="space-y-1.5 py-2.5">
      <p className="text-sm font-medium">{issue.title}</p>
      <div className="text-muted-foreground flex flex-wrap items-center gap-1.5 text-xs">
        <span className={issue.blocking ? BLOCKING_PILL : `${PILL} bg-muted`}>
          {issue.blocking ? "Blocking" : "Not blocking"}
        </span>
        <span>{issue.found_by}</span>
        {issue.date && <span>{new Date(issue.date).toLocaleDateString()}</span>}
        {issue.file && <span className="font-mono">{issue.file}</span>}
        {issue.trial_id && taskHref && (
          <Link
            href={`${taskHref}?trial=${encodeURIComponent(issue.trial_id)}`}
            className="text-foreground hover:underline"
          >
            Trial
          </Link>
        )}
        <span className="ml-auto font-mono">
          {[issue.severity, issue.label].filter(Boolean).join(" · ")}
        </span>
      </div>
      {issue.text && (
        <p className="bg-muted/50 rounded-md p-2.5 text-xs leading-relaxed whitespace-pre-wrap">
          {issue.text}
        </p>
      )}
    </li>
  );
}
