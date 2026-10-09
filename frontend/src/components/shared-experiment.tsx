"use client";

import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ExperimentDescription } from "@/components/experiment-description";
import { ExperimentResultsStatus } from "@/components/experiment-results-status";
import type { PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { useExperimentResults } from "@/lib/use-experiment-results";
import { useExperimentCostTotals } from "@/lib/use-experiment-cost-totals";
import { PUBLIC_API_URL } from "@/lib/utils";

/**
 * A shared experiment, read-only, through the share API. ``shareKey`` is the
 * public token on /share pages, or the experiment id for a customer org
 * opening it inside the app.
 */
export function SharedExperiment({
  shareKey,
  pageHref,
}: {
  shareKey: string | undefined;
  /** This page's path; the embedded QA view links back to it. */
  pageHref: string;
}) {
  // Wait for Clerk so a signed-in viewer's first request carries its token:
  // signed out, an experiment id answers 404 like an unknown one.
  const { isLoaded: authLoaded } = useAuth();
  const publicBase =
    shareKey && authLoaded
      ? `${PUBLIC_API_URL}/experiments/${encodeURIComponent(shareKey)}`
      : null;

  const {
    data: experimentInfo,
    error: experimentError,
    isValidating: isLoadingExperimentInfo,
    mutate: refreshExperimentInfo,
  } = useSWR<PublicExperimentInfo>(publicBase, fetcher, {
    refreshInterval: 30000,
  });

  const {
    experiment,
    tasks: tasksForExperiment,
    error: openError,
    isLoading,
    isLoadingTrials,
    trialsLoaded,
    pagesComplete,
    refreshResults,
  } = useExperimentResults({
    url: publicBase ? `${publicBase}/results` : null,
    publicView: true,
  });
  const { resource: costTotals, refresh: refreshCostTotals } =
    useExperimentCostTotals({
      url: publicBase ? `${publicBase}/cost-totals` : null,
      hasActiveTrials: experiment?.has_active_trials ?? false,
    });

  const experimentName =
    experimentInfo?.name || experiment?.name || "Public Experiment";
  const hasFatalError = !experiment && Boolean(openError);
  const scopedApiBaseUrl = publicBase ?? PUBLIC_API_URL;
  const retryExperiment = () => {
    void Promise.allSettled([refreshExperimentInfo(), refreshResults()]);
  };

  // Mount the view once its QA setting is known so task links start in
  // the correct pane. Results and costs still load in parallel above.
  if (!experimentInfo) {
    return (
      <>
        {experimentError ? (
          <ExperimentResultsStatus
            tasksLoaded={tasksForExperiment.length}
            trialsLoaded={trialsLoaded}
            complete={pagesComplete}
            isLoading={isLoadingExperimentInfo}
            hasError
            fatalError={{
              title: "Failed to load experiment",
              description: "The share link may be invalid or no longer public.",
            }}
            onRetry={retryExperiment}
          />
        ) : (
          <p role="status">Loading experiment...</p>
        )}
      </>
    );
  }

  return (
    <div className="space-y-4">
      <ExperimentDetailView
        tasksForExperiment={tasksForExperiment}
        pageSummary={experiment?.summary ?? undefined}
        costTotals={costTotals}
        onRetryCostTotals={() => void refreshCostTotals()}
        isLoading={isLoading}
        isLoadingTrials={isLoadingTrials}
        pagesComplete={pagesComplete}
        focusUrl={publicBase ? `${publicBase}/focus` : undefined}
        hasError={hasFatalError}
        errorTitle="Failed to load experiment"
        errorDescription="The share link may be invalid or no longer public."
        inlineAlert={
          <ExperimentResultsStatus
            summary={experiment?.summary}
            tasksLoaded={tasksForExperiment.length}
            trialsLoaded={trialsLoaded}
            complete={pagesComplete}
            isLoading={isLoadingTrials}
            hasError={Boolean(openError)}
            fatalError={
              hasFatalError
                ? {
                    title: "Failed to load experiment",
                    description:
                      "The share link may be invalid or no longer public.",
                  }
                : undefined
            }
            onRetry={retryExperiment}
          />
        }
        headerLeft={
          <h1 className="truncate pb-1 font-mono text-[26px] leading-[1.25] font-semibold tracking-[-0.02em] text-[color:var(--paper-ink)]">
            {experimentName}
          </h1>
        }
        headerDescription={
          <ExperimentDescription
            description={experimentInfo?.description ?? null}
            readOnly
          />
        }
        qaIssues={
          experimentInfo.show_qa && shareKey
            ? {
                baseHref: pageHref,
                apiHref: `/api/public/experiments/${encodeURIComponent(shareKey)}/qa-issues`,
                linkTasks: false,
              }
            : undefined
        }
        readOnly
        allowRetry={false}
        showAnalysis={experimentInfo?.show_qa === true}
        showCost={experimentInfo.show_costs !== false}
        apiBaseUrl={scopedApiBaseUrl}
      />
    </div>
  );
}
