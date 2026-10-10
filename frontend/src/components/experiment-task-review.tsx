"use client";

import useSWR from "swr";
import { TaskReviewDialog } from "@/components/task-review-dialog";
import { fetcher } from "@/lib/api";
import { resolveExperimentTaskVersion } from "@/lib/experiment-task-version";
import type { Task, TaskVersionSummary } from "@/lib/types";

export function ExperimentTaskReview({ task }: { task: Task }) {
  const { data: versions } = useSWR<TaskVersionSummary[]>(
    `/api/tasks/${encodeURIComponent(task.id)}/versions`,
    fetcher,
    { revalidateOnFocus: false }
  );
  const selectedVersion = versions?.find(
    (version) => version.version === resolveExperimentTaskVersion(task)
  );
  if (!versions || !selectedVersion) return null;
  return (
    <TaskReviewDialog
      key={`${task.id}:${selectedVersion.id}`}
      taskId={task.id}
      taskName={task.name}
      versions={versions}
      selectedVersionId={selectedVersion.id}
    />
  );
}
