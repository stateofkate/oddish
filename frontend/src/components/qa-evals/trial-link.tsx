"use client";

import { trialHref } from "@/lib/qa-evals";
import { useOrgHref } from "@/lib/use-org-href";

/** A trial's drawer on its task page, in a new tab so the list stays put. */
export function TrialLink({
  taskId,
  trialId,
  className,
  title,
  children,
}: {
  taskId: string;
  trialId: string;
  className?: string;
  title?: string;
  children: React.ReactNode;
}) {
  const orgHref = useOrgHref();
  return (
    <a
      href={orgHref(trialHref(taskId, trialId))}
      target="_blank"
      rel="noreferrer"
      className={className}
      title={title}
    >
      {children}
    </a>
  );
}
