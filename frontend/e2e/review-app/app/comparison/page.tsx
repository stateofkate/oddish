"use client";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SavedFiltersMenu } from "@/components/saved-filters-menu";
import { Suspense } from "react";
import { SelectionProvider } from "@/app/(app)/tasks/selection-context";
import { RecentTasksResults } from "@/app/(app)/tasks/recent-tasks-results";
export default function Page() {
  return (
    <Suspense>
      <SelectionProvider>
        <TooltipProvider>
          <SavedFiltersMenu />
          <RecentTasksResults onClearFilters={() => {}} />
        </TooltipProvider>
      </SelectionProvider>
    </Suspense>
  );
}
