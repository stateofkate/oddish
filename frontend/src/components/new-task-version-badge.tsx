import { ArrowUpRight } from "lucide-react";
import Link from "next/link";

import type { Task } from "@/lib/types";

/**
 * Flags an experiment task whose trials ran an older version than the task's
 * default. Links to the default version on the task page, since the
 * experiment itself has no trials for it.
 */
export function NewTaskVersionBadge({
  task,
  readOnly = false,
}: {
  task: Task;
  /** Public share views do not show task version updates. */
  readOnly?: boolean;
}) {
  const shown = task.trial_version;
  const latest = task.current_version;
  if (readOnly || shown == null || latest == null || latest <= shown) return null;

  const label = `Newer task version available · v${shown} of v${latest}`;
  const badgeClass =
    "inline-flex shrink-0 items-center gap-0.5 rounded-[3px] bg-amber-100 px-1 py-px font-mono text-[9.5px] leading-[14px] font-medium whitespace-nowrap text-amber-800 dark:bg-amber-900/40 dark:text-amber-300";

  // `from=new-version` makes the task page flash the version it opened on.
  const query = task.current_version_id
    ? `?version=${encodeURIComponent(task.current_version_id)}&from=new-version`
    : "?from=new-version";
  return (
    <Link
      prefetch={false}
      href={`/tasks/${encodeURIComponent(task.id)}${query}`}
      className={`${badgeClass} hover:underline`}
    >
      {label}
      <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
    </Link>
  );
}
