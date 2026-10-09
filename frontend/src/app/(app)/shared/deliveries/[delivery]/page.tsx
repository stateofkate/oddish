"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { sharedTaskHref } from "@/lib/use-org-access";
import { useOrgHref } from "@/lib/use-org-href";
import type { SharedDeliveryBoard } from "@/lib/types";
import { formatRelativeTime } from "@/lib/utils";

const CHECK_TONE: Record<string, string> = {
  pass: "text-emerald-600",
  waived: "text-muted-foreground",
  off: "text-muted-foreground",
};

/** A delivery as it shipped to this org. */
export default function SharedDeliveryPage() {
  const params = useParams();
  const id = Array.isArray(params.delivery)
    ? params.delivery[0]
    : params.delivery;
  const orgHref = useOrgHref();
  const { isSignedIn } = useAuth();
  const { data, error } = useSWR<SharedDeliveryBoard>(
    isSignedIn && id
      ? `/api/org-shares/incoming/deliveries/${encodeURIComponent(id)}`
      : null,
    fetcher
  );

  return (
    <div className="space-y-4">
      <Link
        href={orgHref("/shared/deliveries")}
        className="text-muted-foreground text-sm"
      >
        ← Deliveries
      </Link>
      {error ? (
        <p role="alert" className="text-sm text-red-500">
          Unable to load this delivery.
        </p>
      ) : !data ? (
        <p role="status" className="text-muted-foreground text-sm">
          Loading...
        </p>
      ) : (
        <>
          <div>
            <h1 className="text-xl font-semibold">{data.name}</h1>
            <p className="text-muted-foreground text-sm">
              {data.task_count} {data.task_count === 1 ? "task" : "tasks"}
              {data.finalized_at
                ? ` · delivered ${formatRelativeTime(data.finalized_at)}`
                : ""}
            </p>
            {data.description && <p className="mt-2">{data.description}</p>}
          </div>
          <ul className="divide-y rounded-lg border">
            {data.tasks.map((task) => (
              <li key={task.task_id} className="space-y-1 px-4 py-3">
                <div className="flex items-baseline justify-between gap-4">
                  <Link
                    href={orgHref(sharedTaskHref(task.task_id, task.version))}
                    className="truncate font-medium hover:underline"
                  >
                    {task.task_name}
                  </Link>
                  {task.version != null && (
                    <span className="text-muted-foreground shrink-0 text-xs">
                      v{task.version}
                    </span>
                  )}
                </div>
                {task.customer_note && (
                  <p className="text-sm">{task.customer_note}</p>
                )}
                {task.checks.length > 0 && (
                  <p className="flex flex-wrap gap-x-3 text-xs">
                    {task.checks.map((check) => (
                      <span
                        key={check.label}
                        className={CHECK_TONE[check.status] ?? "text-amber-600"}
                      >
                        {check.label}: {check.status}
                      </span>
                    ))}
                  </p>
                )}
                {task.findings.length > 0 && (
                  <ul className="text-muted-foreground list-disc pl-5 text-xs">
                    {task.findings.map((finding) => (
                      <li key={finding}>{finding}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
