"use client";

import Link from "next/link";
import { useIncoming } from "@/lib/use-org-access";
import { useOrgHref } from "@/lib/use-org-href";
import type { SharedDelivery } from "@/lib/types";
import { formatRelativeTime } from "@/lib/utils";

export default function SharedDeliveriesPage() {
  const orgHref = useOrgHref();
  const { data, error } = useIncoming<SharedDelivery[]>("deliveries");

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Deliveries</h1>
      {error ? (
        <p role="alert" className="text-sm text-red-500">
          Unable to load deliveries.
        </p>
      ) : !data ? (
        <p role="status" className="text-muted-foreground text-sm">
          Loading...
        </p>
      ) : data.length === 0 ? (
        <p className="text-muted-foreground text-sm">No deliveries yet.</p>
      ) : (
        <ul className="divide-y rounded-lg border">
          {data.map((delivery) => (
            <li key={delivery.id}>
              <Link
                href={orgHref(
                  `/shared/deliveries/${encodeURIComponent(delivery.id)}`
                )}
                className="hover:bg-muted/50 flex items-baseline justify-between gap-4 px-4 py-3"
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">
                    {delivery.name}
                  </span>
                  <span className="text-muted-foreground block truncate text-sm">
                    {delivery.task_count}{" "}
                    {delivery.task_count === 1 ? "task" : "tasks"}
                    {delivery.description ? ` · ${delivery.description}` : ""}
                  </span>
                </span>
                {delivery.finalized_at && (
                  <span className="text-muted-foreground shrink-0 text-xs">
                    {formatRelativeTime(delivery.finalized_at)}
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
