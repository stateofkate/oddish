"use client";

import { useEffect, useState, useTransition } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ImportDialog } from "@/components/import-dialog";
import { useSelection } from "./selection-context";
import {
  useTaskBrowseCount,
  useTaskBrowseRevalidate,
} from "@/lib/use-task-browse";
import { cn } from "@/lib/utils";

const AUTO_REFRESH_KEY = "oddish.tasks.autoRefresh";
const REFRESH_MS = 60000;

export function TasksHeader() {
  const { deliveryId, delivery, deliveryError } = useSelection();
  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div>
        {deliveryId ? (
          <Link
            href={`/deliveries/${encodeURIComponent(deliveryId)}`}
            className="text-muted-foreground mb-2 block text-sm"
          >
            ← Back to delivery
          </Link>
        ) : null}
        <h1 className="text-2xl font-semibold">
          {deliveryId
            ? `Add tasks${delivery ? ` to ${delivery.name}` : ""}`
            : "Tasks"}
        </h1>
        {delivery?.customer_name ? (
          <p className="text-muted-foreground text-sm">
            {delivery.customer_name}
          </p>
        ) : null}
        {deliveryError ? (
          <p role="alert" className="text-destructive text-sm">
            {deliveryError}
          </p>
        ) : null}
      </div>
      <TasksToolbar showImport={!deliveryId} />
    </header>
  );
}

// How many tasks match the active filters across every page — the grid shows
// at most TASKS_PAGE_SIZE of them. Its own request, keyed on the filters
// alone, so it neither delays the cards nor re-runs when you page.
export function TasksMatchCount() {
  const searchParams = useSearchParams();
  const { total, isStale, error } = useTaskBrowseCount(
    new URLSearchParams(searchParams.toString())
  );
  if (error) return <span>Task count unavailable</span>;
  if (total === null || isStale) return <span role="status">Counting tasks…</span>;
  return <span>{total.toLocaleString()} matching {total === 1 ? "task" : "tasks"}</span>;
}

// Every refresh path revalidates the grid's client-side browse fetch only —
// nothing the page server-renders depends on task data, so there is no
// router.refresh().
function TasksToolbar({ showImport }: { showImport: boolean }) {
  const revalidateBrowse = useTaskBrowseRevalidate();
  const [isPending, startTransition] = useTransition();
  const [autoRefresh, setAutoRefresh] = useState(true);

  // Restore the saved preference client-side (avoids a hydration mismatch).
  useEffect(() => {
    try {
      setAutoRefresh(window.localStorage.getItem(AUTO_REFRESH_KEY) !== "0");
    } catch {
      /* Use the default when storage is unavailable. */
    }
  }, []);

  const toggleAuto = (next: boolean) => {
    setAutoRefresh(next);
    try {
      window.localStorage.setItem(AUTO_REFRESH_KEY, next ? "1" : "0");
    } catch {
      /* Keep the in-memory preference. */
    }
  };

  // Silent background refresh only while auto-refresh is on.
  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") void revalidateBrowse();
    }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [autoRefresh, revalidateBrowse]);

  // The async transition keeps the spinner honest: isPending tracks the
  // browse fetch itself. Failures surface in the grid's error banner.
  const manualRefresh = () =>
    startTransition(async () => {
      await revalidateBrowse();
    });

  return (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="h-8 w-8 border-[#6f88b4]/20"
        onClick={manualRefresh}
        disabled={isPending}
        aria-label="Refresh tasks"
        title="Refresh tasks"
      >
        <RefreshCw className={cn("h-4 w-4", isPending && "animate-spin")} />
      </Button>
      <Button variant="outline" aria-label="Live updates" aria-pressed={autoRefresh} onClick={() => toggleAuto(!autoRefresh)}>
        <span className={cn("mr-2 h-2 w-2 rounded-full", autoRefresh ? "bg-emerald-500" : "bg-muted-foreground")} />
        {autoRefresh ? "Live" : "Paused"}
      </Button>
      {showImport ? (
        <ImportDialog onImported={() => void revalidateBrowse()} />
      ) : null}
    </div>
  );
}
