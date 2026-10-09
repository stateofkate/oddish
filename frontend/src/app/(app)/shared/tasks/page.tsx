"use client";

import Link from "next/link";
import { sharedTaskHref, useIncoming } from "@/lib/use-org-access";
import { useOrgHref } from "@/lib/use-org-href";
import type { SharedTask } from "@/lib/types";
import { formatRelativeTime } from "@/lib/utils";

export default function SharedTasksPage() {
  const orgHref = useOrgHref();
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
        <ul className="divide-y rounded-lg border">
          {data.map((task) => (
            <li key={task.id}>
              <Link
                href={orgHref(sharedTaskHref(task.id, task.version))}
                className="hover:bg-muted/50 flex items-baseline justify-between gap-4 px-4 py-3"
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">
                    {task.name}
                  </span>
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
      )}
    </div>
  );
}
