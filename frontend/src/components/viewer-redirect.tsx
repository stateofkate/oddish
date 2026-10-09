"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useIsCustomerOrg, useIsViewerOrg } from "@/lib/use-org-access";
import { useAppPathname, useOrgHref } from "@/lib/use-org-href";

// View-only customer orgs own no data; every other page would be empty or 403.
const VIEWER_PATHS = ["/shared", "/settings"];
// Abundant's own tools, closed to every customer org even when it runs jobs.
const ABUNDANT_ONLY_PATHS = ["/deliveries", "/leaderboard", "/admin", "/usage"];

export function ViewerRedirect() {
  const viewer = useIsViewerOrg();
  const customer = useIsCustomerOrg();
  const pathname = useAppPathname();
  const orgHref = useOrgHref();
  const router = useRouter();
  const blocked = viewer
    ? !VIEWER_PATHS.some((path) => pathname.startsWith(path))
    : customer === true &&
      ABUNDANT_ONLY_PATHS.some((path) => pathname.startsWith(path));
  useEffect(() => {
    if (blocked) router.replace(orgHref("/shared"));
  }, [blocked, orgHref, router]);
  return null;
}
