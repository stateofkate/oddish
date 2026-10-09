"use client";

import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { SharedExperiment } from "@/lib/types";

/** The active org's access and type, cached per org. Undefined while loading. */
function useOrgInfo() {
  const { isSignedIn, orgId } = useAuth();
  const { data } = useSWR<{ access?: string; org_type?: string }>(
    isSignedIn && orgId ? `/api/org?org=${encodeURIComponent(orgId)}` : null,
    fetcher,
    { revalidateOnFocus: false }
  );
  return data;
}

/**
 * True for a view-only org: it cannot run jobs, so it sees only what is shared
 * with it. A customer org given "Runs jobs" gets the app minus Abundant's own
 * pages (see useIsCustomerOrg).
 */
export function useIsViewerOrg(): boolean | undefined {
  const data = useOrgInfo();
  return data ? data.access === "viewer" : undefined;
}

/** True for a customer org, whatever its access: never Deliveries,
 *  Leaderboard or Admin, which are Abundant's own tools. */
export function useIsCustomerOrg(): boolean | undefined {
  const data = useOrgInfo();
  return data ? data.org_type === "customer" : undefined;
}

/** Experiments shared with the active org, cached per org like the above. */
export function useIncomingShares() {
  const { isSignedIn, orgId } = useAuth();
  return useSWR<SharedExperiment[]>(
    isSignedIn && orgId
      ? `/api/org-shares/incoming?org=${encodeURIComponent(orgId)}`
      : null,
    fetcher,
    { shouldRetryOnError: false }
  );
}

/** Tasks and deliveries shared with the active org, cached per org. */
export function useIncoming<T>(kind: "tasks") {
  const { isSignedIn, orgId } = useAuth();
  return useSWR<T>(
    isSignedIn && orgId
      ? `/api/org-shares/incoming/${kind}?org=${encodeURIComponent(orgId)}`
      : null,
    fetcher
  );
}

/** A shared task's page, at the version that shipped when there is one. */
export function sharedTaskHref(id: string, version: number | null) {
  const query = version != null ? `?version=${version}` : "";
  return `/shared/tasks/${encodeURIComponent(id)}${query}`;
}
