"use client";

import { Fragment, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useSelection } from "./selection-context";
import {
  TASKS_PAGE_SIZE,
  QA_OUTCOME_OPTIONS,
  updateTaskSearchParams,
} from "@/lib/tasks-filters";
import { useTaskBrowse } from "@/lib/use-task-browse";
import { cn } from "@/lib/utils";
import type { TaskBrowseItem } from "@/lib/types";
import { formatCostUsd } from "@/lib/format";
import { TaskCard, TaskDeliveryHistory } from "./task-card";
import { TaskBrowseLink } from "./task-browse-link";
import { TasksGridSkeleton } from "./tasks-grid-skeleton";

function pageHref(sp: URLSearchParams, offset: number): string {
  const params = new URLSearchParams(sp.toString());
  if (offset <= 0) params.delete("offset");
  else params.set("offset", String(offset));
  const query = params.toString();
  return query ? `/tasks?${query}` : "/tasks";
}

const TASK_COLUMNS = [
  {
    label: "QA outcome",
    render: (task: TaskBrowseItem) =>
      QA_OUTCOME_OPTIONS.find((option) => option.value === task.qa_outcome)
        ?.label ?? "Unreviewed",
  },
  {
    label: "Median steps",
    render: (task: TaskBrowseItem) => (
      <>
        <div>{task.steps_p50 ?? "—"}</div>
        <div className="text-muted-foreground text-xs">
          {task.steps_present ?? 0} measured trials
        </div>
      </>
    ),
  },
  {
    label: "Trials",
    render: (task: TaskBrowseItem) => (
      <>
        <div>
          {task.completed_trials}/{task.total_trials} completed
        </div>
        <div className="text-muted-foreground text-xs">
          {task.pending_count} pending · {task.failed_trials} failed
        </div>
      </>
    ),
  },
  { label: "Agents", render: (task: TaskBrowseItem) => task.agent_count ?? 0 },
  {
    label: "Delivery history",
    render: (task: TaskBrowseItem) => <TaskDeliveryHistory task={task} />,
  },
  {
    label: "Cost",
    render: (task: TaskBrowseItem) => formatCostUsd(task.cost_usd),
  },
  {
    label: "Tags",
    render: (task: TaskBrowseItem) =>
      task.user_tags
        .filter((tag) => tag.current)
        .map((tag) => tag.key)
        .join(", ") || "—",
  },
];

const pagerClass =
  "inline-flex h-8 items-center gap-1 rounded-md border border-[#6f88b4]/30 px-3 text-[11px] transition-colors";

// The browse fetch happens here, on the client, through useTaskBrowse — the
// document streams without waiting for it, and a return visit paints from
// the SWR cache and revalidates in the background. The URL stays the source
// of truth: sidebar writes and the pager links change searchParams, which
// changes the SWR key.
export function RecentTasksResults({
  onClearFilters,
}: {
  onClearFilters: () => void;
}) {
  const searchParams = useSearchParams();
  const { selection, toggle, deliveryId } = useSelection();
  const [expanded, setExpanded] = useState<string | null>(null);
  const view = searchParams.get("view") === "table" ? "table" : "cards";
  const columns = TASK_COLUMNS.map((column) => column.label);
  const visibleColumns = searchParams.has("columns")
    ? searchParams.get("columns")!.split(",")
    : columns.slice(0, 5);
  const shownColumns = TASK_COLUMNS.filter((column) =>
    visibleColumns.includes(column.label)
  );
  const sp = new URLSearchParams(searchParams.toString());
  const offset = Math.max(Number(sp.get("offset") ?? "0") || 0, 0);
  const { data, error, isLoading, mutate } = useTaskBrowse(sp);

  if (!data) {
    if (error) {
      return (
        <Alert variant="destructive">
          <AlertTitle>Failed to load tasks</AlertTitle>
          <AlertDescription>
            Check the API connection and try again.{" "}
            <button
              type="button"
              onClick={() => void mutate()}
              className="font-medium underline underline-offset-2"
            >
              Retry
            </button>
          </AlertDescription>
        </Alert>
      );
    }
    return <TasksGridSkeleton />;
  }

  const items = data.items ?? [];
  const hasMore = data.has_more ?? false;
  // The shown-range label and the pager both describe the rows actually on
  // screen, so their offset comes from the response — during a pager
  // transition (and after a failed one) the URL already points at the next
  // page, and pager targets computed from it would let a second click skip
  // a page keepPreviousData never showed.
  const shownOffset = data.offset ?? offset;

  // isLoading while data is present = a different key (filter/pager change)
  // is in flight and keepPreviousData is showing the previous state, dimmed.
  // Background revalidation of the current key never dims.
  //
  // A failed fetch also keeps the previous state on screen — and it may
  // belong to the previous filter — so it must never pass as current
  // silently: the banner says so and offers a retry. Cold-load failures
  // render the full alert above instead.
  const errorBanner = error ? (
    <Alert variant="destructive">
      <AlertTitle>Failed to update tasks</AlertTitle>
      <AlertDescription>
        Showing the last loaded results.{" "}
        <button
          type="button"
          onClick={() => void mutate()}
          className="font-medium underline underline-offset-2"
        >
          Retry
        </button>
      </AlertDescription>
    </Alert>
  ) : null;

  if (items.length === 0) {
    return (
      <div className="space-y-4">
        {errorBanner}
        <div
          className={cn(
            "bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm transition-opacity",
            isLoading && "opacity-60"
          )}
        >
          <p>No tasks match the current filters.</p>
          <Button variant="ghost" onClick={onClearFilters}>
            Clear filters
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn("space-y-4 transition-opacity", isLoading && "opacity-60")}
    >
      {errorBanner}
      <div className="flex justify-end gap-2">
        {view === "table" ? <details className="relative rounded-md border px-3 py-2 text-sm">
          <summary className="cursor-pointer">Columns</summary>
          <div className="bg-popover absolute right-0 z-20 mt-3 w-48 space-y-2 rounded-md border p-3 shadow-md">
            {columns.map((column) => (
              <label key={column} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={visibleColumns.includes(column)}
                  onChange={(event) =>
                    updateTaskSearchParams((params) => {
                      const current = new Set(
                        params.has("columns")
                          ? params.get("columns")!.split(",").filter(Boolean)
                          : columns.slice(0, 5)
                      );
                      if (event.target.checked) current.add(column);
                      else current.delete(column);
                      params.set("columns", Array.from(current).join(","));
                    }, { resetPage: false })
                  }
                />
                {column}
              </label>
            ))}
          </div>
        </details> : null}
        <select
          aria-label="Task view"
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          value={view}
          onChange={(event) =>
            updateTaskSearchParams((params) =>
              params.set("view", event.target.value), { resetPage: false }
            )
          }
        >
          <option value="table">Table</option>
          <option value="cards">Cards</option>
        </select>
      </div>
      {view === "table" ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-muted-foreground border-b text-xs">
              <tr>
                <th className="p-3">Select</th>
                <th>Task</th>
                {shownColumns.map(column => <th key={column.label}>{column.label}</th>)}
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {items.map((task) => (
                <Fragment key={task.id}>
                  <tr className="border-b">
                    <td className="p-3">
                      <input
                        type="checkbox"
                        checked={selection.has(task.id) || Boolean(task.active_deliveries?.some(d => d.delivery_id === deliveryId))}
                        disabled={Boolean(task.active_deliveries?.some(d => d.delivery_id === deliveryId)) || isLoading || Boolean(error)}
                        aria-label={`Select ${task.name}`}
                        onChange={() => toggle(task)}
                      />
                    </td>
                    <td className="py-3">
                      <TaskBrowseLink
                        task={task}
                        className="font-medium underline-offset-4 hover:underline"
                      />
                      <div className="text-muted-foreground text-xs">
                        v{task.current_version ?? "—"}
                      </div>
                    </td>
                    {shownColumns.map(column => <td key={column.label}>{column.render(task)}</td>)}
                    <td>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-expanded={expanded === task.id}
                        onClick={() =>
                          setExpanded(expanded === task.id ? null : task.id)
                        }
                      >
                        Trial details
                      </Button>
                    </td>
                  </tr>
                  {expanded === task.id ? (
                    <tr>
                      <td colSpan={3 + shownColumns.length} className="p-3">
                        <TaskCard task={task} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {items.map((task) => (
            <TaskCard key={task.id} task={task} />
          ))}
        </div>
      )}

      <div className="flex items-center justify-between gap-2">
        <div className="text-muted-foreground text-xs">
          {shownOffset + 1}-{shownOffset + items.length} shown
        </div>
        <div className="flex items-center gap-2">
          {shownOffset === 0 ? (
            <span
              className={cn(
                pagerClass,
                "text-muted-foreground/50 cursor-not-allowed"
              )}
              aria-disabled
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous page
            </span>
          ) : (
            <Link
              href={pageHref(sp, shownOffset - TASKS_PAGE_SIZE)}
              scroll={false}
              className={cn(pagerClass, "hover:bg-muted")}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous page
            </Link>
          )}
          {hasMore ? (
            <Link
              href={pageHref(sp, shownOffset + TASKS_PAGE_SIZE)}
              scroll={false}
              className={cn(pagerClass, "hover:bg-muted")}
            >
              Next page
              <ChevronRight className="h-3.5 w-3.5" />
            </Link>
          ) : (
            <span
              className={cn(
                pagerClass,
                "text-muted-foreground/50 cursor-not-allowed"
              )}
              aria-disabled
            >
              Next page
              <ChevronRight className="h-3.5 w-3.5" />
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
