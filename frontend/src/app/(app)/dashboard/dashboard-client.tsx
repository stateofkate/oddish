"use client";

import { useEffect, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { MemberFilterTypeahead } from "@/components/member-filter-typeahead";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useAuth } from "@clerk/nextjs";
import { isOrgAdminRole } from "@/lib/org-roles";
import { TooltipProvider } from "@/components/ui/tooltip";
import { EffortCell } from "@/components/effort-cell";
import { TokenMetricSelect } from "@/components/token-metric-select";
import { selectedTokenMetric, setTokenMetric } from "@/lib/token-metrics";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ExperimentsSkeleton } from "./experiments-skeleton";
import type { DashboardResponse, QueueHealthResponse } from "@/lib/types";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { fetcher } from "@/lib/api";
import { formatTaskAuthor } from "@/lib/format";
import {
  SearchSyntaxHelp,
  SearchSyntaxMultiRow,
  SearchSyntaxRow,
} from "@/components/search-syntax-help";
import { TagChip } from "@/components/tag-chip";
import { TagFilterDropdown } from "@/components/tag-filter-dropdown";
import {
  cn,
  encodeExperimentRouteParam,
  formatShortDateTime,
  prBadge,
} from "@/lib/utils";
import {
  buildDashboardApiPath,
  dashboardExperimentsRequest,
  DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR,
  QUEUE_STATES,
} from "@/lib/dashboard-request";
import { badgeVariants } from "@/components/ui/badge";
import { UsageSummaryCard } from "@/components/usage-overview";
import { useIsCustomerOrg } from "@/lib/use-org-access";
import { QuotaUsageCard } from "@/components/quota-usage-card";
import { OnboardingSteps } from "@/components/onboarding-steps";
import { CostLeaderboardStrip } from "@/components/cost-leaderboard";
import {
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Filter,
  GitPullRequest,
  Trash2,
  Globe,
  Users,
  X,
} from "lucide-react";

// =============================================================================
// Experiments list — stored summaries with account-scoped request reuse
// =============================================================================

const STATUS_FILTER_OPTIONS = [
  { value: "all", label: "All statuses" },
  { value: "active", label: "Active trials" },
  { value: "retrying", label: "Retrying trials" },
  { value: "completed", label: "Completed" },
  { value: "needs-review", label: "Rejected tasks" },
  { value: "pending-verdict", label: "Tasks without a verdict" },
  { value: "failed", label: "Failures" },
] as const;

function MineEmptyExperimentsState({
  onViewOrgExperiments,
}: {
  onViewOrgExperiments: () => void;
}) {
  return (
    <div className="bg-card/60 rounded-lg border border-dashed border-[#6f88b4]/30 p-6">
      <div className="flex flex-col items-center text-center">
        <Users className="text-muted-foreground/70 mb-3 h-11 w-11" />
        <p className="text-base font-medium">No experiments of yours yet</p>
        <p className="text-muted-foreground mt-1 max-w-md text-sm">
          Your organization may have other experiments. Switch to the org view
          to browse everything, or submit a new job to get started.
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-4"
          onClick={onViewOrgExperiments}
        >
          View org experiments
        </Button>
      </div>
    </div>
  );
}

// =============================================================================
// Experiment list — one shared cache per account and complete filter URL
// =============================================================================

function ExperimentsTableBody({
  authorFilter,
  statusFilter,
  searchQuery,
  currentExperimentsPage,
  onPreviousExperimentsPage,
  onNextExperimentsPage,
  onRefreshData,
  onViewOrgExperiments,
}: {
  authorFilter: string;
  statusFilter: string;
  searchQuery: string;
  currentExperimentsPage: number;
  onPreviousExperimentsPage: () => void;
  onNextExperimentsPage: () => void;
  onRefreshData: () => void;
  onViewOrgExperiments: () => void;
}) {
  const { userId, orgId } = useAuth();
  const queryParams = useSearchParams();
  const pathname = usePathname();
  const tokenMetric = selectedTokenMetric(queryParams);
  const showDetails = queryParams.get("experiment_details") === "true";
  const update = (change: (params: URLSearchParams) => void) => {
    const params = new URLSearchParams(window.location.search);
    change(params);
    params.delete("page");
    window.history.pushState(null, "", `${pathname}?${params}`);
  };
  const metricHeader = (label: string, sort: string, token = false, title?: string) => {
    const active = queryParams.get("sort")?.startsWith(`${sort}_`);
    const ascending = active && queryParams.get("sort")?.endsWith("_asc");
    const Icon = active ? (ascending ? ArrowUp : ArrowDown) : ArrowUpDown;
    return (
      <TableHead
        className="w-px whitespace-nowrap text-right"
        title={title}
        aria-sort={active ? (ascending ? "ascending" : "descending") : "none"}
      >
        <div className="inline-flex items-center gap-1 [&_select]:field-sizing-content [&_select]:max-w-48">
          {token && (
            <TokenMetricSelect
              value={tokenMetric.value}
              onChange={(value) =>
                update((params) => {
                  setTokenMetric(params, value);
                })
              }
            />
          )}
          <button
            type="button"
            className="inline-flex items-center gap-1 py-2 hover:text-foreground"
            aria-label={`Sort by ${label.toLowerCase()}`}
            onClick={() =>
              update((params) =>
                params.set(
                  "sort",
                  `${sort}_${active && !ascending ? "asc" : "desc"}`,
                ),
              )
            }
          >
            {!token && label}
            <Icon className="h-3 w-3" aria-hidden />
          </button>
        </div>
      </TableHead>
    );
  };
  const requestUrl = buildDashboardApiPath(
    dashboardExperimentsRequest(new URLSearchParams(queryParams.toString()))
  );
  const { data, error, isLoading } = useSWR<DashboardResponse>(
    userId && orgId
      ? ["dashboard-experiments", userId, orgId, requestUrl]
      : null,
    ([, , , url]: string[]) =>
      fetcher<DashboardResponse>(url, { cache: "no-store" }),
    {
      revalidateOnFocus: false,
      revalidateIfStale: false,
      dedupingInterval: 2_000,
      refreshInterval: (current) =>
        current?.experiments?.some(
          (row) => row.summary_pending || row.active_trials > 0
        )
          ? 5_000
          : 30_000,
    }
  );
  const experiments = data?.experiments ?? [];
  const hasMore = data?.experiments_has_more ?? false;
  const ok = !error;
  // The qa-report experiment is a debug surface; verdicts live inline on the
  // experiment page. Only admins get a pointer to the machinery.
  const canSeeQaReport = isOrgAdminRole(useAuth().orgRole);

  const [deleteTarget, setDeleteTarget] = useState<{
    id: string;
    name: string;
    counts: { tasks: number; trials: number } | null;
  } | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const searchParams = useSearchParams();
  const trialFiltersActive = TRIAL_FILTER_PARAM_KEYS.some((key) =>
    searchParams.get(key)
  );
  const isMemberSelected = authorFilter !== "all" && authorFilter !== "me";
  const hasFilters =
    searchQuery.trim().length > 0 ||
    statusFilter !== "all" ||
    isMemberSelected ||
    trialFiltersActive;

  const handleDeleteExperiment = async () => {
    if (!deleteTarget || isDeleting) return;
    setIsDeleting(true);
    setDeleteError(null);

    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(deleteTarget.id)}`,
        { method: "DELETE" }
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || errorData.error || "Failed to delete experiment"
        );
      }

      onRefreshData();
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(
        error instanceof Error ? error.message : "Failed to delete experiment"
      );
    } finally {
      setIsDeleting(false);
    }
  };

  const headers = (
    <>
      <TableHead>Experiment</TableHead>
      <TableHead>Author</TableHead>
      {showDetails && <TableHead>Last run</TableHead>}
      {showDetails && <TableHead>PR</TableHead>}
      <TableHead>Tasks</TableHead>
      <TableHead>Trials</TableHead>
      {metricHeader("Avg steps", "avg_steps")}
      {metricHeader(`Avg ${tokenMetric.label} tokens`, tokenMetric.sort, true)}
      {metricHeader("Avg trial cost", "avg_cost")}
      {showDetails && <TableHead className="text-right">Last task</TableHead>}
      {metricHeader("Avg score", "avg_score", false, "Average of per-task average reward, nop/oracle excluded")}
      <TableHead className="text-right">Delete</TableHead>
    </>
  );

  if (isLoading || (!data && !error))
    return (
      <ExperimentsSkeleton headers={headers} columns={showDetails ? 12 : 9} />
    );

  return (
    <TooltipProvider>
      <div className="text-muted-foreground mb-3 text-[11px]">
        Showing {experiments.length}
        {" • "}
        Page {currentExperimentsPage}
      </div>
      {!ok && experiments.length === 0 ? (
        <Alert variant="destructive">
          <AlertTitle>Failed to load experiments</AlertTitle>
          <AlertDescription>
            Try refreshing the page. If this keeps happening, contact Abundant.
          </AlertDescription>
        </Alert>
      ) : experiments.length === 0 && !hasMore && !hasFilters ? (
        authorFilter === "me" ? (
          <MineEmptyExperimentsState
            onViewOrgExperiments={onViewOrgExperiments}
          />
        ) : (
          <OnboardingSteps />
        )
      ) : experiments.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center">
          <p>No experiments match the current filters.</p>
        </div>
      ) : (
        <div className="min-h-[560px] overflow-x-hidden">
          <Table>
            <TableHeader><TableRow>{headers}</TableRow></TableHeader>
            <TableBody className="[&_td]:text-xs">
              {experiments.map((experiment) => {
                const avgScorePct =
                  experiment.avg_score != null
                    ? Math.round(experiment.avg_score * 100)
                    : null;
                const retryingTrials = Number(experiment.retrying_trials) || 0;

                return (
                  <TableRow key={experiment.id}>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <Link
                          href={`/experiments/${encodeExperimentRouteParam(
                            experiment.id
                          )}`}
                          className="text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
                        >
                          {experiment.name}
                        </Link>
                        {experiment.is_public && (
                          <Globe
                            className="text-muted-foreground h-3.5 w-3.5"
                            aria-label="Published experiment"
                          />
                        )}
                        {experiment.qa_report_experiment_id &&
                          canSeeQaReport && (
                            <Link
                              href={`/experiments/${encodeExperimentRouteParam(
                                experiment.qa_report_experiment_id
                              )}`}
                              className="text-muted-foreground rounded border border-amber-500/30 px-1 py-px text-[10px] leading-none whitespace-nowrap hover:border-amber-500/60 hover:underline"
                              title="Open this experiment's QA report"
                            >
                              qa report
                            </Link>
                          )}
                      </div>
                      {(experiment.user_tags?.length ?? 0) > 0 && (
                        <div className="mt-0.5 flex flex-wrap items-center gap-1">
                          {experiment.user_tags!.map((t) => (
                            <TagChip key={t.tag_id} tag={t} />
                          ))}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs whitespace-nowrap">
                      <span className="text-foreground/80">
                        {formatTaskAuthor(
                          experiment.author ?? experiment.last_author
                        )}
                      </span>
                    </TableCell>
                    {showDetails && <><TableCell className="text-muted-foreground text-xs whitespace-nowrap">
                      <span className="text-foreground/80">
                        {formatTaskAuthor(
                          experiment.last_runner ?? experiment.last_author
                        )}
                      </span>
                    </TableCell>
                    <TableCell className="text-xs whitespace-nowrap">
                      {experiment.last_pr_url ? (
                        <Link
                          href={experiment.last_pr_url}
                          target="_blank"
                          rel="noreferrer"
                          title={
                            experiment.last_pr_title
                              ? `${experiment.last_pr_title} — view on GitHub`
                              : "View pull request on GitHub"
                          }
                          className={cn(
                            badgeVariants({ variant: "outline" }),
                            "hover:bg-accent max-w-[200px] gap-1.5 font-mono text-[11px] transition-colors"
                          )}
                        >
                          <GitPullRequest
                            className="h-3 w-3 shrink-0"
                            aria-hidden
                          />
                          {(() => {
                            const { label, number } = prBadge(
                              experiment.last_pr_url,
                              experiment.last_pr_number
                            );
                            return (
                              <span className="min-w-0 truncate">
                                {label}
                                {number && (
                                  <span className="text-muted-foreground">
                                    {" "}
                                    #{number}
                                  </span>
                                )}
                              </span>
                            );
                          })()}
                          <ExternalLink
                            className="h-3 w-3 shrink-0 opacity-50"
                            aria-hidden
                          />
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell></>}
                    <TableCell>
                      {experiment.summary_pending
                        ? "Preparing…"
                        : experiment.task_count}
                    </TableCell>
                    <TableCell className="font-mono text-xs whitespace-nowrap">
                      {/* done = terminal (success + failed + skipped), so a
                          finished experiment reads N/N, not "2/5". The (R)/(F)/(S)
                          suffixes break down the composition. */}
                      {experiment.summary_pending ? (
                        "—"
                      ) : (
                        <>
                          {experiment.completed_trials +
                            experiment.failed_trials +
                            experiment.skipped_trials}
                          /{experiment.total_trials}
                        </>
                      )}
                      {retryingTrials > 0 && (
                        <span className="text-amber-500 dark:text-amber-300">
                          {" "}
                          ({retryingTrials}R)
                        </span>
                      )}
                      {experiment.failed_trials > 0 && (
                        <span className="text-rose-400">
                          {" "}
                          ({experiment.failed_trials}F)
                        </span>
                      )}
                      {experiment.skipped_trials > 0 && (
                        <span className="text-muted-foreground">
                          {" "}
                          ({experiment.skipped_trials}S)
                        </span>
                      )}
                    </TableCell>
                    {["steps", tokenMetric.field, "cost"].map(metric => <TableCell key={metric} className="text-right">
                      <EffortCell effort={experiment.effort} metric={metric as "steps" | typeof tokenMetric.field | "cost"} scope="This experiment's selected task versions." />
                    </TableCell>)}
                    {showDetails && <TableCell className="text-muted-foreground text-right text-xs whitespace-nowrap">
                      {experiment.last_created_at
                        ? formatShortDateTime(experiment.last_created_at)
                        : "—"}
                    </TableCell>}
                    <TableCell className="font-mono text-xs">
                      {avgScorePct === null ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        <span
                          className={
                            avgScorePct >= 80
                              ? "text-[#5c8e43] dark:text-[#85b85c]"
                              : avgScorePct >= 35
                                ? "text-yellow-400"
                                : "text-rose-400"
                          }
                        >
                          {avgScorePct}%
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() =>
                          setDeleteTarget({
                            id: experiment.id,
                            name: experiment.name,
                            counts: experiment.summary_pending
                              ? null
                              : {
                                  tasks: experiment.task_count,
                                  trials: experiment.total_trials,
                                },
                          })
                        }
                        disabled={
                          experiment.id === "uncategorized" ||
                          experiment.name === "Uncategorized"
                        }
                        className="text-destructive hover:text-destructive h-8 w-8"
                        aria-label={`Delete ${experiment.name}`}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          <div className="mt-3 flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 px-3 text-[11px]"
              onClick={onPreviousExperimentsPage}
              disabled={currentExperimentsPage <= 1}
            >
              <ChevronLeft className="mr-1 h-3.5 w-3.5" />
              Previous page
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 px-3 text-[11px]"
              onClick={onNextExperimentsPage}
              disabled={!hasMore}
            >
              Next page
              <ChevronRight className="ml-1 h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      )}
      <AlertDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteTarget(null);
            setDeleteError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this experiment?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes{" "}
              <span className="text-foreground font-medium">
                {deleteTarget?.name}
              </span>{" "}
              {deleteTarget?.counts
                ? `and removes ${deleteTarget.counts.tasks} tasks and ${deleteTarget.counts.trials} trials.`
                : "and its associated tasks and trials. Counts are still being prepared."}{" "}
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && (
            <Alert variant="destructive">
              <AlertTitle>Delete failed</AlertTitle>
              <AlertDescription>{deleteError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteExperiment}
              disabled={isDeleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDeleting ? "Deleting..." : "Delete experiment"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </TooltipProvider>
  );
}

// =============================================================================
// Recent Experiments card — filter controls and cached rows
// =============================================================================

function RecentTasksCard({
  searchQuery,
  onSearchQueryChange,
  statusFilter,
  onStatusFilterChange,
  authorFilter,
  onAuthorFilterChange,
  currentExperimentsPage,
  onPreviousExperimentsPage,
  onNextExperimentsPage,
  onRefreshData,
  onViewOrgExperiments,
}: {
  searchQuery: string;
  onSearchQueryChange: (value: string) => void;
  statusFilter: string;
  onStatusFilterChange: (value: string) => void;
  authorFilter: string;
  onAuthorFilterChange: (value: string) => void;
  currentExperimentsPage: number;
  onPreviousExperimentsPage: () => void;
  onNextExperimentsPage: () => void;
  onRefreshData: () => void;
  onViewOrgExperiments: () => void;
}) {
  const statusFilterLabel =
    STATUS_FILTER_OPTIONS.find((option) => option.value === statusFilter)
      ?.label ?? "Filter status";

  return (
    <Card className="col-span-5 border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <CardTitle className="text-base">Recent Experiments</CardTitle>
        </div>
        <div className="flex flex-1 flex-wrap gap-2 sm:justify-end">
          {/* Owner filter: whole org by default; "Mine" or one member. */}
          <MemberFilterTypeahead
            selectedUserId={authorFilter === "all" ? null : authorFilter}
            onSelect={(userId) => onAuthorFilterChange(userId ?? "all")}
          />
          <div className="relative w-full sm:w-[220px]">
            <Input
              value={searchQuery}
              onChange={(event) => onSearchQueryChange(event.target.value)}
              placeholder="Search anything..."
              className="h-8 w-full border-[#6f88b4]/20 pr-7"
            />
            <SearchSyntaxHelp>
              <p className="font-medium">Search syntax</p>
              <p className="text-muted-foreground">
                Matches experiment name, author, or tags. Add a prefix below to
                specify filters.
              </p>
              <SearchSyntaxRow
                example="cybersecurity agent"
                hint="every word must match (AND)"
              />
              <SearchSyntaxRow
                example="daytona OR modal"
                hint="either word (OR)"
              />
              <SearchSyntaxRow example={'"exact name"'} hint="exact phrase" />
              <SearchSyntaxRow example="-wip" hint="exclude" />
              <SearchSyntaxMultiRow
                examples={["github:alice", "author:alice", "user:alice"]}
                hint="by author — GitHub handle, email, or name"
              />
              <SearchSyntaxRow example="tag:smoke" hint="by a specific tag" />
              <p className="text-muted-foreground">
                Filters stack (AND) and are case-insensitive, e.g.{" "}
                <code className="bg-muted rounded px-1 font-mono">
                  github:alice tag:smoke
                </code>
              </p>
            </SearchSyntaxHelp>
          </div>
          <TagFilterDropdown
            query={searchQuery}
            onQueryChange={onSearchQueryChange}
            countField="experiment_count"
          />
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 w-full justify-between border-[#6f88b4]/20 sm:w-[170px]"
              >
                <span className="truncate">{statusFilterLabel}</span>
                <ChevronDown className="h-4 w-4 opacity-50" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-[170px]">
              <DropdownMenuRadioGroup
                value={statusFilter}
                onValueChange={onStatusFilterChange}
              >
                {STATUS_FILTER_OPTIONS.map((option) => (
                  <DropdownMenuRadioItem
                    key={option.value}
                    value={option.value}
                  >
                    {option.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <ExperimentColumns />
          <ExperimentTrialFilters />
        </div>
      </CardHeader>
      <CardContent>
        <ExperimentsTableBody
          authorFilter={authorFilter}
          statusFilter={statusFilter}
          searchQuery={searchQuery}
          currentExperimentsPage={currentExperimentsPage}
          onPreviousExperimentsPage={onPreviousExperimentsPage}
          onNextExperimentsPage={onNextExperimentsPage}
          onRefreshData={onRefreshData}
          onViewOrgExperiments={onViewOrgExperiments}
        />
      </CardContent>
    </Card>
  );
}

function ExperimentColumns() {
  const params = useSearchParams();
  const pathname = usePathname();
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8">
          Columns
          <ChevronDown className="ml-1 h-3.5 w-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuCheckboxItem
          checked={params.get("experiment_details") === "true"}
          onCheckedChange={(checked) => {
            const next = new URLSearchParams(window.location.search);
            if (checked) next.set("experiment_details", "true");
            else next.delete("experiment_details");
            window.history.pushState(null, "", `${pathname}?${next}`);
          }}
        >
          Last run, PR and last task
        </DropdownMenuCheckboxItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const AVERAGE_FILTER_GROUPS = [
  { label: "Avg steps", keys: ["min_avg_steps", "max_avg_steps"] },
  { label: "Avg trial cost ($)", keys: ["min_avg_cost", "max_avg_cost"] },
  { label: "Avg tokens", keys: ["min_avg_tokens", "max_avg_tokens"] },
];

// Trial filter groups managed by the popover, keyed by their URL params.
// Mirrors the tasks sidebar's Trial group (label + per-group clear + h-8
// controls) so both surfaces read as the same filter system.
const TRIAL_FILTER_GROUPS: {
  label: string;
  keys: [string] | [string, string];
  placeholder?: string;
}[] = [
  { label: "Model", keys: ["models"], placeholder: "e.g. openai/gpt-5" },
  {
    label: "Queue",
    keys: ["queue"],
    placeholder: "queue key, e.g. openai/gpt-5",
  },
  { label: "Trajectory length", keys: ["min_steps", "max_steps"] },
  {
    label: "Trajectory time (s)",
    keys: ["min_duration_seconds", "max_duration_seconds"],
  },
  { label: "Tool calls", keys: ["min_tool_calls", "max_tool_calls"] },
  {
    label: "Tool names",
    keys: ["tool_names"],
    placeholder: "bash, read_file",
  },
];

// Every URL param the trial filters own; the Suspense key and empty-state
// checks read these so metric-filter changes behave like any other filter.
const TRIAL_FILTER_PARAM_KEYS = [
  ...AVERAGE_FILTER_GROUPS.flatMap((group) => group.keys),
  ...TRIAL_FILTER_GROUPS.flatMap((group) => group.keys),
  "trial_metric_match",
  "queue_state",
];

function ExperimentTrialFilters() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [open, setOpen] = useState(false);
  // Active queue keys come from the admin-only queue health endpoint; anyone
  // else (or a failed fetch) gets the plain text box. keepPreviousData keeps
  // the list through the close animation, when the key goes back to null.
  const isOrgAdmin = isOrgAdminRole(useAuth().orgRole);
  const { data: queueHealth, error: queueHealthError } =
    useSWR<QueueHealthResponse>(
      open && isOrgAdmin ? "/api/admin/queue-health" : null,
      fetcher,
      { keepPreviousData: true }
    );
  const showQueuePicker = isOrgAdmin && !queueHealthError;
  // Scheduled retries still match the experiments filter, and the applied
  // queue stays listed even after it drains so it can be toggled off.
  const selectedQueue = searchParams.get("queue");
  const activeQueues = queueHealth?.capacity
    .map((row) => ({
      queue_key: row.queue_key,
      queued: row.queued + row.queued_scheduled,
      running: row.running,
    }))
    .filter(
      (row) =>
        row.queued > 0 || row.running > 0 || row.queue_key === selectedQueue
    );
  if (
    activeQueues &&
    selectedQueue &&
    !activeQueues.some((row) => row.queue_key === selectedQueue)
  ) {
    activeQueues.unshift({ queue_key: selectedQueue, queued: 0, running: 0 });
  }

  const commit = (patch: Record<string, string>) => {
    const next = new URLSearchParams(window.location.search);
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    // Keep committed ranges ordered — the server rejects min > max.
    for (const group of [...AVERAGE_FILTER_GROUPS, ...TRIAL_FILTER_GROUPS]) {
      if (group.keys.length !== 2) continue;
      const [minKey, maxKey] = group.keys;
      const min = Number(next.get(minKey));
      const max = Number(next.get(maxKey));
      if (
        next.get(minKey) &&
        next.get(maxKey) &&
        Number.isFinite(min) &&
        Number.isFinite(max) &&
        min > max
      ) {
        next.set(minKey, String(max));
        next.set(maxKey, String(min));
      }
    }
    if (!next.get("queue")) next.delete("queue_state");
    next.delete("page");
    const queryString = next.toString();
    window.history.pushState(
      null,
      "",
      queryString ? `${pathname}?${queryString}` : pathname
    );
  };

  const mode = searchParams.get("trial_metric_match") === "all" ? "all" : "any";
  const groupActive = (keys: readonly string[]) =>
    keys.some((key) => searchParams.get(key));
  const activeCount =
    [...AVERAGE_FILTER_GROUPS, ...TRIAL_FILTER_GROUPS].filter((group) => groupActive(group.keys)).length +
    (mode === "all" ? 1 : 0);
  const clearGroup = (keys: readonly string[]) =>
    commit(Object.fromEntries(keys.map((key) => [key, ""])));
  const help =
    "Any trial matches when one selected-model trial meets every metric constraint. All trials requires every selected-model trial to meet every constraint.";

  const field = (key: string, placeholder: string, numeric: boolean, label?: string) => (
    <Input
      key={`${key}-${searchParams.get(key) ?? ""}`}
      defaultValue={searchParams.get(key) ?? ""}
      placeholder={placeholder}
      type={numeric ? "number" : "text"}
      min={numeric ? 0 : undefined}
      step={label ? "any" : undefined}
      aria-label={label}
      className="h-8 text-xs"
      onBlur={(event) => commit({ [key]: event.target.value.trim() })}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
      }}
    />
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 border-[#6f88b4]/20"
        >
          <Filter className="mr-1 h-3.5 w-3.5" />
          Filters
          {activeCount > 0 ? (
            <span className="text-muted-foreground ml-1 text-[11px]">
              ({activeCount})
            </span>
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 max-h-[var(--radix-popover-content-available-height)] overflow-y-auto p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-sm font-medium">
            <Filter className="h-3.5 w-3.5" />
            Filters
            {activeCount > 0 ? (
              <span className="text-muted-foreground text-[11px]">
                ({activeCount})
              </span>
            ) : null}
          </span>
          {activeCount > 0 ? (
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground text-[11px]"
              onClick={() =>
                commit(
                  Object.fromEntries(
                    [
                      ...AVERAGE_FILTER_GROUPS.flatMap((group) => group.keys),
                      ...TRIAL_FILTER_GROUPS.flatMap((group) => group.keys),
                      "trial_metric_match",
                    ].map((key) => [key, ""])
                  )
                )
              }
            >
              Clear all
            </button>
          ) : null}
        </div>
        <Tabs defaultValue="averages">
          <TabsList className="mb-3 w-full"><TabsTrigger value="averages" className="flex-1">Averages</TabsTrigger><TabsTrigger value="trials" className="flex-1">Trial metrics</TabsTrigger></TabsList>
          <TabsContent value="averages" className="space-y-3">
            <p className="text-xs text-muted-foreground">Completed model trials in this experiment's selected task versions.</p>
            {AVERAGE_FILTER_GROUPS.map(group => <div key={group.label}>
              <div className="mb-1.5 flex justify-between text-xs font-medium">
                <span>{group.label}{group.label === "Avg tokens" && ` · ${selectedTokenMetric(searchParams).label}`}</span>
                {groupActive(group.keys) && <button type="button" aria-label={`Clear ${group.label} filter`} onClick={() => clearGroup(group.keys)}><X className="h-3 w-3" /></button>}
              </div>
              <div className="flex items-center gap-1">{field(group.keys[0], "min", true, `Minimum ${group.label.toLowerCase()}`)}<span className="text-xs text-muted-foreground">–</span>{field(group.keys[1], "max", true, `Maximum ${group.label.toLowerCase()}`)}</div>
            </div>)}
          </TabsContent>
          <TabsContent value="trials" className="space-y-3">
          {TRIAL_FILTER_GROUPS.map((group) => (
            <div
              key={group.label}
              className="border-b border-[#6f88b4]/10 pb-3"
            >
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-xs font-medium">{group.label}</span>
                {groupActive(group.keys) ? (
                  <button
                    type="button"
                    aria-label={`Clear ${group.label} filter`}
                    className="text-muted-foreground hover:text-foreground"
                    onClick={() => clearGroup(group.keys)}
                  >
                    <X className="h-3 w-3" />
                  </button>
                ) : null}
              </div>
              {group.keys.length === 2 ? (
                <div className="flex items-center gap-1">
                  {field(group.keys[0], "min", true)}
                  <span className="text-muted-foreground text-xs">–</span>
                  {field(group.keys[1], "max", true)}
                </div>
              ) : group.keys[0] === "queue" && showQueuePicker ? (
                <div className="max-h-48 space-y-0.5 overflow-y-auto">
                  {!activeQueues ? (
                    <p className="text-muted-foreground text-xs">Loading…</p>
                  ) : activeQueues.length === 0 ? (
                    <p className="text-muted-foreground text-xs">
                      Nothing queued or running.
                    </p>
                  ) : null}
                  {activeQueues?.map((row) => {
                    const selected = selectedQueue === row.queue_key;
                    return (
                      <button
                        key={row.queue_key}
                        type="button"
                        title={`${row.queued} queued, ${row.running} running`}
                        className={cn(
                          "hover:bg-muted flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left",
                          selected && "bg-muted"
                        )}
                        onClick={() =>
                          commit({ queue: selected ? "" : row.queue_key })
                        }
                      >
                        <QueueKeyIcon queueKey={row.queue_key} size={12} />
                        <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
                          {row.queue_key}
                        </span>
                        <span className="text-muted-foreground shrink-0 text-[11px] tabular-nums">
                          {row.queued} · {row.running}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                field(group.keys[0], group.placeholder ?? "", false)
              )}
              {group.keys[0] === "queue" && searchParams.get("queue") ? (
                <div className="mt-1.5 grid grid-cols-3 rounded-md border p-0.5">
                  {(["", ...QUEUE_STATES] as const).map((value) => (
                    <button
                      key={value || "both"}
                      type="button"
                      className={cn(
                        "rounded px-2 py-1.5 text-xs capitalize",
                        (searchParams.get("queue_state") ?? "") === value &&
                          "bg-primary text-primary-foreground"
                      )}
                      onClick={() => commit({ queue_state: value })}
                    >
                      {value || "both"}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
          <div title={help}>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium">Match metrics across</span>
              {mode === "all" ? (
                <button
                  type="button"
                  aria-label="Clear match mode filter"
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => commit({ trial_metric_match: "" })}
                >
                  <X className="h-3 w-3" />
                </button>
              ) : null}
            </div>
            <div className="grid grid-cols-2 rounded-md border p-0.5">
              {(["any", "all"] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  className={cn(
                    "rounded px-2 py-1.5 text-xs capitalize",
                    mode === value && "bg-primary text-primary-foreground"
                  )}
                  onClick={() =>
                    commit({
                      trial_metric_match: value === "all" ? "all" : "",
                    })
                  }
                >
                  {value} trial{value === "all" ? "s" : ""}
                </button>
              ))}
            </div>
          </div>
        </TabsContent>
        </Tabs>
      </PopoverContent>
    </Popover>
  );
}

// =============================================================================
// Main Dashboard
// =============================================================================

export function DashboardClient() {
  const customer = useIsCustomerOrg();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { userId, orgId } = useAuth();
  const { mutate } = useSWRConfig();
  const authorFilter =
    searchParams.get("author") || DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR;
  const statusFilter = searchParams.get("status") || "all";
  const committedQuery = searchParams.get("q") || "";
  const currentExperimentsPage = Math.max(
    1,
    Number.parseInt(searchParams.get("page") || "1", 10) || 1
  );
  const [searchDraft, setSearchDraft] = useState<{
    value: string;
    query: string;
  } | null>(null);
  const searchQuery =
    searchDraft?.query === committedQuery ? searchDraft.value : committedQuery;
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const cancelSearch = () => {
      if (searchTimer.current) clearTimeout(searchTimer.current);
    };
    // Back/Forward restores a URL; a pending search must not rewrite it.
    const handlePopState = () => {
      cancelSearch();
      setSearchDraft(null);
    };
    window.addEventListener("popstate", handlePopState);
    return () => {
      window.removeEventListener("popstate", handlePopState);
      cancelSearch();
    };
  }, [userId, orgId]);

  // Build a dashboard URL for the given selection/page, preserving the current
  // search and any params this helper doesn't manage (e.g. trial metric
  // filters). Defaults are omitted so the clean view stays "/dashboard".
  const buildFilterHref = (overrides: {
    author?: string;
    status?: string;
    page?: number;
  }) => {
    const author = overrides.author ?? authorFilter;
    const status = overrides.status ?? statusFilter;
    const page = overrides.page ?? 1;
    const query = searchQuery.trim();
    const params = new URLSearchParams(window.location.search);
    params.delete("author");
    params.delete("status");
    params.delete("q");
    params.delete("page");
    if (author !== DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR) {
      params.set("author", author);
    }
    if (status !== "all") {
      params.set("status", status);
    }
    if (query) {
      params.set("q", query);
    }
    if (page > 1) {
      params.set("page", String(page));
    }
    const queryString = params.toString();
    return queryString ? `${pathname}?${queryString}` : pathname;
  };

  // The URL owns applied filters; cached results remain usable on revisits.
  const navigateToFilters = (overrides: {
    author?: string;
    status?: string;
    page?: number;
  }) => {
    if (searchTimer.current) clearTimeout(searchTimer.current);
    window.history.pushState(null, "", buildFilterHref(overrides));
    setSearchDraft(null);
  };

  const handleAuthorFilterChange = (author: string) =>
    navigateToFilters({ author });
  const handleStatusFilterChange = (status: string) =>
    navigateToFilters({ status });
  const handlePreviousExperimentsPage = () => {
    if (currentExperimentsPage <= 1) return;
    navigateToFilters({ page: currentExperimentsPage - 1 });
  };
  const handleNextExperimentsPage = () => {
    navigateToFilters({ page: currentExperimentsPage + 1 });
  };
  const handleRefreshData = () => {
    void mutate(
      (key) =>
        Array.isArray(key) &&
        key[0] === "dashboard-experiments" &&
        key[1] === userId &&
        key[2] === orgId,
      undefined,
      { revalidate: true }
    );
  };
  const handleSearchChange = (value: string) => {
    setSearchDraft({ value, query: committedQuery });
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      const params = new URLSearchParams(window.location.search);
      params.delete("page");
      if (value.trim()) params.set("q", value.trim());
      else params.delete("q");
      window.history.replaceState(
        null,
        "",
        `${pathname}${params.size ? `?${params}` : ""}`
      );
      setSearchDraft(null);
    }, 400);
  };

  return (
    <div className="space-y-4">
      {/* Customer orgs never see Abundant's quota, budget, or spend. */}
      {customer === false && (
        <>
          <QuotaUsageCard />
          <UsageSummaryCard />
          <CostLeaderboardStrip />
        </>
      )}
      <RecentTasksCard
        searchQuery={searchQuery}
        onSearchQueryChange={handleSearchChange}
        statusFilter={statusFilter}
        onStatusFilterChange={handleStatusFilterChange}
        authorFilter={authorFilter}
        onAuthorFilterChange={handleAuthorFilterChange}
        currentExperimentsPage={currentExperimentsPage}
        onPreviousExperimentsPage={handlePreviousExperimentsPage}
        onNextExperimentsPage={handleNextExperimentsPage}
        onRefreshData={handleRefreshData}
        onViewOrgExperiments={() => handleAuthorFilterChange("all")}
      />
    </div>
  );
}
