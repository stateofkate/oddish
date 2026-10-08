"use client";

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { clearTaskFilters, setTaskFilters, updateTaskSearchParams } from "@/lib/tasks-filters";
import { TooltipProvider } from "@/components/ui/tooltip";
import { TasksHeader } from "./tasks-client";
import { TasksFilters } from "./tasks-filters";
import {
  SelectionBar,
  SelectionProvider,
} from "./selection-context";
import { RecentTasksResults } from "./recent-tasks-results";

export const dynamic = "force-dynamic";

export default function TasksPage() {
  const searchParams = useSearchParams();
  // Free-text search lives in the URL `q` param (debounced). `query` is the
  // legacy param some deep links still use — read it as a fallback.
  const urlSearch = searchParams.get("q") ?? searchParams.get("query") ?? "";
  const [searchQuery, setSearchQuery] = useState(urlSearch);

  // Search values committed below whose navigations haven't landed yet. Lets
  // the re-sync effect tell "our own commit landing" (skip — the input may
  // already be ahead of it) from an external URL change.
  const pendingSearchCommits = useRef<string[]>([]);

  const searchTimer = useRef<number | null>(null);

  // Re-sync the input when the URL search text changes externally (back/forward,
  // applying a saved filter, Clear all) — but never for our own commits landing,
  // which would clobber whatever the user has typed since.
  useEffect(() => {
    const pending = pendingSearchCommits.current;
    const landed = pending.indexOf(urlSearch);
    if (landed !== -1) {
      pending.splice(0, landed + 1);
      return;
    }
    pendingSearchCommits.current = [];
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
    setSearchQuery((prev) => (prev.trim() === urlSearch ? prev : urlSearch));
  }, [urlSearch]);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      updateTaskSearchParams((params) => {
        const trimmed = searchQuery.trim();
        // Already committed (e.g. a whitespace-only edit) — skip the refetch.
        if (trimmed === (params.get("q") ?? params.get("query") ?? "")) return;
        if (trimmed) params.set("q", trimmed);
        else params.delete("q");
        params.delete("query"); // collapse the legacy param into `q`
        pendingSearchCommits.current.push(trimmed);
      });
    }, 300);
    searchTimer.current = handle;
    return () => window.clearTimeout(handle);
  }, [searchQuery]);

  const clearFilters = () => {
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
    pendingSearchCommits.current = [];
    setSearchQuery("");
    clearTaskFilters();
  };

  return (
    <SelectionProvider>
      <TooltipProvider>
        <div className="space-y-5 pb-24" data-testid="tasks-browser">
          <TasksHeader />
          <div className="grid items-start gap-6 md:grid-cols-[280px_minmax(0,1fr)]">
          <TasksFilters
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            onSearchFilter={outcome => {
              if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
              pendingSearchCommits.current = [];
              setSearchQuery("");
              updateTaskSearchParams(params => { params.delete("q"); params.delete("query"); });
              setTaskFilters({ qaOutcomes: [outcome] });
            }}
            onClearFilters={clearFilters}
          />
          <div className="min-w-0"><RecentTasksResults onClearFilters={clearFilters} /></div>
          </div>
          <div className="fixed bottom-4 left-4 z-40 w-max max-w-[calc(100vw-2rem)]">
            <SelectionBar />
          </div>
        </div>
      </TooltipProvider>
    </SelectionProvider>
  );
}
