// Live QA issue report — mirrors QAIssueReportResponse in oddish/schemas.py.

export interface QaIssue {
  category: string;
  kind: "finding" | "trial" | "verdict";
  blocking: boolean;
  severity: string | null;
  found_by: string;
  label: string;
  title: string;
  text: string;
  file?: string | null;
  trial_id: string | null;
  date: string | null;
}

export interface QaIssueTask {
  task_id: string;
  task_name: string;
  qa_status: string;
  author: string | null;
  pr_url: string | null;
  pr_number: string | null;
  blocking_issues: number;
  issues: QaIssue[];
}

export interface QaIssueCategory {
  slug: string;
  name: string;
  dimension: string;
  description: string;
  issues: number;
  blocking_issues: number;
  tasks: number;
}

export interface QaBurndownPoint {
  /** null for the last point: the live headline ("Now"). */
  version: number | null;
  blocked: number;
  clear: number;
  waiting: number;
}

// Stacked from the baseline: blocked, clear, waiting. Fill classes match the
// task split legend on the QA issues page.
export const BURNDOWN_SERIES = [
  { key: "blocked", label: "Blocked", fill: "fill-red-500", dot: "bg-red-500" },
  {
    key: "clear",
    label: "Clear",
    fill: "fill-emerald-500",
    dot: "bg-emerald-500",
  },
  {
    key: "waiting",
    label: "Waiting for QA",
    fill: "fill-muted-foreground/30",
    dot: "bg-muted-foreground/30",
  },
] as const;

export interface QaIssueReport {
  scope_name: string;
  /** blocked + clear + waiting = tasks */
  summary: { tasks: number; blocked: number; clear: number; waiting: number };
  /** The same split at each task version number, then Now (= summary). */
  burndown: QaBurndownPoint[];
  categories: QaIssueCategory[];
  category: string | null;
  task_total: number;
  tasks: QaIssueTask[];
}

export type QaIssueReportResult =
  | { ok: true; report: QaIssueReport }
  | { ok: false; status: number; detail?: string };

/** URL state of the QA issues page; every control is a link or a GET form. */
export interface QaIssuesView {
  category?: string;
  blocking: boolean;
  q: string;
  page: number;
}

export function parseQaIssuesView(
  params: Record<string, string | string[] | undefined>
): QaIssuesView {
  const one = (key: string) => {
    const value = params[key];
    return (Array.isArray(value) ? value[0] : value) ?? "";
  };
  return {
    category: one("category") || undefined,
    blocking: one("blocking") === "1",
    q: one("q"),
    page: Math.max(1, Number.parseInt(one("page"), 10) || 1),
  };
}

function qaIssuesParams(view: Partial<QaIssuesView>) {
  const params = new URLSearchParams();
  if (view.category) params.set("category", view.category);
  if (view.blocking) params.set("blocking", "1");
  if (view.q) params.set("q", view.q);
  if (view.page && view.page > 1) params.set("page", String(view.page));
  return params;
}

export const PAGE_SIZE = 25;
/** One fetch holds this many tasks: the page on screen and the copy button. */
export const COPY_LIMIT = 500;

/** Backend query for a view: the COPY_LIMIT window that holds its page. */
export function qaIssuesFetchParams(view: QaIssuesView) {
  const params = qaIssuesParams({ ...view, page: 1 });
  if (view.category) {
    const window = Math.floor(((view.page - 1) * PAGE_SIZE) / COPY_LIMIT);
    params.set("page", String(window + 1));
    params.set("page_size", String(COPY_LIMIT));
  }
  return params;
}

export function qaIssuesHref(base: string, view: Partial<QaIssuesView>) {
  const query = qaIssuesParams(view).toString();
  return query ? `${base}?${query}` : base;
}
