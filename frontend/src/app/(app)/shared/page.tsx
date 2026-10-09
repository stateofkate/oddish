"use client";

import Link from "next/link";
import { useIncomingShares } from "@/lib/use-org-access";
import { useOrgHref } from "@/lib/use-org-href";
import { formatRelativeTime } from "@/lib/utils";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export default function SharedPage() {
  const orgHref = useOrgHref();
  const { data, error } = useIncomingShares();

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Samples</h1>
      {error ? (
        <p role="alert" className="text-sm text-red-500">
          Unable to load samples.
        </p>
      ) : !data ? (
        <p role="status" className="text-muted-foreground text-sm">
          Loading...
        </p>
      ) : data.length === 0 ? (
        <p className="text-muted-foreground text-sm">Nothing shared yet.</p>
      ) : (
        <ul className="divide-y rounded-lg border">
          {data.map((experiment) => (
            <li key={experiment.id}>
              <Link
                href={orgHref(`/shared/${encodeURIComponent(experiment.id)}`)}
                className="hover:bg-muted/50 flex items-baseline justify-between gap-4 px-4 py-3"
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">
                    {experiment.name}
                  </span>
                  {experiment.description && (
                    <span className="text-muted-foreground block truncate text-sm">
                      {experiment.description}
                    </span>
                  )}
                  <span className="text-muted-foreground block text-xs">
                    {plural(experiment.task_count, "task")} ·{" "}
                    {plural(experiment.run_count, "run")}
                  </span>
                </span>
                <span className="text-muted-foreground shrink-0 text-xs">
                  {formatRelativeTime(experiment.shared_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
