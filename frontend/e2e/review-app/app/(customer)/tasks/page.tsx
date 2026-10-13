"use client";

import { Suspense } from "react";
import { SelectionProvider } from "@/app/(app)/tasks/selection-context";
import { SharedTasksSection } from "@/app/(app)/tasks/tasks-client";

// The Tasks page's shared section alone; the grid needs the full backend.
export default function TasksFixture() {
  return (
    <Suspense>
      <SelectionProvider>
        <SharedTasksSection />
      </SelectionProvider>
    </Suspense>
  );
}
