"use client";

import { SharedTaskList } from "@/components/shared-task-list";
import { useIncoming } from "@/lib/use-org-access";
import type { SharedTask } from "@/lib/types";

export default function SharedTasksPage() {
  const { data, error } = useIncoming<SharedTask[]>("tasks");

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Tasks</h1>
      {error ? (
        <p role="alert" className="text-sm text-red-500">
          Unable to load shared tasks.
        </p>
      ) : !data ? (
        <p role="status" className="text-muted-foreground text-sm">
          Loading...
        </p>
      ) : data.length === 0 ? (
        <p className="text-muted-foreground text-sm">Nothing shared yet.</p>
      ) : (
        <SharedTaskList tasks={data} />
      )}
    </div>
  );
}
