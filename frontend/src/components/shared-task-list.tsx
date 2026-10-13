"use client";

import Link from "next/link";
import { sharedTaskHref } from "@/lib/use-org-access";
import { useOrgHref } from "@/lib/use-org-href";
import type { SharedTask } from "@/lib/types";
import { cn, formatRelativeTime } from "@/lib/utils";

/** Tasks shared with the org, each opening at the version that shipped. */
export function SharedTaskList({
  tasks,
  className,
}: {
  tasks: SharedTask[];
  className?: string;
}) {
  const orgHref = useOrgHref();
  return (
    <ul className={cn("divide-y rounded-lg border", className)}>
      {tasks.map((task) => (
        <li key={task.id}>
          <Link
            href={orgHref(sharedTaskHref(task.id, task.version))}
            className="hover:bg-muted/50 flex items-baseline justify-between gap-4 px-4 py-3"
          >
            <span className="min-w-0">
              <span className="block truncate font-medium">{task.name}</span>
              {task.delivery_name && (
                <span className="text-muted-foreground block truncate text-sm">
                  {task.delivery_name}
                  {task.version != null ? ` · v${task.version}` : ""}
                </span>
              )}
            </span>
            {task.shared_at && (
              <span className="text-muted-foreground shrink-0 text-xs">
                {formatRelativeTime(task.shared_at)}
              </span>
            )}
          </Link>
        </li>
      ))}
    </ul>
  );
}
