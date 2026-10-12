"use client";

import { Fragment, useState } from "react";
import { CheckCircle2, CircleX, TriangleAlert, Clock3 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";

type Monitor = {
  id: string;
  name: string;
  model: string;
  credential_ref: string;
  alerts_enabled: boolean;
  last_outcome: "success" | "failure" | "monitor_error" | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  error: string | null;
  incident_id: string | null;
  incident_opened_at: string | null;
  stale: boolean;
};
type Health = { enabled: boolean; timestamp: string; monitors: Monitor[] };
type Check = {
  id: string;
  checked_at: string;
  outcome: "success" | "failure" | "monitor_error";
  latency_ms: number;
  error: string | null;
  status_code: number | null;
  request_id: string | null;
};

const checkStyles = {
  success: {
    icon: CheckCircle2,
    row: "border-l-emerald-600 bg-emerald-50/60 dark:border-l-emerald-400 dark:bg-emerald-950/20",
    badge:
      "border-emerald-200 bg-emerald-100 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
    iconColor: "text-emerald-700 dark:text-emerald-400",
    label: "Succeeded",
  },
  failure: {
    icon: CircleX,
    row: "border-l-red-600 bg-red-50/60 dark:border-l-red-400 dark:bg-red-950/20",
    badge:
      "border-red-200 bg-red-100 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200",
    iconColor: "text-red-700 dark:text-red-400",
    label: "Failed",
  },
  monitor_error: {
    icon: TriangleAlert,
    row: "border-l-amber-600 bg-amber-50/60 dark:border-l-amber-400 dark:bg-amber-950/20",
    badge:
      "border-amber-200 bg-amber-100 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
    iconColor: "text-amber-700 dark:text-amber-400",
    label: "Check could not run",
  },
  overdue: {
    icon: Clock3,
    row: "border-l-slate-500 bg-slate-50/60 dark:border-l-slate-400 dark:bg-slate-900/30",
    badge:
      "border-slate-200 bg-slate-100 text-slate-800 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200",
    iconColor: "text-slate-600 dark:text-slate-400",
    label: "Checks overdue",
  },
};

export function EndpointHealthCard() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedId = searchParams.get("endpoint");
  const [showAll, setShowAll] = useState(false);
  const [checkRequest, setCheckRequest] = useState<{
    id: string;
    observedAt: string | null;
    state: "sending" | "scheduled" | "error";
  } | null>(null);
  const { data, error, mutate } = useSWR<Health>(
    "/api/admin/endpoint-health",
    fetcher,
    { refreshInterval: 30000 }
  );
  const {
    data: history,
    error: historyError,
    mutate: retryHistory,
  } = useSWR<{ checks: Check[] }>(
    selectedId
      ? `/api/admin/endpoint-health/${encodeURIComponent(selectedId)}/checks`
      : null,
    fetcher,
    { refreshInterval: 30000 }
  );
  const monitors = data?.monitors ?? [];
  const issues = monitors.filter(
    (m) => m.stale || m.incident_id || m.last_outcome !== "success"
  );
  const visible = showAll ? monitors : issues;
  const selected = monitors.find((m) => m.id === selectedId);
  const lastCheck = monitors.reduce<string | null>(
    (oldest, m) =>
      !m.last_checked_at
        ? oldest
        : !oldest || m.last_checked_at < oldest
          ? m.last_checked_at
          : oldest,
    null
  );

  function selectMonitor(id: string | null) {
    const params = new URLSearchParams(searchParams.toString());
    if (id) params.set("endpoint", id);
    else params.delete("endpoint");
    router.push(`/admin?${params}`, { scroll: false });
  }

  async function requestCheck(id: string) {
    setCheckRequest({
      id,
      observedAt: monitors.find((m) => m.id === id)?.last_checked_at ?? null,
      state: "sending",
    });
    try {
      const response = await fetch(
        `/api/admin/endpoint-health/${encodeURIComponent(id)}/check`,
        { method: "POST" }
      );
      if (!response.ok) throw new Error("Could not schedule check");
      setCheckRequest({
        id,
        observedAt: monitors.find((m) => m.id === id)?.last_checked_at ?? null,
        state: "scheduled",
      });
    } catch {
      setCheckRequest({
        id,
        observedAt: monitors.find((m) => m.id === id)?.last_checked_at ?? null,
        state: "error",
      });
    }
  }

  // SWR keeps stale data after a failed refresh, so rows can be hidden
  // while `visible` still lists them.
  const rowsShown = !error && !!data?.enabled;
  const selectedVisible = rowsShown && visible.some((m) => m.id === selectedId);
  const historyPanel = (
    <section
      aria-label="Endpoint check history"
      className="space-y-3 rounded-md border px-3 py-3"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-medium">
          Recent checks{selected ? `: ${selected.name}` : ""}
        </h3>
        <Button size="sm" variant="ghost" onClick={() => selectMonitor(null)}>
          Close history
        </Button>
      </div>
      <p className="text-muted-foreground text-xs">
        Latest 100 checks from the past 30 days. Times use your local timezone.
      </p>
      {selected && (
        <div className="space-y-1 text-sm">
          {selected.error && <p>{selected.error}</p>}
          <p className="text-muted-foreground text-xs">
            Request model: {selected.model} · Credential:{" "}
            {selected.credential_ref}
          </p>
          <p className="text-muted-foreground text-xs">
            Last check:{" "}
            {selected.last_checked_at
              ? new Date(selected.last_checked_at).toLocaleString()
              : "No completed checks"}
          </p>
        </div>
      )}
      {historyError ? (
        <div role="alert">
          Could not load check history.{" "}
          <Button size="sm" variant="outline" onClick={() => retryHistory()}>
            Retry history
          </Button>
        </div>
      ) : !history ? (
        <p role="status">Loading checks…</p>
      ) : history.checks.length === 0 ? (
        <p>No recorded checks.</p>
      ) : (
        <ol className="max-h-96 space-y-3 overflow-y-auto text-sm">
          {history.checks.map((check) => {
            const style = checkStyles[check.outcome];
            const StatusIcon = style.icon;
            return (
              <li key={check.id} className="space-y-2 border-b pb-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge
                    variant="outline"
                    className={cn("gap-1.5", style.badge)}
                  >
                    <StatusIcon aria-hidden="true" className="size-3.5" />
                    {style.label}
                  </Badge>
                  <span>
                    {new Date(check.checked_at).toLocaleString()} ·{" "}
                    {check.latency_ms} ms
                  </span>
                </div>
                {check.error && (
                  <p>
                    {check.error}
                    {check.status_code && ` (HTTP ${check.status_code})`}
                  </p>
                )}
                {check.request_id && (
                  <p className="text-muted-foreground text-xs break-all">
                    Provider request: {check.request_id}
                  </p>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Model endpoint health</CardTitle>
        <p className="text-muted-foreground text-sm">
          Checks run automatically. Check any connection again with one click.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Endpoint health unavailable</AlertTitle>
            <AlertDescription>
              Current checks could not be loaded.
              <Button variant="outline" size="sm" onClick={() => mutate()}>
                Retry
              </Button>
            </AlertDescription>
          </Alert>
        ) : !data ? (
          <p role="status">Loading endpoint health…</p>
        ) : !data.enabled ? (
          <p>Endpoint monitoring is disabled for this environment.</p>
        ) : monitors.length === 0 ? (
          <p>
            No connections are being monitored. An operator must configure
            monitoring before health can be reported.
          </p>
        ) : (
          <>
            {monitors.some((m) => m.last_checked_at && !m.alerts_enabled) && (
              <p className="text-muted-foreground text-sm">
                Slack alerts are disabled or no channel is configured. Results
                remain available here.
              </p>
            )}
            {issues.length === 0 && (
              <p role="status" className="text-sm">
                No endpoint issues detected. All connections checked since{" "}
                {lastCheck && new Date(lastCheck).toLocaleString()}.
              </p>
            )}
            {visible.map((m) => {
              const status =
                m.stale || !m.last_outcome
                  ? "overdue"
                  : m.last_outcome === "monitor_error"
                    ? "monitor_error"
                    : m.incident_id
                      ? "failure"
                      : m.last_outcome;
              const style = checkStyles[status];
              const StatusIcon = style.icon;
              return (
                <Fragment key={m.id}>
                  <div
                    className={cn(
                      "flex items-center gap-3 rounded-md border border-l-4 px-3 py-2",
                      style.row
                    )}
                  >
                    <StatusIcon
                      aria-hidden="true"
                      className={cn("size-4 shrink-0", style.iconColor)}
                    />
                    <span
                      className="min-w-0 flex-1 truncate font-medium"
                      title={m.name}
                    >
                      {m.name}
                    </span>
                    <Badge
                      variant="outline"
                      className={cn("shrink-0 whitespace-nowrap", style.badge)}
                      title={m.error ?? undefined}
                    >
                      {status === "overdue"
                        ? "Checks overdue"
                        : m.last_outcome === "monitor_error"
                          ? "Check could not run"
                          : m.incident_id
                            ? "Repeated failures"
                            : m.last_outcome === "failure"
                              ? "Confirming failure"
                              : "Healthy"}
                    </Badge>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="shrink-0"
                      aria-expanded={m.id === selectedId}
                      onClick={() =>
                        selectMonitor(m.id === selectedId ? null : m.id)
                      }
                    >
                      {m.id === selectedId ? "Hide checks" : "Inspect checks"}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      className="shrink-0"
                      disabled={
                        checkRequest?.id === m.id &&
                        (checkRequest.state === "sending" ||
                          (checkRequest.state === "scheduled" &&
                            checkRequest.observedAt === m.last_checked_at))
                      }
                      onClick={() => requestCheck(m.id)}
                    >
                      {checkRequest?.id === m.id &&
                      checkRequest.state === "sending"
                        ? "Requesting…"
                        : checkRequest?.id === m.id &&
                            checkRequest.state === "scheduled" &&
                            checkRequest.observedAt === m.last_checked_at
                          ? "Queued"
                          : "Check again"}
                    </Button>
                    {checkRequest?.id === m.id &&
                      checkRequest.state === "scheduled" &&
                      checkRequest.observedAt === m.last_checked_at && (
                        <span role="status" className="sr-only">
                          Check requested; waiting for the monitor.
                        </span>
                      )}
                    {checkRequest?.id === m.id &&
                      checkRequest.state === "error" && (
                        <span role="alert" className="text-destructive text-xs">
                          Request failed. Try again.
                        </span>
                      )}
                  </div>
                  {m.id === selectedId && historyPanel}
                </Fragment>
              );
            })}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setShowAll(!showAll)}
            >
              {showAll
                ? "Show issues only"
                : `All connections and history (${monitors.length})`}
            </Button>
          </>
        )}
        {selectedId && !selectedVisible && historyPanel}
      </CardContent>
    </Card>
  );
}
