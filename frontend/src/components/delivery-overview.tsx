"use client";

import { DELIVERY_STATES, type DeliveryTaskState } from "@/lib/deliveries";
import type { DeliveryPageResponse } from "@/lib/types";

export function DeliveryOverview({ board }: { board: DeliveryPageResponse }) {
  const counts = board.owner_counts;
  return (
    <section aria-label="Delivery overview" className="space-y-5">
      <dl
        className={`grid gap-4 py-4 ${counts.awaiting_signoff ? "grid-cols-2 sm:grid-cols-4" : "grid-cols-3"}`}
      >
        {(
          Object.entries(DELIVERY_STATES) as [
            DeliveryTaskState,
            (typeof DELIVERY_STATES)[DeliveryTaskState],
          ][]
        )
          .filter(([key]) => key !== "awaiting_signoff" || counts[key] > 0)
          .map(([key, state]) => (
            <div key={key}>
              <dt className="text-muted-foreground text-sm">{state.label}</dt>
              <dd className="mt-2 text-3xl font-medium tabular-nums">
                {counts[key]}
              </dd>
            </div>
          ))}
      </dl>
    </section>
  );
}
