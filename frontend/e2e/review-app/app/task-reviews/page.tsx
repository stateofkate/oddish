"use client";
import { TaskReviewDialog } from "@/components/task-review-dialog";
export default function Page() {
  return (
    <TaskReviewDialog
      taskId="review-task"
      taskName="Example task"
      selectedVersionId="v2"
      versions={[
        { id: "v2", version: 2, is_current: true },
        { id: "v1", version: 1, is_current: false },
      ]}
    />
  );
}
