"use client";

import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { SharedExperiment } from "@/lib/types";

/**
 * True for a customer org (or any view-only one): it sees only what is shared
 * with it. Undefined while loading. The org id in the key keeps one cache per
 * org.
 */
export function useIsViewerOrg(): boolean | undefined {
  const { isSignedIn, orgId } = useAuth();
  const { data } = useSWR<{ access?: string; org_type?: string }>(
    isSignedIn && orgId ? `/api/org?org=${encodeURIComponent(orgId)}` : null,
    fetcher,
    { revalidateOnFocus: false }
  );
  return data
    ? data.org_type === "customer" || data.access === "viewer"
    : undefined;
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
