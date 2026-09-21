"use client";

import Link from "next/link";
import { useSWRConfig } from "swr";
import {
  isBrowseTaskOpen,
  taskOpenFromBrowse,
  taskOpenKey,
  type TaskOpenResource,
} from "@/lib/task-open-resource";
import type { TaskBrowseItem } from "@/lib/types";

// Preserve the loaded browse data for immediate detail rendering, without
// replacing a canonical task response already in the shared cache.
export function TaskBrowseLink({
  task,
  className,
}: {
  task: TaskBrowseItem;
  className?: string;
}) {
  const { mutate } = useSWRConfig();
  return (
    <Link
      href={`/tasks/${encodeURIComponent(task.id)}`}
      className={className}
      onClick={() => {
        const snapshot = taskOpenFromBrowse(task);
        void mutate(
          taskOpenKey(task.id),
          (current: TaskOpenResource | undefined) =>
            current && !isBrowseTaskOpen(current) ? current : snapshot,
          { revalidate: false }
        );
      }}
    >
      {task.name}
    </Link>
  );
}
