"use client";

import { TaskFilesPanel } from "@/components/task-files-panel";

export default function EagerTaskFilesFixture() {
  return (
    <TaskFilesPanel
      isOpen
      onClose={() => {}}
      taskId="task-a"
      taskVersion={7}
      activePane="file"
      showAnalysis={false}
      contentOnly
    />
  );
}
