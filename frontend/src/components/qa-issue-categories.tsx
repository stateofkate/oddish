"use client";

// The category list opens, searches, and pages in place: the URL follows
// with history.pushState, and each category's tasks load once through SWR,
// starting when the pointer reaches the row.
import Link from "next/link";
import { useState } from "react";
import useSWR, { preload, SWRConfig, useSWRConfig } from "swr";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  GitPullRequest,
  Search,
} from "lucide-react";
import { Bar } from "@/components/qa-split";
import { CopyJsonButton } from "@/components/qa-report/copy-json-button";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import { useOrgPath, useQaIssuesView } from "@/lib/use-qa-issues-view";
import { qaStatusLabel } from "@/lib/deliveries";
import type { DeliveryQAStatus } from "@/lib/types";
import {
  COPY_LIMIT,
  PAGE_SIZE,
  qaIssuesFetchParams,
  qaIssuesHref,
  type QaIssue,
  type QaIssueCategory,
  type QaIssueReport,
  type QaIssueTask,
  type QaIssuesView,
} from "@/lib/qa-issues";

const RED = "bg-red-500";
const GREEN = "bg-emerald-500";
const GREY = "bg-muted-foreground/30";
const PILL = "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs";
const BLOCKING_PILL = `${PILL} bg-red-500/10 text-red-700 dark:text-red-300`;

/** A link to this page that keeps the view in the URL and changes ``set``. */
export function QaIssuesLink({
  baseHref,
  set,
  children,
}: {
  baseHref: string;
  set: Partial<QaIssuesView>;
  children: React.ReactNode;
}) {
  const view = useQaIssuesView(baseHref);
  const orgPath = useOrgPath();
  return (
    <Link
      href={orgPath(qaIssuesHref(baseHref, { ...view, page: 1, ...set }))}
      scroll={false}
      prefetch={false}
      className="hover:underline"
    >
      {children}
    </Link>
  );
}

function reportKey(apiHref: string, view: QaIssuesView) {
  return view.category ? `${apiHref}?${qaIssuesFetchParams(view)}` : null;
}

export function QaCategories({
  categories,
  initial,
  baseHref,
  apiHref,
  linkTasks,
}: {
  categories: QaIssueCategory[];
  /** The server's report, which holds tasks when the URL opens a category. */
  initial: QaIssueReport;
  baseHref: string;
  /** The same report through this app's API, for the browser. */
  apiHref: string;
  linkTasks: boolean;
}) {
  const view = useQaIssuesView(baseHref) ?? { blocking: false, q: "", page: 1 };
  // The server fetched for the URL the page loaded with; keep that key.
  const [fallback] = useState<Record<string, QaIssueReport>>(() =>
    initial.category
      ? {
          [reportKey(apiHref, { ...view, category: initial.category })!]:
            initial,
        }
      : {}
  );
  const { cache } = useSWRConfig();
  const orgPath = useOrgPath();
  const go = (next: Partial<QaIssuesView>) =>
    window.history.pushState(
      null,
      "",
      orgPath(qaIssuesHref(baseHref, { ...view, page: 1, ...next }))
    );
  return (
    <SWRConfig value={{ fallback }}>
      {categories.map((category) => {
        const open = view.category === category.slug;
        // The search belongs to the open category; another starts clear.
        const next = {
          version: view.version,
          blocking: false,
          q: "",
          page: 1,
          category: category.slug,
        };
        return (
          <CategoryRow
            key={category.slug}
            category={category}
            scale={categories[0].issues}
            open={open}
            href={orgPath(
              qaIssuesHref(
                baseHref,
                open ? { ...next, category: undefined } : next
              )
            )}
            onOpen={() => go(open ? { ...next, category: undefined } : next)}
            onPrefetch={() => {
              const key = reportKey(apiHref, next)!;
              if (!open && !cache.get(key) && !fallback[key]) {
                preload(key, fetcher);
              }
            }}
          >
            {open && (
              <CategoryDetail
                category={category}
                dataKey={reportKey(apiHref, view)!}
                view={view}
                go={go}
                linkTasks={linkTasks}
              />
            )}
          </CategoryRow>
        );
      })}
    </SWRConfig>
  );
}

function CategoryRow({
  category,
  scale,
  open,
  href,
  onOpen,
  onPrefetch,
  children,
}: {
  category: QaIssueCategory;
  /** The largest category's issue count, so all bars share one scale. */
  scale: number;
  open: boolean;
  href: string;
  onOpen: () => void;
  onPrefetch: () => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <a
        href={href}
        onClick={(event) => {
          if (event.metaKey || event.ctrlKey || event.shiftKey) return;
          event.preventDefault();
          onOpen();
        }}
        onPointerEnter={onPrefetch}
        onFocus={onPrefetch}
        aria-expanded={open}
        title={category.description}
        className="hover:bg-muted/60 focus-visible:ring-ring flex items-center gap-3 rounded-lg px-3 py-2.5 focus-visible:ring-2 focus-visible:outline-none"
      >
        {open ? (
          <ChevronDown className="text-muted-foreground h-4 w-4 shrink-0" />
        ) : (
          <ChevronRight className="text-muted-foreground h-4 w-4 shrink-0" />
        )}
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
              {category.fixed_issues !== undefined && (
                <>
                  {" · "}
                  <span className="text-emerald-700 dark:text-emerald-400">
                    Fixed: {category.fixed_issues}
                  </span>
                  {" · "}Still open: {category.issues - category.fixed_issues}
                </>
              )}
            </span>
            <span className="text-muted-foreground text-xs tabular-nums">
              Tasks: {category.tasks}
              {category.dimension !== "Other" && ` · ${category.dimension}`}
            </span>
          </div>
          <div className="mt-1.5 max-w-xs">
            <Bar
              of={scale}
              parts={
                category.fixed_issues !== undefined
                  ? [
                      [category.fixed_issues, GREEN],
                      [category.issues - category.fixed_issues, RED],
                    ]
                  : [
                      [category.blocking_issues, RED],
                      [category.issues - category.blocking_issues, GREY],
                    ]
              }
            />
          </div>
        </div>
      </a>
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
  category,
  dataKey,
  view,
  go,
  linkTasks,
}: {
  category: QaIssueCategory;
  dataKey: string;
  view: QaIssuesView;
  go: (next: Partial<QaIssuesView>) => void;
  linkTasks: boolean;
}) {
  const { data, error, isValidating } = useSWR<QaIssueReport>(
    dataKey,
    fetcher,
    {
      revalidateIfStale: false,
      revalidateOnFocus: false,
    }
  );
  const pages = Math.max(1, Math.ceil((data?.task_total ?? 0) / PAGE_SIZE));
  // Not clamped: data.tasks is the window for the URL page, so a page past
  // the end shows no tasks and links back to the last page.
  const page = view.page;
  // data.tasks is the COPY_LIMIT window that holds this page.
  const offset = ((page - 1) * PAGE_SIZE) % COPY_LIMIT;
  const shown = data?.tasks.slice(offset, offset + PAGE_SIZE) ?? [];
  return (
    <div className="space-y-3" aria-busy={isValidating}>
      <form
        // Remount on Back/Forward so the fields match the URL's search.
        key={`${view.q}|${view.blocking}`}
        className="flex flex-wrap items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          go({
            category: view.category,
            q: String(form.get("q") ?? ""),
            blocking: form.get("blocking") === "1",
          });
        }}
      >
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
        {data && data.tasks.length > 0 && (
          <CopyJsonButton
            value={copyValue(category, data)}
            label={`${data.tasks.length} tasks`}
            className="self-stretch"
          />
        )}
      </form>
      {error && !data && (
        <p className="text-destructive text-sm">Could not load tasks.</p>
      )}
      {!data && !error && (
        <div className="space-y-2" aria-busy>
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
      )}
      {data && !shown.length && (
        <p className="text-muted-foreground py-6 text-sm">
          {view.q || view.blocking ? "No matches." : "No tasks."}
        </p>
      )}
      <ul className="space-y-2">
        {shown.map((task) => (
          <TaskRow key={task.task_id} task={task} linkTasks={linkTasks} />
        ))}
      </ul>
      {data && (pages > 1 || page > 1) && (
        <nav className="flex items-center justify-between text-sm">
          <PageButton
            onClick={() =>
              go({ category: view.category, page: Math.min(page - 1, pages) })
            }
            disabled={page <= 1}
          >
            Previous
          </PageButton>
          <span className="text-muted-foreground tabular-nums">
            Page {page} of {pages}
          </span>
          <PageButton
            onClick={() => go({ category: view.category, page: page + 1 })}
            disabled={page >= pages}
          >
            Next
          </PageButton>
        </nav>
      )}
    </div>
  );
}

/** What a fixing agent needs: each task and the text of its issues. */
function copyValue(category: QaIssueCategory, data: QaIssueReport) {
  return {
    category: category.name,
    description: category.description,
    tasks: data.tasks.map((task) => ({
      task: task.task_name,
      pr: task.pr_url,
      issues: task.issues.map((issue) => ({
        blocking: issue.blocking,
        found_by: issue.found_by,
        file: issue.file ?? undefined,
        title: issue.title,
        text: issue.text,
        fixed_in: issue.fixed_in,
      })),
    })),
  };
}

function PageButton({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled: boolean;
  children: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="hover:bg-muted disabled:text-muted-foreground/50 rounded-md px-2 py-1 disabled:hover:bg-transparent"
    >
      {children}
    </button>
  );
}

function TaskRow({
  task,
  linkTasks,
}: {
  task: QaIssueTask;
  linkTasks: boolean;
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
        {task.version && (
          <span className="text-muted-foreground font-mono text-xs">
            v{task.version}
          </span>
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
        {issue.fixed_in !== undefined && (
          <span
            className={
              issue.fixed_in
                ? `${PILL} bg-emerald-500/15 font-medium text-emerald-700 dark:text-emerald-400`
                : `${PILL} bg-amber-500/15 font-medium text-amber-700 dark:text-amber-400`
            }
          >
            {issue.fixed_in ? `Fixed in v${issue.fixed_in}` : "Still open"}
          </span>
        )}
        {issue.fixed_in ? (
          <span className={`${PILL} bg-muted`}>
            {issue.blocking ? "Was blocking" : "Was not blocking"}
          </span>
        ) : (
          <span className={issue.blocking ? BLOCKING_PILL : `${PILL} bg-muted`}>
            {issue.blocking ? "Blocking" : "Not blocking"}
          </span>
        )}
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
