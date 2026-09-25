"use client";

import useSWR from "swr";
import { QaSplit } from "@/components/qa-split";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import type { QaIssueReport } from "@/lib/qa-issues";

/** Client-side so the board's refresh after each action refreshes it too. */
export function deliveryQaKey(deliveryId: string) {
  return `/api/deliveries/${encodeURIComponent(deliveryId)}/qa-issues`;
}

export function DeliveryQaCard({ deliveryId }: { deliveryId: string }) {
  const { data, error } = useSWR<QaIssueReport>(
    deliveryQaKey(deliveryId),
    fetcher
  );
  // A failed refresh keeps the last counts; only a failed first load hides it.
  if (!data) return error ? null : <Skeleton className="h-56 rounded-xl" />;
  return (
    <QaSplit
      summary={data.summary}
      burndown={data.burndown}
      href={`/deliveries/${encodeURIComponent(deliveryId)}/qa-issues`}
      pointsHref={`/deliveries/${encodeURIComponent(deliveryId)}/qa-issues`}
    />
  );
}
